#!/usr/bin/env python3
"""Пульт, сторож и надёжная доставка: чего стоит «архив живёт в Telegram».

Постоянного архива на диске нет и не будет — записи ищут в темах. Отсюда три
проверяемых здесь обязательства: событие, не доехавшее до темы, не исчезает
молча; поломка камеры не выглядит как спокойная ночь; управлять камерой можно
из чата, но адреса и пароли этим путём не меняются.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


import httpx  # noqa: E402

from cctv.bot import bot as bot_module  # noqa: E402
from cctv.bot.bot import CctvBot, human_time  # noqa: E402
from cctv.bot.bridge import Bridge  # noqa: E402
from cctv.bot.events import normalize_event  # noqa: E402
from cctv.bot.state import State  # noqa: E402
from test_cctv_contract_20260824 import BRIDGE, make_config  # noqa: E402
from test_cctv_flow_20260824 import FakeTelegram, OWNER  # noqa: E402

PHOTO = b"jpeg-bytes"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


class MoscowTime(unittest.TestCase):
    def test_utc_marks_are_shown_in_moscow_time(self) -> None:
        """По проводу UTC, человеку — МСК: время события искали, сложив три часа."""
        self.assertEqual("04.09.2026 14:19:53 МСК", human_time("2026-09-04T11:19:53Z"))
        self.assertEqual("04.09.2026 14:06:52 МСК", human_time("2026-09-04T11:06:52+00:00"))

    def test_date_rolls_over_correctly(self) -> None:
        self.assertEqual("16.01.2026 02:40:00 МСК", human_time("2026-01-15T23:40:00Z"))

    def test_missing_and_broken_marks_do_not_break_caption(self) -> None:
        self.assertEqual("время неизвестно", human_time(None))
        self.assertEqual("мусор", human_time("мусор"))


class ControlTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.status = "online"
        self.title = "Город"
        self.motion_health = {"state": "watching", "reason": "", "last_motion_at": None}
        self.storage = {"used_bytes": 1024, "budget_bytes": 8 * 1024 ** 3,
                        "free_bytes": 80 * 1024 ** 3}
        self.state_calls: list[dict] = []

        def handler(request: httpx.Request):
            if request.url.path == "/v1/cameras":
                return httpx.Response(200, json={
                    "cameras": [{"camera_id": "city", "title": self.title,
                                 "site": "city", "status": self.status,
                                 "last_frame_at": "2026-09-04T10:00:00Z",
                                 "motion": self.motion_health}],
                    "storage": self.storage})
            if request.url.path.endswith("/state"):
                body = json.loads(request.content)
                body["path"] = request.url.path
                self.state_calls.append(body)
                return httpx.Response(200, json={"camera_id": "city",
                                                 "action": body["action"]})
            if request.url.path == "/v1/media-requests":
                return httpx.Response(202, json={"status": "accepted"})
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = FakeTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    async def register(self) -> None:
        await self.bot.on_event(normalize_event({
            "event_id": "e-reg", "type": "camera.registered", "camera_id": "city",
            "title": "Город", "site": "city",
            "occurred_at": "2026-09-04T10:00:00Z"}))

    def motion(self, event_id="e-motion"):
        return normalize_event({"event_id": event_id, "type": "motion.detected",
                                "camera_id": "city",
                                "occurred_at": "2026-09-04T11:01:00Z",
                                "snapshot": {"url": f"{BRIDGE}/v1/media/opaque",
                                             "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})

    def texts(self) -> str:
        return " | ".join(str(call.get("text", "")) for call in self.tg.of("send_message"))

    def button(self, action: str) -> str:
        markup = self.tg.of("send_message")[0]["reply_markup"]
        for row in markup.inline_keyboard:
            for item in row:
                if item.callback_data.startswith(f"cv:{action}:"):
                    return item.callback_data
        raise AssertionError(f"нет кнопки {action}")

    # --- доставка ---------------------------------------------------------
    async def test_delivery_retries_and_publishes_once(self) -> None:
        """Обрыв на первой попытке не должен стоить кадра в архиве."""
        await self.register()
        attempts = {"n": 0}
        original = self.tg.send_photo

        async def flaky(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("сеть моргнула")
            return await original(**kwargs)

        self.tg.send_photo = flaky
        bot_module.DELIVERY_BACKOFF_SEC = (0, 0)
        await self.bot.on_event(self.motion())
        self.assertEqual(2, attempts["n"])
        self.assertEqual(1, len(self.tg.of("send_photo")))

    async def test_exhausted_delivery_warns_and_lets_event_repeat(self) -> None:
        """Событие, не доехавшее до темы, не существует нигде — молчать нельзя."""
        await self.register()

        async def always_fails(**kwargs):
            raise httpx.ConnectError("сети нет")

        self.tg.send_photo = always_fails
        bot_module.DELIVERY_BACKOFF_SEC = (0, 0)
        await self.bot.on_event(self.motion("e-lost"))
        self.assertIn("не доставлено", self.texts())
        # Отметка дедупликации снята: повтор того же события от Bridge пройдёт.
        self.assertTrue(self.state.is_new_event("e-lost"))

    # --- сторож -----------------------------------------------------------
    async def test_watchdog_reports_only_transitions(self) -> None:
        await self.register()
        await self.bot.watch_health()          # первое наблюдение — не новость
        before = len(self.tg.of("send_message"))
        self.status = "unavailable"
        await self.bot.watch_health()
        await self.bot.watch_health()          # состояние не менялось — молчим
        after = self.tg.of("send_message")[before:]
        self.assertEqual(1, len(after))
        self.assertIn("Нет кадров", after[0]["text"])

    async def test_watchdog_reports_recovery_and_dead_detector(self) -> None:
        await self.register()
        await self.bot.watch_health()
        self.status = "unavailable"
        await self.bot.watch_health()
        self.status = "online"
        await self.bot.watch_health()
        self.assertIn("снова на связи", self.texts())
        self.motion_health = {"state": "stalled", "reason": "heartbeat_stale",
                              "last_motion_at": None}
        await self.bot.watch_health()
        self.assertIn("Детектор молчит", self.texts())

    async def test_camera_broken_before_start_is_still_announced(self) -> None:
        """Бот мог подняться, когда камера уже мертва: «сменится» ему не с чего."""
        await self.register()
        self.status = "unavailable"
        await self.bot.watch_health()
        self.assertIn("Нет кадров", self.texts())

    async def test_watchdog_is_silent_about_pause(self) -> None:
        """Пауза — решение человека, а не поломка: сторож о ней не рассказывает."""
        await self.register()
        await self.bot.watch_health()
        before = len(self.tg.of("send_message"))
        self.status = "paused"
        await self.bot.watch_health()
        self.assertEqual(before, len(self.tg.of("send_message")))

    async def test_storage_overflow_is_announced(self) -> None:
        await self.register()
        self.storage = {"used_bytes": 9 * 1024 ** 3, "budget_bytes": 8 * 1024 ** 3,
                        "free_bytes": 500 * 1024 ** 2}
        await self.bot.watch_health()
        self.assertIn("Хранилище моста", self.texts())

    # --- управление -------------------------------------------------------
    async def test_pause_goes_to_bridge_and_never_touches_credentials(self) -> None:
        await self.register()
        answer = await self.bot.on_callback(OWNER, 101, self.button("pause"))
        self.assertIn("паузе", answer)
        self.assertEqual([{"action": "pause", "path": "/v1/cameras/city/state"}],
                         self.state_calls)

    async def test_rename_applies_after_text_and_renames_topic(self) -> None:
        await self.register()
        asked = await self.bot.on_callback(OWNER, 101, self.button("rename"))
        self.assertIn("новое имя", asked)
        answer = await self.bot.on_text(OWNER, 101, "Двор у ворот")
        self.assertIn("Двор у ворот", answer)
        self.assertEqual("rename", self.state_calls[0]["action"])
        self.assertEqual("Двор у ворот", self.state_calls[0]["title"])
        self.assertEqual("Двор у ворот", self.tg.of("edit_forum_topic")[0]["name"])

    async def test_retire_requires_confirmation_word(self) -> None:
        """Случайное нажатие не должно уносить камеру из пульта."""
        await self.register()
        await self.bot.on_callback(OWNER, 101, self.button("retire"))
        self.assertEqual("Снятие отменено.", await self.bot.on_text(OWNER, 101, "ой нет"))
        self.assertEqual([], self.state_calls)
        await self.bot.on_callback(OWNER, 101, self.button("retire"))
        answer = await self.bot.on_text(OWNER, 101, "снять")
        self.assertIn("снята", answer)
        self.assertEqual("retire", self.state_calls[0]["action"])
        self.assertEqual(1, len(self.tg.of("close_forum_topic")))

    async def test_console_topic_and_button_scope(self) -> None:
        await self.register()
        console = await self.bot.ensure_console()
        await self.bot.refresh_console()
        names = [call["name"] for call in self.tg.of("create_forum_topic")]
        self.assertIn("Пульт", names)
        panel = [call for call in self.tg.of("send_message")
                 if call.get("message_thread_id") == console][-1]
        refresh = [item.callback_data for row in panel["reply_markup"].inline_keyboard
                   for item in row if item.callback_data.startswith("cv:panel:")][0]
        self.assertEqual("Пульт обновлён.", await self.bot.on_callback(OWNER, console, refresh))
        # Та же кнопка из темы камеры не работает: у пульта своя область.
        await self.bot.refresh_console()
        panel = [call for call in self.tg.of("send_message")
                 if call.get("message_thread_id") == console][-1]
        self.assertEqual("Кнопка не относится к этой теме.",
                         await self.bot.on_callback(OWNER, 101, refresh))

    async def test_forced_refresh_replaces_stale_buttons(self) -> None:
        """После выката текст панели тот же, а кнопки новые — их надо донести."""
        await self.register()
        before = len(self.tg.of("edit_message_text"))
        await self.bot.refresh_all_panels()
        self.assertEqual(before, len(self.tg.of("edit_message_text")))
        await self.bot.refresh_all_panels(force=True)
        self.assertEqual(before + 1, len(self.tg.of("edit_message_text")))

    # --- иконки тем -------------------------------------------------------
    async def test_new_topics_get_icons(self) -> None:
        await self.register()
        await self.bot.ensure_console()
        icons = [call.get("icon_custom_emoji_id") for call in self.tg.of("create_forum_topic")]
        # Иконка темы камеры выбирается по площадке: Город — городская.
        self.assertEqual([bot_module.topic_icon("Город"), bot_module.TOPIC_ICON_CONSOLE],
                         icons)

    async def test_existing_topics_get_icons_once(self) -> None:
        """Правка темы всплывает у владельца как событие — делаем её один раз."""
        await self.register()
        await self.bot.ensure_console()
        before = len(self.tg.of("edit_forum_topic"))
        await self.bot.ensure_topic_icons()
        first = self.tg.of("edit_forum_topic")[before:]
        self.assertEqual(2, len(first))  # тема камеры и пульт
        self.assertEqual(bot_module.topic_icon("Город"), first[0]["icon_custom_emoji_id"])
        await self.bot.ensure_topic_icons()
        self.assertEqual(before + 2, len(self.tg.of("edit_forum_topic")))

    async def test_icon_migration_survives_already_set_topics(self) -> None:
        """Повторная простановка той же иконки — «not modified», а не провал."""
        await self.register()
        await self.bot.ensure_console()
        calls = {"n": 0}

        async def picky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Bad Request: TOPIC_NOT_MODIFIED")
            return None

        self.tg.edit_forum_topic = picky
        await self.bot.ensure_topic_icons()
        self.assertEqual(2, calls["n"])       # соседнюю тему не бросили
        await self.bot.ensure_topic_icons()
        self.assertEqual(2, calls["n"])       # отметка поставлена, повтора нет

    async def test_request_for_text_stays_in_the_topic(self) -> None:
        """Кнопка, ждущая ответа, обязана оставить след в теме, а не тост.

        Ответ на нажатие живёт секунды: отложил телефон — и чат выглядит
        молчащим, будто кнопка сломана. Проверяем оба обещания: просьба ушла
        сообщением в тему камеры, и она многострочная — тогда Telegram
        показывает её окном с «ОК», а не исчезающей подсказкой.
        """
        await self.register()
        thread_id = self.tg.of("send_message")[0]["message_thread_id"]
        before = len(self.tg.of("send_message"))
        asked = await self.bot.on_callback(OWNER, 101, self.button("rename"))
        posted = self.tg.of("send_message")[before:]
        self.assertEqual(1, len(posted))
        self.assertEqual(thread_id, posted[0]["message_thread_id"])
        self.assertIn("новое имя", posted[0]["text"])
        self.assertIn("новое имя", asked)
        self.assertIn("\n", asked)

    async def test_confirmation_requests_are_visible_too(self) -> None:
        """Снятие и удаление просят слово-подтверждение — их тоже видно в теме."""
        await self.register()
        before = len(self.tg.of("send_message"))
        asked = await self.bot.on_callback(OWNER, 101, self.button("retire"))
        posted = self.tg.of("send_message")[before:]
        self.assertEqual(1, len(posted))
        self.assertIn("снять", posted[0]["text"])
        self.assertIn("\n", asked)

        # Удаление из реестра живёт не на панели, а в карточке настройки:
        # токен берём напрямую — для нажатия он всё равно непрозрачный.
        drop = "cv:drop:" + self.state.issue_callback("city", "drop", 600)
        before = len(self.tg.of("send_message"))
        asked = await self.bot.on_callback(OWNER, 101, drop)
        posted = self.tg.of("send_message")[before:]
        self.assertEqual(1, len(posted))
        self.assertIn("удалить", posted[0]["text"])
        self.assertIn("\n", asked)

    async def test_closed_topic_does_not_break_the_button(self) -> None:
        """Тема закрылась между нажатием и ответом — человек всё равно узнает, что делать."""
        await self.register()

        async def refuses(**kwargs):
            raise RuntimeError("тема закрыта")

        self.tg.send_message = refuses
        asked = await self.bot.on_callback(OWNER, 101, self.button("rename"))
        self.assertIn("новое имя", asked)

    async def test_rename_keeps_the_icon(self) -> None:
        await self.register()
        await self.bot.on_callback(OWNER, 101, self.button("rename"))
        await self.bot.on_text(OWNER, 101, "Двор у ворот")
        call = self.tg.of("edit_forum_topic")[-1]
        self.assertEqual("Двор у ворот", call["name"])
        # Имя сменилось, площадка — нет: иконка остаётся площадкой камеры.
        self.assertEqual(bot_module.topic_icon("city"), call["icon_custom_emoji_id"])

    async def test_console_shows_state_and_storage(self) -> None:
        await self.register()
        text = await self.bot.console_text()
        self.assertIn("Город", text)
        self.assertIn("в работе", text)
        self.assertIn("Хранилище", text)
        self.assertIn("МСК", text)


if __name__ == "__main__":
    unittest.main()
