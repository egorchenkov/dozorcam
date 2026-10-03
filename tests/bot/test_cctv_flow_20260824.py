#!/usr/bin/env python3
"""CCTV-интерфейс: сценарии тем, кнопок и публикации.

Настоящие `CctvBot`, `State` и `Bridge`; подменены только Bot API и HTTP-
транспорт Bridge. Проверяется именно приёмка platform: одна тема на повторную
регистрацию, кнопки без LLM, отказ чужому пользователю и чужой теме, повтор
события не создаёт второй пост.
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import sys
import tempfile
import unittest


import httpx  # noqa: E402

from cctv.bot.bot import CctvBot  # noqa: E402
from cctv.bot.bridge import Bridge  # noqa: E402
from cctv.bot.events import normalize_event  # noqa: E402
from cctv.bot.state import State  # noqa: E402
from test_cctv_contract_20260824 import BRIDGE, make_config  # noqa: E402

OWNER = 7
STRANGER = 99
PHOTO = b"jpeg-bytes"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class FakeTelegram:
    """Bot API ровно в том объёме, который сервису разрешено использовать."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_thread = 100

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))

    def of(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]

    async def create_forum_topic(self, **kwargs):
        self._record("create_forum_topic", kwargs)
        self._next_thread += 1
        return {"message_thread_id": self._next_thread}

    async def edit_forum_topic(self, **kwargs):
        self._record("edit_forum_topic", kwargs)

    async def close_forum_topic(self, **kwargs):
        self._record("close_forum_topic", kwargs)

    async def send_message(self, **kwargs):
        self._record("send_message", kwargs)
        return {"message_id": len(self.calls)}

    async def pin_chat_message(self, **kwargs):
        self._record("pin_chat_message", kwargs)

    async def send_photo(self, **kwargs):
        self._record("send_photo", kwargs)
        return {"message_id": len(self.calls)}

    async def send_video(self, **kwargs):
        self._record("send_video", kwargs)
        return {"message_id": len(self.calls)}

    async def edit_message_text(self, **kwargs):
        self._record("edit_message_text", kwargs)
        return {"message_id": kwargs.get("message_id")}


class FlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.media_requests: list[dict] = []
        self.motion_health = {"state": "watching", "reason": "", "last_motion_at": None}
        self.last_frame_at = "2026-08-24T10:00:00Z"

        def handler(request: httpx.Request):
            if request.url.path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    {"camera_id": "city", "title": "Город", "site": "city",
                     "status": "online", "last_frame_at": self.last_frame_at,
                     "motion": self.motion_health},
                ]})
            if request.url.path == "/v1/media-requests":
                import json as _json
                body = _json.loads(request.content)
                self.media_requests.append(body)
                return httpx.Response(202, json={"request_id": body["request_id"],
                                                 "status": "accepted"})
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = FakeTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    def registered(self, event_id="e-reg"):
        return normalize_event({"event_id": event_id, "type": "camera.registered",
                                "camera_id": "city", "title": "Город",
                                "site": "city", "occurred_at": "2026-08-24T10:00:00Z"})

    def motion(self, event_id="e-motion"):
        return normalize_event({"event_id": event_id, "type": "motion.detected",
                                "camera_id": "city", "occurred_at": "2026-08-24T10:01:00Z",
                                "snapshot": {"url": f"{BRIDGE}/v1/media/opaque",
                                             "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})

    async def test_repeated_registration_creates_exactly_one_topic(self):
        await self.bot.on_event(self.registered("e1"))
        await self.bot.on_event(self.registered("e2"))
        self.assertEqual(1, len(self.tg.of("create_forum_topic")))
        self.assertEqual(1, len(self.tg.of("pin_chat_message")))

    async def test_passport_buttons_carry_no_camera_id_in_callback(self):
        """Кадр, клип, статус, движение, пауза, имя, снятие и настройка камеры."""
        await self.bot.on_event(self.registered())
        markup = self.tg.of("send_message")[0]["reply_markup"]
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(8, len(datas))
        self.assertTrue(all(d.startswith("cv:") for d in datas))
        self.assertFalse(any("city" in d for d in datas))

    async def test_stranger_and_stale_button_get_nothing(self):
        await self.bot.on_event(self.registered())
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][0].callback_data
        before = len(self.tg.calls)
        self.assertEqual("Нет доступа.", await self.bot.on_callback(STRANGER, 101, data))
        self.assertEqual("Кнопка устарела, откройте /menu.",
                         await self.bot.on_callback(OWNER, 101, "cv:snap:подделка"))
        self.assertEqual(before, len(self.tg.calls))
        self.assertEqual([], self.media_requests)

    async def test_button_does_not_work_from_another_topic(self):
        await self.bot.on_event(self.registered())
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual("Кнопка не относится к этой теме.",
                         await self.bot.on_callback(OWNER, 4242, data))
        self.assertEqual([], self.media_requests)

    async def test_snapshot_button_sends_idempotent_request(self):
        await self.bot.on_event(self.registered())
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][0].callback_data
        await self.bot.on_callback(OWNER, 101, data)
        self.assertEqual(1, len(self.media_requests))
        self.assertEqual("snapshot", self.media_requests[0]["kind"])
        self.assertNotIn("rtsp", str(self.media_requests[0]))

    async def test_clip_button_carries_fixed_duration(self):
        await self.bot.on_event(self.registered())
        buttons = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard
        clip = buttons[0][1].callback_data
        await self.bot.on_callback(OWNER, 101, clip)
        self.assertEqual("clip", self.media_requests[0]["kind"])
        self.assertEqual(30, self.media_requests[0]["duration_sec"])

    async def test_repeated_motion_event_does_not_double_post(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion("m1"))
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual(1, len(self.tg.of("send_photo")))

    async def test_person_detector_source_is_captioned_as_person(self):
        # source приходит из cctv_pipeline как "recorded_main_person_detector",
        # не как голое "person_detector" — регресс на 604b0f4/eadc08c.
        await self.bot.on_event(self.registered())
        event = normalize_event({"event_id": "e-person", "type": "motion.detected",
                                 "camera_id": "city", "occurred_at": "2026-08-24T10:01:00Z",
                                 "source": "recorded_main_person_detector",
                                 "snapshot": {"url": f"{BRIDGE}/v1/media/opaque",
                                              "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})
        await self.bot.on_event(event)
        caption = self.tg.of("send_photo")[0]["caption"]
        self.assertTrue(caption.startswith("Обнаружен человек"))

    async def test_plain_motion_source_is_captioned_as_motion(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion())
        caption = self.tg.of("send_photo")[0]["caption"]
        self.assertTrue(caption.startswith("Движение"))

    async def test_motion_for_unknown_camera_is_ignored(self):
        await self.bot.on_event(self.motion("m2"))
        self.assertEqual([], self.tg.of("send_photo"))
        self.assertEqual([], self.tg.of("create_forum_topic"))

    async def test_published_frame_offers_clip_around_itself(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion())
        photo = self.tg.of("send_photo")[0]
        clip_data = photo["reply_markup"].inline_keyboard[0][0].callback_data
        camera_id, action, center_at = self.state.resolve_callback(clip_data.split(":", 2)[2])
        self.assertEqual(("city", "clip"), (camera_id, action))
        self.assertEqual("2026-08-24T10:01:00Z", center_at)
        await self.bot.on_callback(OWNER, 101, clip_data)
        self.assertEqual("2026-08-24T10:01:00Z", self.media_requests[0]["center_at"])

    async def test_text_reply_to_motion_frame_requests_its_exact_clip(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion())
        message_id = self.state.db.execute("SELECT message_id FROM frame_replies").fetchone()[0]
        answer = await self.bot.on_text(OWNER, 101, "клип", message_id)
        self.assertEqual("Запрос клипа отправлен.", answer)
        self.assertEqual("city", self.media_requests[-1]["camera_id"])
        self.assertEqual("2026-08-24T10:01:00Z", self.media_requests[-1]["center_at"])

    async def test_text_reply_refuses_foreign_topic_and_expired_frame(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion())
        message_id = self.state.db.execute("SELECT message_id FROM frame_replies").fetchone()[0]
        self.assertEqual("Этот кадр относится к другой теме камеры.",
                         await self.bot.on_text(OWNER, 4242, "клип", message_id))
        self.state.db.execute("UPDATE frame_replies SET expires_at=0 WHERE message_id=?", (message_id,))
        self.assertIn("истёк", await self.bot.on_text(OWNER, 101, "клип", message_id))

    async def test_media_ready_lands_in_requesting_topic_and_only_once(self):
        await self.bot.on_event(self.registered())
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][0].callback_data
        await self.bot.on_callback(OWNER, 101, data)
        request_id = self.media_requests[0]["request_id"]
        ready = {"event_id": "ready-1", "type": "media.ready", "camera_id": "city",
                 "request_id": request_id, "kind": "snapshot",
                 "captured_at": "2026-08-24T10:02:00Z", "content_type": "image/jpeg",
                 "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                              "bytes": len(PHOTO)}}
        await self.bot.on_event(normalize_event(ready))
        await self.bot.on_event(normalize_event(dict(ready, event_id="ready-2")))
        photos = self.tg.of("send_photo")
        self.assertEqual(1, len(photos))
        self.assertEqual(101, photos[0]["message_thread_id"])

    def panel_texts(self):
        return [call["text"] for call in self.tg.of("edit_message_text")]

    async def test_existing_topic_gets_a_panel_on_sync(self):
        """Тема городской камеры была заведена до панели: без добора живого статуса
        в ней не появилось бы никогда."""
        await self.bot.on_event(self.registered())
        self.state.db.execute("DELETE FROM panels")
        await self.bot.sync_registry()
        self.assertIsNotNone(self.state.panel_for("city"))
        self.assertIn("Детекция:", self.panel_texts()[-1])

    async def test_panel_shows_live_detector_health(self):
        """«Детекция включена» обязана означать живой детектор, а не подписку:
        подписка — состояние пользователя, живость приходит из реестра Bridge."""
        await self.bot.on_event(self.registered())
        self.assertIn("🟢 детектор смотрит поток", self.panel_texts()[-1])
        self.assertIn("🔕 выключены", self.panel_texts()[-1])

        self.motion_health = {"state": "stalled", "reason": "heartbeat_stale",
                              "last_motion_at": "2026-08-24T10:00:00Z"}
        await self.bot.refresh_panel("city")
        self.assertIn("🔴 детектор не отвечает", self.panel_texts()[-1])

    async def test_fresh_frames_alone_do_not_redraw_the_panel(self):
        """Кадры идут постоянно, и панель, привязанная к их метке, правилась
        каждую минуту: тема всплывала у владельца как новое событие.
        Перерисовка допустима только при смене сути."""
        await self.bot.on_event(self.registered())
        before = len(self.panel_texts())
        for minute in range(3):
            self.last_frame_at = f"2026-08-24T10:0{minute + 1}:00Z"
            await self.bot.refresh_panel("city")
        self.assertEqual(before, len(self.panel_texts()))

        self.motion_health = {"state": "stalled", "reason": "heartbeat_stale",
                              "last_motion_at": None}
        await self.bot.refresh_panel("city")
        self.assertEqual(before + 1, len(self.panel_texts()))

    async def test_panel_marks_detector_unknown_when_bridge_is_silent(self):
        await self.bot.on_event(self.registered())
        self.motion_health = {}
        await self.bot.refresh_panel("city")
        self.assertIn("состояние детектора неизвестно", self.panel_texts()[-1])

    async def test_subscription_switch_updates_panel_instead_of_new_message(self):
        await self.bot.on_event(self.registered())
        before = len(self.tg.of("send_message"))
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[1][1].callback_data
        await self.bot.on_callback(OWNER, 101, data)
        self.assertEqual(before, len(self.tg.of("send_message")), "подписка снова пишет в ленту")
        self.assertIn("🔔 включены", self.panel_texts()[-1])

    async def test_status_button_answers_with_live_state(self):
        """Регрессия 29.08.2026: «Статус» отвечал «Панель обновлена.» — и это
        ничего не говорило о камере, а при неизменной панели было ещё и неправдой."""
        await self.bot.on_event(self.registered())
        answer = await self.bot.on_text(OWNER, 101, "🔄 Статус")
        self.assertNotIn("Панель обновлена", answer)
        self.assertIn("🟢 онлайн", answer)
        self.assertIn("🟢 детектор смотрит поток", answer)
        self.assertIn("🔕 выключены", answer)
        self.assertEqual([], self.media_requests, "статус не заказывает медиа")

    async def test_status_stays_informative_when_panel_is_unchanged(self):
        """Панель не перерисовывается, если суть не изменилась, — ответ кнопки
        всё равно обязан нести живое состояние."""
        await self.bot.on_event(self.registered())
        before = len(self.panel_texts())
        answer = await self.bot.on_text(OWNER, 101, "🔄 Статус")
        self.assertEqual(before, len(self.panel_texts()), "лишняя правка панели")
        self.assertIn("Детекция:", answer)

    async def test_persistent_keyboard_button_works_by_topic(self):
        """Постоянная клавиатура одна на чат: камеру обязана давать тема."""
        await self.bot.on_event(self.registered())
        # Telegram selective keyboard присылает кнопку как reply на сообщение,
        # с которым клавиатура была показана (в живом сбое это был id 794).
        answer = await self.bot.on_text(OWNER, 101, "📷 Кадр", reply_to_message_id=794)
        self.assertIn("кадр придёт", answer)
        self.assertEqual("snapshot", self.media_requests[-1]["kind"])
        self.assertEqual("city", self.media_requests[-1]["camera_id"])

    async def test_persistent_keyboard_refuses_stranger_and_foreign_topic(self):
        await self.bot.on_event(self.registered())
        self.assertEqual("Нет доступа.", await self.bot.on_text(STRANGER, 101, "📷 Кадр"))
        self.assertIn("в теме камеры", await self.bot.on_text(OWNER, 777, "📷 Кадр"))
        self.assertEqual([], self.media_requests)

    async def test_persistent_keyboard_is_not_swallowed_by_pending_input(self):
        """Ожидание подтверждения не должно отключать нижнюю клавиатуру."""
        await self.bot.on_event(self.registered())
        self.state.expect_input(OWNER, "city", "retire", 300)
        answer = await self.bot.on_text(OWNER, 101, "📷 Кадр")
        self.assertIn("кадр придёт", answer)
        self.assertEqual("snapshot", self.media_requests[-1]["kind"])
        # Запрос подтверждения остался осознанным отдельным действием, а не
        # был неявно отменён нажатием служебной кнопки.
        self.assertEqual("Снятие отменено.",
                         await self.bot.on_text(OWNER, 101, "не снимать"))

    async def test_free_text_still_gets_only_the_menu_hint(self):
        await self.bot.on_event(self.registered())
        answer = await self.bot.on_text(OWNER, 101, "покажи что происходит во дворе")
        self.assertIn("/menu", answer)
        self.assertEqual([], self.media_requests)

    async def test_motion_clip_lands_in_camera_topic(self):
        """Регрессия 24.08.2026: у клипа движения request_id — это event_id самого
        движения, бот его не заказывал и молча выбрасывал каждый такой клип."""
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion("m-1"))
        await self.bot.on_event(normalize_event(
            {"event_id": "clip-1", "type": "media.ready", "camera_id": "city",
             "request_id": "m-1", "source_event_id": "m-1", "kind": "clip",
             "occurred_at": "2026-08-24T10:01:00Z", "captured_at": "2026-08-24T10:01:00Z",
             "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                          "bytes": len(PHOTO)}}))
        videos = self.tg.of("send_video")
        self.assertEqual(1, len(videos))
        self.assertEqual(101, videos[0]["message_thread_id"])

    async def test_second_camera_gets_its_own_topic_and_keeps_its_traffic(self):
        """Приёмка карточки дачи: вторая камера не делит тему с первой.

        Проверяется не только факт разных thread_id, но и адресация всего её
        трафика — кадр движения и ответный клип обязаны уйти в её собственную
        тему, а тема первой камеры остаться нетронутой.
        """
        await self.bot.on_event(self.registered("reg-1"))
        await self.bot.on_event(normalize_event(
            {"event_id": "reg-2", "type": "camera.registered", "camera_id": "dacha",
             "title": "Дача", "site": "dacha", "occurred_at": "2026-08-24T10:00:05Z"}))
        first = self.state.topic_for("city").thread_id
        second = self.state.topic_for("dacha").thread_id
        self.assertNotEqual(first, second)

        await self.bot.on_event(normalize_event(
            {"event_id": "m-2", "type": "motion.detected", "camera_id": "dacha",
             "occurred_at": "2026-08-24T10:01:00Z",
             "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                          "bytes": len(PHOTO)}}))
        await self.bot.on_event(normalize_event(
            {"event_id": "clip-2", "type": "media.ready", "camera_id": "dacha",
             "request_id": "m-2", "source_event_id": "m-2", "kind": "clip",
             "occurred_at": "2026-08-24T10:01:00Z", "captured_at": "2026-08-24T10:01:00Z",
             "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                          "bytes": len(PHOTO)}}))
        published = self.tg.of("send_photo") + self.tg.of("send_video")
        self.assertEqual(2, len(published))
        self.assertTrue(all(call["message_thread_id"] == second for call in published))
        self.assertFalse(any(call["message_thread_id"] == first for call in published))

    async def test_media_failed_reports_reason_into_requesting_topic(self):
        """Регрессия 24.08.2026: media.failed бот не знал, отвечал 400 и терял отказ —
        кнопка «Клип вокруг кадра» выглядела сломанной, в тему не приходило ничего."""
        await self.bot.on_event(self.registered())
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][1].callback_data
        await self.bot.on_callback(OWNER, 101, data)
        request_id = self.media_requests[0]["request_id"]
        failed = normalize_event({"event_id": "failed-1", "type": "media.failed",
                                  "camera_id": "city", "request_id": request_id,
                                  "kind": "clip", "error": "clip_window_empty",
                                  "occurred_at": "2026-08-24T10:02:00Z"})
        await self.bot.on_event(failed)
        texts = [call["text"] for call in self.tg.of("send_message")]
        self.assertTrue(any("вышел из буфера" in text for text in texts), texts)
        self.assertEqual(101, self.tg.of("send_message")[-1]["message_thread_id"])

    async def test_media_failed_with_unknown_code_still_reaches_the_topic(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(normalize_event(
            {"event_id": "failed-2", "type": "media.failed", "camera_id": "city",
             "kind": "snapshot", "error": "какой-то-новый-код",
             "occurred_at": "2026-08-24T10:02:00Z"}))
        self.assertIn("Кадр не получен", self.tg.of("send_message")[-1]["text"])

    async def test_media_failed_without_reason_is_rejected(self):
        from cctv.bot.events import EventRejected

        with self.assertRaises(EventRejected):
            normalize_event({"event_id": "failed-3", "type": "media.failed",
                             "camera_id": "city", "kind": "clip",
                             "occurred_at": "2026-08-24T10:02:00Z"})

    async def test_temp_file_is_removed_after_publication(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion())
        self.assertEqual([], [p for p in self.tmp.iterdir() if p.name.startswith("cctv-")])

    async def test_retired_camera_closes_topic_and_disables_buttons(self):
        await self.bot.on_event(self.registered())
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][0].callback_data
        await self.bot.on_event(normalize_event(
            {"event_id": "e-ret", "type": "camera.retired", "camera_id": "city",
             "occurred_at": "2026-08-24T11:00:00Z"}))
        self.assertEqual(1, len(self.tg.of("close_forum_topic")))
        self.assertEqual("Камера снята с эксплуатации.",
                         await self.bot.on_callback(OWNER, 101, data))

    async def test_registry_sync_is_idempotent(self):
        """Повтор синка не плодит тем: ни камерных, ни служебной темы пульта."""
        await self.bot.sync_registry()
        await self.bot.sync_registry()
        created = [call["name"] for call in self.tg.of("create_forum_topic")]
        self.assertEqual(["Город", "Пульт"], created)
        self.assertIn("Город", await self.bot.menu_text())

    async def test_no_secret_leaks_into_published_text(self):
        await self.bot.on_event(self.registered())
        await self.bot.on_event(self.motion())
        blob = repr(self.tg.calls).lower()
        for forbidden in ("rtsp", "onvif", "isapi", "password", "https://cctv-bridge", "0:test"):
            self.assertNotIn(forbidden, blob, forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
