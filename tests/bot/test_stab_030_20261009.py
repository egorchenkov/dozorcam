#!/usr/bin/env python3
"""Стабилизация 0.3.0 после жалобы 09.10.2026: регрессии аудита — во всех пресетах.

Жалоба: после переключения прода в «один чат без тем» в личку перестали приходить
события, в темах перестал работать кадр, кнопка подписки показывала одно, а
отвечала другое. Аудит (docs/Dozorcam_0.3.0_аудит_стабилизации_2026-10-09.md в
рабочем репо) нашёл 12 дефектов бота (Б-1…Б-12); решение владельца по доставке —
одновременно: событие уходит в ленту (тема камеры, тема локации или General) и в
личку каждому допущенному, кто включил её у камеры кнопкой «📩 Мне в личку».

Контракт гоняется в пяти вариантах установки: «тема на камеру», «тема на
локацию», «плоско» в группе с темами (переключили из тем — как прод 09.10),
«плоско» в обычной группе и «плоско» без группы. Отдельно — переходы между
пресетами туда-обратно: темы, панели и закрепы не плодятся, личка и кадр
работают после каждого шага.

Настоящие CctvBot, State и Bridge; подменены только Bot API и HTTP моста.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import httpx
from telegram import ReplyKeyboardRemove
from telegram.error import Forbidden

from cctv.bot import bot as bot_module
from cctv.bot.bot import CctvBot, keyboard_action
from cctv.bot.bridge import Bridge
from cctv.bot.events import normalize_event
from cctv.bot.routes import Dest
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER, STRANGER, FakeTelegram

GROUP = -100500
FRIEND = 8
PHOTO = b"jpeg-bytes"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
# Две камеры одной площадки и одна своя (площадка = имя) — как мастер заводит камеры.
CAMERAS = [
    {"camera_id": "gate", "title": "Калитка", "site": "Дача"},
    {"camera_id": "porch", "title": "Крыльцо", "site": "Дача"},
    {"camera_id": "garage", "title": "Гараж", "site": "Гараж"},
]


class StabTelegram(FakeTelegram):
    """Bot API: группа (форум или обычная), закрепы и откреп, закрытая личка."""

    def __init__(self, *, forum: bool = True) -> None:
        super().__init__()
        self.forum = forum
        self.blocked: set[int] = set()  # лички, где бот заблокирован / «Старт» не нажат

    def _guard(self, name, kwargs):
        if kwargs.get("chat_id") in self.blocked:
            self._record(f"{name}_forbidden", kwargs)
            raise Forbidden("Forbidden: bot can't initiate conversation with a user")

    async def send_message(self, **kwargs):
        self._guard("send_message", kwargs)
        return await super().send_message(**kwargs)

    async def send_photo(self, **kwargs):
        self._guard("send_photo", kwargs)
        return await super().send_photo(**kwargs)

    async def send_video(self, **kwargs):
        self._guard("send_video", kwargs)
        return await super().send_video(**kwargs)

    async def unpin_chat_message(self, **kwargs):
        self._record("unpin_chat_message", kwargs)

    async def get_me(self):
        return type("Me", (), {"id": 4242, "username": "dozor_test_bot"})()

    async def get_chat(self, **kwargs):
        return type("Chat", (), {"is_forum": self.forum})()

    async def get_chat_member(self, **kwargs):
        return type("Member", (), {"status": "administrator"})()

    # --- что видит человек ---------------------------------------------------
    def texts(self) -> dict[tuple[int, int], str]:
        """Последний текст каждого сообщения бота (отправка или правка)."""
        result: dict[tuple[int, int], str] = {}
        for index, (name, kwargs) in enumerate(self.calls):
            if name == "send_message":
                result[(kwargs["chat_id"], index + 1)] = kwargs["text"]
            elif name == "edit_message_text":
                result[(kwargs["chat_id"], kwargs["message_id"])] = kwargs["text"]
        return result

    def pinned(self) -> set[tuple[int, int]]:
        """Закреплённые сейчас сообщения: закрепы минус откреп."""
        current: set[tuple[int, int]] = set()
        for name, kwargs in self.calls:
            key = (kwargs.get("chat_id"), kwargs.get("message_id"))
            if name == "pin_chat_message":
                current.add(key)
            elif name == "unpin_chat_message":
                current.discard(key)
        return current


class Harness:
    """Бот на трёх камерах; пресет START, затем (если иной) переключение в PRESET."""

    START = "camera"
    PRESET = "camera"
    GROUP_BOUND = True
    FORUM = True

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        cfg = dataclasses.replace(make_config(self.tmp), owner_ids=frozenset({OWNER}),
                                  allowed_user_ids=frozenset({OWNER, FRIEND}))
        if not self.GROUP_BOUND:
            cfg = dataclasses.replace(cfg, chat_id=None)
        self.cfg = cfg
        self.clock = [1_800_000_000.0]
        self.state = State(":memory:", now=lambda: self.clock[0])
        self.addCleanup(self.state.close)
        self.media_requests: list[dict] = []

        def handler(request: httpx.Request):
            path = request.url.path
            if path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    dict(c, status="online", last_frame_at="2026-10-09T10:00:00Z",
                         motion={"state": "watching", "reason": "", "last_motion_at": None})
                    for c in CAMERAS]})
            if path == "/v1/media-requests":
                body = json.loads(request.content)
                self.media_requests.append(body)
                return httpx.Response(202, json={"request_id": body["request_id"], "status": "accepted"})
            if path.startswith("/v1/detector"):
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = StabTelegram(forum=self.FORUM)
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)
        self.bot.routes.set_preset(self.START)
        # ffprobe/ffmpeg клипу не нужны: байты в тесте — не видео.
        for name, value in (("probe_video", {}), ("make_thumbnail", None)):
            patcher = mock.patch.object(bot_module, name, mock.AsyncMock(return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)

    async def prepare(self) -> None:
        for camera in CAMERAS:
            await self.bot.on_event(normalize_event({
                "event_id": f"reg-{camera['camera_id']}", "type": "camera.registered",
                "occurred_at": "2026-10-09T10:00:00Z", **camera}))
        await self.bot.sync_registry()
        if self.PRESET != self.START:
            answer = await self.bot.apply_mode(self.PRESET)
            self.assertIn("✅", answer)

    # --- события -------------------------------------------------------------
    def motion(self, event_id: str, camera_id: str = "gate", *, person: bool = True,
               at: str = "2026-10-09T10:01:00Z"):
        payload = {"event_id": event_id, "type": "motion.detected", "camera_id": camera_id,
                   "occurred_at": at, "captured_at": at,
                   "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                                "bytes": len(PHOTO)}}
        if person:
            payload["source"] = "recorded_main_person_detector"
        return normalize_event(payload)

    def motion_clip(self, source_event_id: str, camera_id: str = "gate"):
        return normalize_event({
            "event_id": f"clip-{source_event_id}", "type": "media.ready", "camera_id": camera_id,
            "source_event_id": source_event_id, "request_id": source_event_id, "kind": "clip",
            "captured_at": "2026-10-09T10:01:15Z",
            "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})

    def ready(self, request_id: str, camera_id: str = "gate", kind: str = "snapshot"):
        return normalize_event({"event_id": f"ready-{request_id}", "type": "media.ready",
                                "camera_id": camera_id, "request_id": request_id, "kind": kind,
                                "captured_at": "2026-10-09T10:02:00Z",
                                "download": {"url": f"{BRIDGE}/v1/media/opaque",
                                             "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})

    # --- помощники -----------------------------------------------------------
    def t(self, key: str, **params) -> str:
        return self.bot._t(key, **params)

    @staticmethod
    def where(call: dict) -> Dest:
        return Dest(call["chat_id"], call.get("message_thread_id"))

    def photos(self) -> list[dict]:
        return self.tg.of("send_photo")

    def message_id(self, call: dict) -> int:
        return next(i + 1 for i, (_n, kw) in enumerate(self.tg.calls) if kw is call)

    def feed(self, camera_id: str = "gate") -> Dest:
        return self.bot.routes.feed(camera_id)[0]

    def token(self, action: str, camera_id: str = "gate", payload: str | None = None) -> str:
        return f"cv:{action}:{self.state.issue_callback(camera_id, action, 600, payload)}"

    async def press(self, user: int, place: Dest, data: str, message_id: int | None = None) -> str:
        return await self.bot.on_callback(user, place.thread_id, data, chat_id=place.chat_id,
                                          message_id=message_id)

    def labels(self, markup) -> list[str]:
        return [b.text for row in markup.inline_keyboard for b in row]


class StabContract(Harness):
    """Сценарии жалобы и аудита, общие для всех вариантов установки."""

    async def asyncSetUp(self) -> None:
        await self.prepare()

    @property
    def grouped(self) -> bool:
        return self.GROUP_BOUND

    # --- Г / Б-1: событие в ленту и в личку подписчику одновременно -------------
    async def test_event_goes_to_feed_and_to_private_chat_of_subscriber(self) -> None:
        answer = await self.press(FRIEND, self.feed(), self.token("sub"))
        key = "notify.dm_on" if self.grouped else "notify.sound_on"
        self.assertEqual(self.t(key, title="Калитка"), answer)
        # Посторонний с записью подписки (снят с доступа мимо /invite) копий не получает.
        self.state.db.execute("INSERT INTO motion_subs(user_id, camera_id) VALUES(?, 'gate')", (STRANGER,))
        await self.bot.on_event(self.motion("m1"))
        places = [self.where(p) for p in self.photos()]
        if self.grouped:
            self.assertEqual([self.feed(), Dest(FRIEND)], places)
        else:
            self.assertEqual([Dest(OWNER), Dest(FRIEND)], places)
        self.assertNotIn(Dest(STRANGER), places)
        by_place = {self.where(p): p for p in self.photos()}
        # Лента тихая (кроме ленты без группы у неподписанного — тоже тихо), подписчик слышит.
        self.assertFalse(by_place[Dest(FRIEND)]["disable_notification"])
        first = places[0]
        self.assertTrue(by_place[first]["disable_notification"])
        # В личке камеру не назовёт тема — имя в подписи.
        self.assertTrue(by_place[Dest(FRIEND)]["caption"].startswith("Калитка · "))
        # Отписка — копии больше не идут.
        await self.press(FRIEND, self.feed(), self.token("sub"))
        await self.bot.on_event(self.motion("m2", at="2026-10-09T10:05:00Z"))
        self.clock[0] += 120
        await self.bot.on_event(self.motion("m3", at="2026-10-09T10:07:00Z"))
        late = [self.where(p) for p in self.photos()[2:]]
        if self.grouped:
            self.assertNotIn(Dest(FRIEND), late)
        else:  # без группы личка — сама лента, подписка — только звук
            self.assertIn(Dest(FRIEND), late)
            self.assertTrue(all(p["disable_notification"] for p in self.photos()[2:]))

    async def test_closed_private_chat_does_not_hold_the_feed(self) -> None:
        """Личка подписчика закрыта (бот заблокирован, «Старт» не нажат): лента
        получает событие, повторов в личку и «событие потеряно» туда нет."""
        await self.press(FRIEND, self.feed(), self.token("sub"))
        self.tg.blocked.add(FRIEND)
        with mock.patch.object(bot_module.asyncio, "sleep", mock.AsyncMock()) as slept:
            await self.bot.on_event(self.motion("m1"))
        self.assertEqual(1, len(self.tg.of("send_photo_forbidden")))
        self.assertEqual([], self.tg.of("send_message_forbidden"))
        slept.assert_not_awaited()
        # Без группы лента FRIEND — его же закрытая личка; лента владельца доехала.
        open_feed = self.feed() if self.grouped else Dest(OWNER)
        self.assertIn(open_feed, [self.where(p) for p in self.photos()])
        await self.bot.on_event(self.motion("m1"))  # повтор моста — ленте второй пост не нужен
        self.assertEqual(1, len([p for p in self.photos() if self.where(p) == open_feed]))

    # --- Б-2 / Б-3: кадр и клип там, где нажали ---------------------------------
    async def test_frame_button_and_reply_answer_where_pressed(self) -> None:
        await self.bot.on_event(self.motion("m1"))
        photo = self.photos()[-1]
        place = self.where(photo)
        data = photo["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual(self.t("media.clip_requested"), await self.press(OWNER, place, data))
        frame_id = self.message_id(photo)
        await self.bot.on_text(OWNER, place.thread_id, "клип", frame_id, 900, chat_id=place.chat_id)
        self.assertEqual(["clip", "clip"], [r["kind"] for r in self.media_requests])
        # «📷 Кадр» с карточки там же — кадр возвращается в это же место.
        await self.press(OWNER, place, self.token("snap"))
        await self.bot.on_event(self.ready(self.media_requests[-1]["request_id"]))
        self.assertEqual(place, self.where(self.photos()[-1]))

    async def test_camera_topic_keeps_working_in_every_preset(self) -> None:
        """Тема камеры (если она есть) — место камеры при любом пресете: панель,
        клавиатура и reply на кадр отвечают в неё же, а не в General."""
        topic = self.bot.routes.camera_topic("gate")
        if topic is None:
            self.skipTest("у этой установки тем камер нет")
        # Инлайн «📷 Кадр» на панели темы.
        answer = await self.press(OWNER, topic, self.token("snap"))
        self.assertEqual(self.t("media.snap_requested"), answer)
        await self.bot.on_event(self.ready(self.media_requests[-1]["request_id"]))
        frame = self.photos()[-1]
        self.assertEqual(topic, self.where(frame))
        # Клавиатура в теме: «📷 Кадр» и подписка — новой подписью и старой («🔔 Движение»).
        self.assertEqual(self.t("media.snap_requested"),
                         await self.bot.on_text(OWNER, topic.thread_id, "📷 Кадр", chat_id=GROUP))
        self.assertEqual(self.t("notify.dm_on", title="Калитка"),
                         await self.bot.on_text(OWNER, topic.thread_id, "📩 Мне в личку", chat_id=GROUP))
        self.assertEqual(self.t("notify.dm_off", title="Калитка"),
                         await self.bot.on_text(OWNER, topic.thread_id, "🔔 Движение", chat_id=GROUP))
        # Reply «клип» на кадр в теме — клип вокруг кадра, а не «другая тема».
        answer = await self.bot.on_text(OWNER, topic.thread_id, "клип", self.message_id(frame), 901,
                                        chat_id=GROUP)
        self.assertEqual(self.t("media.clip_requested"), answer)
        self.assertEqual("clip", self.media_requests[-1]["kind"])

    # --- Б-5: строка и кнопка подписки не спорят --------------------------------
    async def test_subscription_line_and_button_agree(self) -> None:
        shared = Dest(GROUP) if self.grouped else None
        if shared is not None:
            text, markup = await self.bot._render("card:gate", shared, await self.bot._registry())
            self.assertIn(self.t("panel.dm_nobody"), text)
            self.assertIn(self.t("action.dm"), self.labels(markup))
            await self.press(OWNER, self.feed(), self.token("sub"))
            await self.press(FRIEND, self.feed(), self.token("sub"))
            text, markup = await self.bot._render("card:gate", shared, await self.bot._registry())
            # Общий экран: сколько человек получают, кнопка нейтральна — кто бы ни нажал последним.
            self.assertIn(self.t("panel.dm_count", count=2), text)
            self.assertIn(self.t("action.dm"), self.labels(markup))
            self.assertNotIn("🔔 включены", text)
            panel = self.state.panel_for("gate")
            if panel is not None and self.bot.routes.camera_topic("gate") is not None:
                # Второй подписчик меняет строку — панель темы перерисована.
                self.assertIn(self.t("panel.dm_count", count=2), panel[1])
        else:
            await self.press(FRIEND, Dest(FRIEND), self.token("sub"))
        # В личке — про этого человека, явным глаголом.
        on_key, off_key = (("panel.dm_you_on", "panel.dm_you_off") if self.grouped
                           else ("panel.sound_on", "panel.sound_off"))
        stop, start = (("action.dm_off", "action.dm_on") if self.grouped
                       else ("action.sound_off", "action.sound_on"))
        registry = await self.bot._registry()
        text, markup = await self.bot._render("card:gate", Dest(FRIEND), registry)
        self.assertIn(self.t(on_key), text)
        self.assertIn(self.t(stop), self.labels(markup))
        if self.grouped:
            await self.press(OWNER, self.feed(), self.token("sub"))  # OWNER отписался
        text, markup = await self.bot._render("card:gate", Dest(OWNER), registry)
        self.assertIn(self.t(off_key), text)
        self.assertIn(self.t(start), self.labels(markup))
        # Статус отвечает строкой для места, где нажали.
        status = await self.press(FRIEND, Dest(FRIEND), self.token("stat"))
        self.assertIn(self.t(on_key), status)

    # --- Б-6: клип движения — ответом на пост, звук по месту ----------------------
    async def test_motion_clip_replies_to_event_post_with_place_sound(self) -> None:
        await self.press(FRIEND, self.feed(), self.token("sub"))
        await self.bot.on_event(self.motion("m1"))
        posts = {self.where(p): self.message_id(p) for p in self.photos()}
        await self.bot.on_event(self.motion_clip("m1"))
        clips = {self.where(v): v for v in self.tg.of("send_video")}
        self.assertEqual(set(posts), set(clips))
        for place, clip in clips.items():
            self.assertEqual(posts[place], clip["reply_to_message_id"], place)
            self.assertTrue(clip["allow_sending_without_reply"])
            self.assertEqual(self.bot.routes.silent_for("gate", place), clip["disable_notification"], place)
        self.assertFalse(clips[Dest(FRIEND)]["disable_notification"])
        # Событие, вклеенное в пост, — его клип тоже ответом на этот пост, и тихо.
        self.clock[0] += 20
        await self.bot.on_event(self.motion("m2", at="2026-10-09T10:01:20Z"))
        await self.bot.on_event(self.motion_clip("m2"))
        merged = self.tg.of("send_video")[len(clips):]
        self.assertEqual(len(clips), len(merged))
        for clip in merged:
            self.assertEqual(posts[self.where(clip)], clip["reply_to_message_id"])
            self.assertTrue(clip["disable_notification"])

    # --- Б-7: «человек» после «движения» звенит у подписчика ---------------------
    async def test_person_after_motion_is_a_new_post_where_it_rings(self) -> None:
        await self.press(FRIEND, self.feed(), self.token("sub"))
        await self.bot.on_event(self.motion("m1", person=False))
        self.clock[0] += 20
        await self.bot.on_event(self.motion("m2", person=True, at="2026-10-09T10:01:20Z"))
        new_posts = [self.where(p) for p in self.photos()]
        edits = [Dest(e["chat_id"], None) for e in self.tg.of("edit_message_media")]
        # У подписчика — второй пост со звуком; в тихой ленте — правка.
        self.assertEqual(2, new_posts.count(Dest(FRIEND)))
        self.assertFalse(self.photos()[-1]["disable_notification"])
        quiet = [p for p in set(new_posts) if p != Dest(FRIEND)]
        self.assertTrue(quiet)
        for place in quiet:
            self.assertEqual(1, new_posts.count(place))
            self.assertIn(Dest(place.chat_id, None), edits)

    # --- Б-11: тексты знают режим ----------------------------------------------
    async def test_texts_know_the_mode(self) -> None:
        preset = self.bot.routes.preset
        footer = {"camera": "menu.footer", "location": "menu.footer_location"}.get(preset, "menu.footer_flat")
        self.assertTrue((await self.bot.menu_text()).endswith(self.t(footer)))
        hint = {"camera": "hint.unknown", "location": "hint.unknown_location"}.get(preset, "hint.unknown_flat")
        place = self.feed()
        self.assertEqual(self.t(hint), await self.bot.on_text(OWNER, place.thread_id, "привет",
                                                              chat_id=place.chat_id))
        where = {"camera": "mode.where.camera", "location": "mode.where.location"}.get(
            preset, "mode.where.flat_group" if self.grouped else "mode.where.flat_private")
        self.assertEqual(self.t(where), self.bot.mode_where())

    # --- Б-4: клавиатура /menu только там, где работает --------------------------
    async def test_menu_keyboard_is_removed_where_it_cannot_work(self) -> None:
        self.assertIsInstance(self.bot.menu_keyboard(OWNER), ReplyKeyboardRemove)
        if not self.grouped:
            return
        markup = self.bot.menu_keyboard(GROUP)
        if self.state.active_topics():
            self.assertIsNone(markup)  # в группе с темами камер она работает — не трогать
        else:
            self.assertIsInstance(markup, ReplyKeyboardRemove)
        self.assertEqual("sub", keyboard_action("📩 Мне в личку"))
        self.assertEqual("sub", keyboard_action("🔔 Движение"))  # клавиатура до 0.3.1


class CameraPresetTest(StabContract, unittest.IsolatedAsyncioTestCase):
    """Тема на камеру (форум)."""


class LocationPresetTest(StabContract, unittest.IsolatedAsyncioTestCase):
    """Тема на локацию (форум), установка заведена сразу в этом пресете."""
    START = PRESET = "location"


class FlatForumGroupTest(StabContract, unittest.IsolatedAsyncioTestCase):
    """«Плоско» в группе с темами — переключили из «темы на камеру», как прод 09.10."""
    PRESET = "flat"


class FlatPlainGroupTest(StabContract, unittest.IsolatedAsyncioTestCase):
    """«Плоско» в обычной группе без тем."""
    START = PRESET = "flat"
    FORUM = False


class FlatPrivateTest(StabContract, unittest.IsolatedAsyncioTestCase):
    """«Плоско» без группы: личка каждого — лента, подписка — звук."""
    START = PRESET = "flat"
    GROUP_BOUND = False

    def feed(self, camera_id: str = "gate") -> Dest:
        return Dest(FRIEND)


class LocationSiteTest(Harness, unittest.IsolatedAsyncioTestCase):
    """Б-8: площадка, совпавшая с именем камеры, — локация, если она есть у соседей."""
    START = PRESET = "location"

    async def test_camera_named_like_its_site_stays_with_neighbours(self) -> None:
        CAMERAS.append({"camera_id": "dacha", "title": "Дача", "site": "Дача"})
        self.addCleanup(CAMERAS.pop)
        await self.prepare()
        self.assertEqual("Дача", self.bot.routes.location("dacha"))
        self.assertEqual(self.bot.routes.route("gate"), self.bot.routes.route("dacha"))
        self.assertEqual("", self.bot.routes.location("garage"))  # одна на площадке — не локация
        names = sorted(c["name"] for c in self.tg.of("create_forum_topic"))
        self.assertEqual(["Дача", "Камеры", "Пульт"], names)


class ConsoleAskerTest(Harness, unittest.IsolatedAsyncioTestCase):
    """Б-9: без группы ответ пульта — в личку нажавшего, а не владельца."""
    START = PRESET = "flat"
    GROUP_BOUND = False

    async def test_console_answer_goes_to_who_pressed(self) -> None:
        await self.prepare()
        before = len(self.tg.of("send_message"))
        token = self.token("model", bot_module.CONSOLE_CAMERA)
        await self.press(FRIEND, Dest(FRIEND), token)
        for _ in range(100):  # меню модели строится фоновой задачей (запрос моста в потоке)
            if len(self.tg.of("send_message")) > before:
                break
            await bot_module.asyncio.sleep(0.02)
        sent = self.tg.of("send_message")[before:]
        self.assertTrue(sent)
        self.assertEqual({FRIEND}, {m["chat_id"] for m in sent})
        self.assertEqual(Dest(OWNER), self.bot.routes.console())  # без нажатия — владелец


class TransitionsTest(Harness, unittest.IsolatedAsyncioTestCase):
    """Переходы camera ↔ location ↔ flat туда-обратно на одной установке с
    накопленным состоянием: подписка, панели, карта. После каждого шага темы,
    панели и закрепы не растут, старые карты откреплены и говорят «переехала»,
    личка получает событие, кадр с панели темы приходит в тему (Б-10, Г, Б-2)."""

    PATH = ("location", "flat", "camera", "flat", "location", "camera", "location", "camera")

    def map_pins(self) -> list[tuple[int, int]]:
        texts = self.tg.texts()
        header = self.t("map.header", cameras=3, online=3, events=0).split(":")[0]
        return [key for key in self.tg.pinned() if texts.get(key, "").startswith(header)]

    def maps_sent(self) -> int:
        header = self.t("map.header", cameras=3, online=3, events=0).split(":")[0]
        return len([m for m in self.tg.of("send_message") if m["text"].startswith(header)])

    def moved_pins(self) -> list[tuple[int, int]]:
        texts = self.tg.texts()
        return [key for key in self.tg.pinned()
                if texts.get(key) in (self.t("map.moved"), self.t("map.moved_group"))]

    async def step_checks(self, label: str, step: int) -> dict:
        await self.bot.refresh_console()
        await self.bot.sync_registry()
        result = {"topics": len(self.tg.of("create_forum_topic")), "panels": len(
            self.state.db.execute("SELECT * FROM panels").fetchall()), "maps": self.maps_sent(),
            "maps_pinned": len(self.map_pins()), "moved_pinned": len(self.moved_pins())}
        self.assertEqual(1, result["maps_pinned"], label)
        self.assertEqual(0, result["moved_pinned"], label)
        # Личка подписчика.
        before = len([p for p in self.photos() if p["chat_id"] == FRIEND])
        self.clock[0] += 120
        await self.bot.on_event(self.motion(f"m-{step}", at=f"2026-10-09T11:{step:02d}:00Z"))
        self.assertEqual(before + 1, len([p for p in self.photos() if p["chat_id"] == FRIEND]), label)
        # Кадр с панели темы камеры — в тему.
        topic = self.bot.routes.camera_topic("gate")
        self.assertIsNotNone(topic, label)
        await self.press(OWNER, topic, self.token("snap"))
        await self.bot.on_event(self.ready(self.media_requests[-1]["request_id"]))
        self.assertEqual(topic, self.where(self.photos()[-1]), label)
        return result

    async def test_round_trips_do_not_multiply_anything(self) -> None:
        await self.prepare()
        await self.press(FRIEND, self.feed(), self.token("sub"))
        first = await self.step_checks("camera (старт)", 0)
        seen = {"camera"}
        stable = None
        for step, preset in enumerate(self.PATH, start=1):
            answer = await self.bot.apply_mode(preset)
            self.assertIn("✅", answer, preset)
            result = await self.step_checks(f"шаг {step}: {preset}", step)
            seen.add(preset)
            if stable is not None:
                # Все пресеты уже пройдены: повторные заходы не заводят ни тем, ни новых карт —
                # прежняя карта места снова становится картой и закрепляется.
                self.assertEqual(stable, (result["topics"], result["maps"]), f"шаг {step}: {preset}")
            elif seen == {"camera", "location", "flat"}:
                stable = (result["topics"], result["maps"])
            self.assertEqual(first["panels"], result["panels"], f"шаг {step}: {preset}")
        # Всего тем: 3 камеры + пульт + 2 локации («Дача», «Камеры»); карт — пульт и General.
        self.assertEqual(6, len(self.tg.of("create_forum_topic")))
        self.assertEqual(2, self.maps_sent())


class PrivateToGroupTest(Harness, unittest.IsolatedAsyncioTestCase):
    """Б-1, Б-10: «Сюда» (личка), затем добавили группу — лента в группе,
    подписчик по-прежнему получает в личку, карта в личке откреплена."""
    START = PRESET = "flat"
    GROUP_BOUND = False

    async def test_group_added_later_keeps_private_copies(self) -> None:
        await self.prepare()
        await self.bot.refresh_console()
        await self.press(OWNER, Dest(OWNER), self.token("sub"))
        old_map = self.state.home_screen(OWNER, None).message_id
        self.assertIn((OWNER, old_map), self.tg.pinned())
        self.tg.forum = False
        await self.bot.bind_group(GROUP, OWNER)
        self.assertEqual(GROUP, self.bot.chat_id)
        self.assertNotIn((OWNER, old_map), self.tg.pinned())
        self.assertEqual(self.t("map.moved_group"), self.tg.texts()[(OWNER, old_map)])
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual([Dest(GROUP), Dest(OWNER)], [self.where(p) for p in self.photos()])
        self.assertFalse(self.photos()[-1]["disable_notification"])
        # Неподписанный FRIEND личной копии не получает — только группа.
        self.assertNotIn(FRIEND, [p["chat_id"] for p in self.photos()])


if __name__ == "__main__":
    unittest.main()
