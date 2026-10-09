#!/usr/bin/env python3
"""Карта камер, карточка камеры, /mode, /cam, /invite (0.3.0) — во всех пресетах.

Карта заменяет «Пульт»: одно закреплённое сообщение в каждом своём месте (тема
пульта в форуме, сама группа в плоском режиме, личка каждого допущенного без
группы), камеры секциями по локациям. Нажатие на камеру правит это же
сообщение в карточку, «◀ К карте» — обратно («один экран»). Карточка — та же
панель, что закреплена в теме камеры; `/cam <имя>` показывает её без карты.
`/mode` меняет пресет маршрутов, `/invite` добавляет людей без группы.

Настоящие CctvBot, State и Bridge; подменены только Bot API и HTTP моста.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import tempfile
import unittest

import httpx
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from cctv.bot.bot import CctvBot, HOME_VIEW_TTL_SEC, MAP_PAGE_LIMIT
from cctv.bot.bridge import Bridge, Camera
from cctv.bot.events import normalize_event
from cctv.bot.routes import Dest
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER, STRANGER, FakeTelegram

GROUP = -100500
FRIEND = 8
GUEST = 9
OTHER_CHAT = -100777
PHOTO = b"jpeg-bytes"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
CAMERAS = [
    {"camera_id": "gate", "title": "Калитка", "site": "Дача"},
    {"camera_id": "porch", "title": "Крыльцо", "site": "Дача"},
    {"camera_id": "garage", "title": "Гараж", "site": "Гараж"},
]


class ScreenTelegram(FakeTelegram):
    """Bot API со знанием бота о себе и о группе (мастер, /invite, /mode)."""

    def __init__(self, *, forum: bool = True) -> None:
        super().__init__()
        self.forum = forum
        self.edit_error: Exception | None = None  # ответ Telegram на правку текста

    async def edit_message_text(self, **kwargs):
        if self.edit_error is not None:
            self._record("edit_message_text_failed", kwargs)
            raise self.edit_error
        return await super().edit_message_text(**kwargs)

    async def get_me(self):
        return type("Me", (), {"id": 4242, "username": "dozor_test_bot"})()

    async def get_chat(self, **kwargs):
        return type("Chat", (), {"is_forum": self.forum})()

    async def get_chat_member(self, **kwargs):
        return type("Member", (), {"status": "administrator"})()

    async def edit_message_media(self, **kwargs):
        return await super().edit_message_media(**kwargs)


class Harness:
    """Общая обвязка: бот, мост на трёх камерах, помощники нажатий и экранов."""

    PRESET = "camera"
    GROUP_BOUND = True

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
        self.state_calls: list[dict] = []
        self.status = {c["camera_id"]: "online" for c in CAMERAS}

        def handler(request: httpx.Request):
            path = request.url.path
            if path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    dict(c, status=self.status[c["camera_id"]], last_frame_at="2026-10-09T10:00:00Z",
                         motion={"state": "watching", "reason": "", "last_motion_at": None})
                    for c in CAMERAS]})
            if path.endswith("/state"):
                body = json.loads(request.content)
                self.state_calls.append(body)
                camera_id = path.split("/")[-2]
                self.status[camera_id] = {"pause": "paused", "resume": "online"}.get(
                    body["action"], self.status[camera_id])
                return httpx.Response(200, json={"ok": True})
            if path == "/v1/media-requests":
                body = json.loads(request.content)
                self.media_requests.append(body)
                return httpx.Response(202, json={"request_id": body["request_id"], "status": "accepted"})
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = ScreenTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)
        self.bot.routes.set_preset(self.PRESET)

    async def register(self) -> None:
        for camera in CAMERAS:
            await self.bot.on_event(normalize_event({
                "event_id": f"reg-{camera['camera_id']}", "type": "camera.registered",
                "occurred_at": "2026-10-09T10:00:00Z", **camera}))
        await self.bot.sync_registry()

    def motion(self, event_id: str, camera_id: str = "gate"):
        return normalize_event({
            "event_id": event_id, "type": "motion.detected", "camera_id": camera_id,
            "occurred_at": "2026-10-09T10:01:00Z", "captured_at": "2026-10-09T10:01:00Z",
            "source": "recorded_main_person_detector",
            "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})

    def place(self) -> Dest:
        return self.bot.routes.map_places()[0]

    def home(self, place: Dest | None = None):
        place = place or self.place()
        return self.state.home_screen(place.chat_id, place.thread_id)

    def message(self, chat_id: int, message_id: int) -> dict:
        """Последний текст и кнопки сообщения: отправка или его правка."""
        found = None
        for number, (name, kwargs) in enumerate(self.tg.calls, start=1):
            if name == "send_message" and kwargs["chat_id"] == chat_id and number == message_id:
                found = kwargs  # id сообщения у FakeTelegram — номер вызова
            if name == "edit_message_text" and kwargs["chat_id"] == chat_id \
                    and kwargs["message_id"] == message_id:
                found = kwargs
        assert found is not None, (chat_id, message_id)
        return found

    @staticmethod
    def buttons(message: dict) -> list[tuple[str, str]]:
        markup = message.get("reply_markup")
        if markup is None:
            return []
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    def button(self, message: dict, label: str) -> str:
        return next(data for text, data in self.buttons(message) if label in text)

    async def press(self, place: Dest, message_id: int, label: str, user: int = OWNER) -> str:
        data = self.button(self.message(place.chat_id, message_id), label)
        return await self.bot.on_callback(user, place.thread_id, data, chat_id=place.chat_id,
                                          message_id=message_id)


class MapCardContract(Harness):
    """Сценарии карты и карточки, общие для всех пресетов."""

    async def test_map_is_one_pinned_message_per_place(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        await self.bot.refresh_console()  # повтор — правка, а не новое сообщение
        places = self.bot.routes.map_places()
        self.assertEqual(self.MAP_PLACES, len(places))
        for place in places:
            screen = self.home(place)
            self.assertIsNotNone(screen, place)
            self.assertIn(screen.message_id, [p["message_id"] for p in self.tg.of("pin_chat_message")
                                              if p["chat_id"] == place.chat_id])
            maps = [c for c in self.tg.of("send_message") if c["chat_id"] == place.chat_id
                    and c.get("message_thread_id") == place.thread_id
                    and c["text"].startswith("📍 Камер:")]
            self.assertEqual(1, len(maps), place)

    async def test_telegram_outage_keeps_the_map(self) -> None:
        """Bad Gateway, таймаут или flood wait на правке — не «карту удалили»:
        новая карта не заводится и не закрепляется, правка повторяется позже."""
        await self.register()
        await self.bot.refresh_console()
        homes = {place: self.home(place).message_id for place in self.bot.routes.map_places()}
        sent, pins = len(self.tg.of("send_message")), len(self.tg.of("pin_chat_message"))
        for error in (NetworkError("Bad Gateway"), TimedOut(), RetryAfter(dt.timedelta(seconds=5))):
            self.tg.edit_error = error
            await self.bot.refresh_console(force=True)
            await self.bot.refresh_all_panels(force=True)
        self.assertTrue(self.tg.of("edit_message_text_failed"))
        self.assertEqual(sent, len(self.tg.of("send_message")))
        self.assertEqual(pins, len(self.tg.of("pin_chat_message")))
        self.tg.edit_error = None
        await self.bot.refresh_console(force=True)
        for place, message_id in homes.items():
            self.assertEqual(message_id, self.home(place).message_id, place)
            self.assertIn(message_id, [e["message_id"] for e in self.tg.of("edit_message_text")
                                       if e["chat_id"] == place.chat_id])

    async def test_deleted_map_is_posted_again(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place = self.place()
        old = self.home(place).message_id
        self.tg.edit_error = BadRequest("Message to edit not found")
        await self.bot.refresh_console(force=True)
        self.assertNotEqual(old, self.home(place).message_id)
        self.assertIn(self.home(place).message_id,
                      [p["message_id"] for p in self.tg.of("pin_chat_message")])

    async def test_map_has_sections_by_location_and_camera_buttons(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place = self.place()
        text = self.message(place.chat_id, self.home().message_id)["text"]
        self.assertTrue(text.startswith("📍 Камер: 3 · в сети: 3 · событий сегодня: 0"), text)
        self.assertLess(text.index("▪️ Дача"), text.index("Калитка"))
        self.assertLess(text.index("Крыльцо"), text.index("▪️ Камеры"))
        self.assertLess(text.index("▪️ Камеры"), text.index("Гараж"))
        self.assertIn("в работе", text)
        labels = [t for t, d in self.buttons(self.message(place.chat_id, self.home().message_id))
                  if d.startswith("cv:card:")]
        self.assertEqual(["🟢 Калитка", "🟢 Крыльцо", "🟢 Гараж"], labels)
        datas = [d for _, d in self.buttons(self.message(place.chat_id, self.home().message_id))]
        self.assertFalse(any(c["camera_id"] in d for d in datas for c in CAMERAS))
        for action in ("panel", "add", "model", "thr", "mode"):
            self.assertTrue(any(d.startswith(f"cv:{action}:") for d in datas), action)

    async def test_events_today_are_counted_on_the_map(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        await self.bot.on_event(self.motion("m1"))  # повтор моста — не событие
        await self.bot.on_event(self.motion("m2", "garage"))
        await self.bot.refresh_console()
        text = self.message(self.place().chat_id, self.home().message_id)["text"]
        self.assertIn("событий сегодня: 2", text.splitlines()[0])

    async def test_camera_button_turns_the_map_into_a_card_and_back(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place, home = self.place(), self.home().message_id
        sent = len(self.tg.of("send_message"))
        self.assertEqual("", await self.press(place, home, "Калитка"))
        card = self.message(place.chat_id, home)
        self.assertEqual(sent, len(self.tg.of("send_message")), "карточка — правкой, не новым сообщением")
        self.assertIn("Камера: Калитка", card["text"])
        self.assertIn("Локация: Дача", card["text"])
        self.assertIn("#калитка #дача", card["text"])
        labels = [t for t, _ in self.buttons(card)]
        for label in ("Кадр", "Клип", "Статус", "Пауза", "Имя", "Локация", "Настройка", "К карте"):
            self.assertTrue(any(label in t for t in labels), (label, labels))
        self.assertEqual("card:gate", self.home().view)
        await self.press(place, home, "К карте")
        self.assertEqual("map", self.home().view)
        self.assertTrue(self.message(place.chat_id, home)["text"].startswith("📍 Камер:"))
        self.assertEqual(sent, len(self.tg.of("send_message")))

    async def test_card_buttons_act_on_the_camera(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place, home = self.place(), self.home().message_id
        await self.press(place, home, "Калитка")
        self.assertIn(self.bot._t("media.snap_requested" if place.thread_id else "media.snap_requested_here"),
                      await self.press(place, home, "Кадр"))
        request = self.media_requests[-1]
        self.assertEqual(("gate", "snapshot"), (request["camera_id"], request["kind"]))
        await self.bot.on_event(normalize_event({
            "event_id": "ready-1", "type": "media.ready", "camera_id": "gate",
            "request_id": request["request_id"], "kind": "snapshot", "captured_at": "2026-10-09T10:02:00Z",
            "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}}))
        photo = self.tg.of("send_photo")[-1]
        # Кадр — туда, где карточка: на карте пульта, в плоском чате, в личке.
        self.assertEqual(place, Dest(photo["chat_id"], photo.get("message_thread_id")))
        # Пауза с карточки — карточка перерисовывается на месте: кнопка «Вернуть в работу».
        self.assertIn("паузе", await self.press(place, home, "Пауза"))
        self.assertEqual("pause", self.state_calls[-1]["action"])
        self.assertTrue(any("Вернуть в работу" in t for t, _ in self.buttons(self.message(place.chat_id, home))))

    async def test_open_card_stays_and_forgotten_card_returns_to_map(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place, home = self.place(), self.home().message_id
        await self.press(place, home, "Крыльцо")
        await self.bot.refresh_console()
        self.assertEqual("card:porch", self.home().view)
        self.clock[0] += HOME_VIEW_TTL_SEC + 1
        await self.bot.refresh_console()
        self.assertEqual("map", self.home().view)
        self.assertTrue(self.message(place.chat_id, home)["text"].startswith("📍 Камер:"))

    async def test_cam_command_shows_card_here(self) -> None:
        await self.register()
        place = self.place()
        sent = len(self.tg.of("send_message"))
        self.assertIsNone(await self.bot.on_cam(OWNER, ["калитка"], chat_id=place.chat_id,
                                                thread=place.thread_id))
        card = self.tg.of("send_message")[sent]
        self.assertEqual(place, Dest(card["chat_id"], card.get("message_thread_id")))
        self.assertIn("Камера: Калитка", card["text"])
        # Хэштег и camera_id тоже находят камеру; неоднозначное — список имён.
        self.assertIsNone(await self.bot.on_cam(OWNER, ["#гараж"], chat_id=place.chat_id,
                                                thread=place.thread_id))
        self.assertIn("Камера: Гараж", self.tg.of("send_message")[-1]["text"])
        answer = await self.bot.on_cam(OWNER, ["к"], chat_id=place.chat_id, thread=place.thread_id)
        self.assertIn("Не нашлась одна камера", answer)
        self.assertIn("Калитка", answer)
        self.assertEqual("Нет доступа.", await self.bot.on_cam(STRANGER, ["калитка"], chat_id=place.chat_id))

    async def test_cam_card_works_in_private_chat_of_allowed_person(self) -> None:
        await self.register()
        await self.bot.on_cam(FRIEND, ["калитка"], chat_id=FRIEND)
        card = self.tg.of("send_message")[-1]
        self.assertEqual(FRIEND, card["chat_id"])
        message_id = len(self.tg.calls)
        await self.press(Dest(FRIEND), message_id, "Кадр", user=FRIEND)
        request = self.media_requests[-1]
        await self.bot.on_event(normalize_event({
            "event_id": "ready-dm", "type": "media.ready", "camera_id": "gate",
            "request_id": request["request_id"], "kind": "snapshot", "captured_at": "2026-10-09T10:02:00Z",
            "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}}))
        self.assertEqual(FRIEND, self.tg.of("send_photo")[-1]["chat_id"], "кто попросил — тому и кадр")

    async def test_cam_in_a_foreign_place_is_refused(self) -> None:
        await self.register()
        other = self.foreign_place()
        answer = await self.bot.on_cam(OWNER, ["калитка"], chat_id=other.chat_id, thread=other.thread_id)
        self.assertEqual("Карточка этой камеры работает на карте, в её теме или в личке с ботом.", answer)

    def foreign_place(self) -> Dest:
        return Dest(OTHER_CHAT)

    async def test_location_from_card_moves_camera_into_a_section(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place, home = self.place(), self.home().message_id
        await self.press(place, home, "Гараж")
        asked = await self.press(place, home, "Локация")
        self.assertIn("Пришлите локацию камеры", asked)
        answer = await self.bot.on_text(OWNER, place.thread_id, "Дом", chat_id=place.chat_id)
        self.assertEqual("📍 Гараж: локация «Дом».", answer)
        self.assertEqual("Дом", self.bot.routes.location("garage"))
        await self.bot.refresh_console()
        self.clock[0] += HOME_VIEW_TTL_SEC + 1
        await self.bot.refresh_console()
        text = self.message(place.chat_id, home)["text"]
        self.assertLess(text.index("▪️ Дом"), text.index("Гараж"))
        self.assertNotIn("▪️ Камеры", text)
        # Снять тег — «-»: локацией снова становится площадка (здесь она — имя камеры).
        await self.bot.on_callback(OWNER, place.thread_id,
                                   f"cv:loc:{self.state.issue_callback('garage', 'loc', 600)}",
                                   chat_id=place.chat_id)
        self.assertEqual("📍 Гараж: локация снята.",
                         await self.bot.on_text(OWNER, place.thread_id, "-", chat_id=place.chat_id))
        self.assertEqual("", self.bot.routes.location("garage"))

    async def test_stranger_gets_nothing_from_map(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        place, home = self.place(), self.home().message_id
        before = len(self.tg.calls)
        self.assertEqual("Нет доступа.", await self.press(place, home, "Калитка", user=STRANGER))
        self.assertEqual(before, len(self.tg.calls))


class CameraTopicMapTest(MapCardContract, unittest.IsolatedAsyncioTestCase):
    PRESET = "camera"
    MAP_PLACES = 1

    def foreign_place(self) -> Dest:
        return self.bot.routes.route("porch")[0]

    async def test_cam_without_name_in_camera_topic_shows_its_card(self) -> None:
        await self.register()
        topic = self.bot.routes.route("garage")[0]
        await self.bot.on_cam(OWNER, [], chat_id=topic.chat_id, thread=topic.thread_id)
        self.assertIn("Камера: Гараж", self.tg.of("send_message")[-1]["text"])

    async def test_legacy_console_message_becomes_the_map(self) -> None:
        """Пульт до 0.3.0: закреплённое сообщение правится в карту, нового нет."""
        await self.register()
        console = await self.bot.ensure_console()
        self.state.set_service("console_panel", "7777")
        self.state.forget_screen(console.chat_id, self.home().message_id)
        sent = len(self.tg.of("send_message"))
        await self.bot.refresh_console()
        self.assertEqual(sent, len(self.tg.of("send_message")))
        self.assertEqual(7777, self.tg.of("edit_message_text")[-1]["message_id"])
        self.assertEqual(7777, self.home().message_id)

    async def test_topic_panel_has_no_back_button(self) -> None:
        await self.register()
        panel = self.tg.of("send_message")[1]
        self.assertIn("Камера: ", panel["text"])
        self.assertFalse(any("К карте" in t for t, _ in self.buttons(panel)))
        self.assertTrue(any("Локация" in t for t, _ in self.buttons(panel)))


class LocationTopicMapTest(MapCardContract, unittest.IsolatedAsyncioTestCase):
    PRESET = "location"
    MAP_PLACES = 1

    def foreign_place(self) -> Dest:
        return self.bot.routes.route("garage")[0]

    async def test_new_location_gets_its_topic(self) -> None:
        await self.register()
        await self.bot.on_callback(OWNER, None, f"cv:loc:{self.state.issue_callback('garage', 'loc', 600)}")
        await self.bot.on_text(OWNER, None, "Дом")
        self.assertIn("Дом", [c["name"] for c in self.tg.of("create_forum_topic")])
        topic = self.bot.routes.route("garage")[0]
        self.assertEqual(self.state.location_topic("Дом"), topic.thread_id)


class FlatGroupMapTest(MapCardContract, unittest.IsolatedAsyncioTestCase):
    PRESET = "flat"
    MAP_PLACES = 1

    async def test_map_is_in_the_group_itself(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        self.assertEqual(Dest(GROUP), self.place())
        self.assertEqual([], self.tg.of("create_forum_topic"))


class FlatPrivateMapTest(MapCardContract, unittest.IsolatedAsyncioTestCase):
    """Без группы: у каждого допущенного своя карта, своя открытая карточка и свой звук."""
    PRESET = "flat"
    GROUP_BOUND = False
    MAP_PLACES = 2

    async def test_each_person_has_own_map_and_own_open_card(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        self.assertEqual([Dest(OWNER), Dest(FRIEND)], self.bot.routes.map_places())
        mine, theirs = self.home(Dest(OWNER)).message_id, self.home(Dest(FRIEND)).message_id
        await self.press(Dest(OWNER), mine, "Калитка")
        self.assertEqual("card:gate", self.home(Dest(OWNER)).view)
        self.assertEqual("map", self.home(Dest(FRIEND)).view)
        self.assertTrue(self.message(FRIEND, theirs)["text"].startswith("📍 Камер:"))

    async def test_quiet_mark_is_per_person(self) -> None:
        await self.register()
        self.state.toggle_motion(FRIEND, "gate")
        await self.bot.refresh_console()
        line = lambda user: next(l for l in self.message(user, self.home(Dest(user)).message_id)["text"]
                                 .splitlines() if "Калитка" in l)
        self.assertIn("🔕", line(OWNER))
        self.assertNotIn("🔕", line(FRIEND))
        # В карточке — уведомления этого человека.
        await self.press(Dest(FRIEND), self.home(Dest(FRIEND)).message_id, "Калитка", user=FRIEND)
        self.assertIn("🔔 включены", self.message(FRIEND, self.home(Dest(FRIEND)).message_id)["text"])


class ModeTest(Harness, unittest.IsolatedAsyncioTestCase):
    """/mode: пресет меняется без потери камер; карта переезжает, темы не плодятся."""
    PRESET = "camera"

    async def test_menu_shows_presets_and_current(self) -> None:
        await self.register()
        console = await self.bot.ensure_console()
        self.assertIsNone(await self.bot.on_mode(OWNER, [], chat_id=GROUP, thread=console.thread_id))
        menu = self.tg.of("send_message")[-1]
        self.assertIn("Сейчас: Тема на камеру", menu["text"])
        self.assertEqual(["✅ Тема на камеру", "Тема на локацию", "Один чат без тем"],
                         [t for t, _ in self.buttons(menu)])
        self.assertEqual("Это может только владелец установки.",
                         await self.bot.on_mode(FRIEND, ["flat"], chat_id=GROUP))
        self.assertEqual("Нет доступа.", await self.bot.on_mode(STRANGER, [], chat_id=GROUP))

    async def test_switch_to_flat_and_back_keeps_cameras_and_topics(self) -> None:
        await self.register()
        await self.bot.refresh_console()
        console = await self.bot.ensure_console()
        old_map = self.home(console).message_id
        topics = len(self.tg.of("create_forum_topic"))
        gate_topic = self.bot.routes.route("gate")[0]
        menu_press = await self.bot.on_mode(OWNER, [], chat_id=GROUP, thread=console.thread_id)
        self.assertIsNone(menu_press)
        menu = self.tg.of("send_message")[-1]
        answer = await self.bot.on_callback(OWNER, console.thread_id, self.button(menu, "Один чат"),
                                            chat_id=GROUP)
        self.assertIn("✅ Режим: Один чат без тем", answer)
        self.assertEqual("flat", self.bot.routes.preset)
        # Прежняя карта в теме пульта говорит, куда делась; новая — в самой группе.
        moved = [e["message_id"] for e in self.tg.of("edit_message_text") if "переехала" in e["text"]]
        self.assertEqual([old_map], moved)
        self.assertIsNotNone(self.home(Dest(GROUP)))
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual(Dest(GROUP), Dest(self.tg.of("send_photo")[-1]["chat_id"],
                                           self.tg.of("send_photo")[-1].get("message_thread_id")))
        # Панель в старой теме камеры остаётся рабочей: тема — место камеры.
        token = self.state.issue_callback("gate", "snap", 600)
        self.assertIn("Запрос отправлен, кадр придёт", await self.bot.on_callback(
            OWNER, gate_topic.thread_id, f"cv:snap:{token}", chat_id=GROUP))
        # Назад: темы камер те же, новых не заводится; карта снова в пульте (тот же закреп).
        self.assertIn("Тема на камеру", await self.bot.on_mode(OWNER, ["camera"], chat_id=GROUP))
        self.assertEqual(topics, len(self.tg.of("create_forum_topic")))
        self.assertEqual([gate_topic], self.bot.routes.route("gate"))
        self.assertEqual(old_map, self.home(console).message_id)

    async def test_location_without_locations_hints_and_topics_need_a_group(self) -> None:
        await self.register()
        answer = await self.bot.on_mode(OWNER, ["location"], chat_id=GROUP)
        self.assertIn("Режим: Тема на локацию", answer)
        # Площадка «Дача» из реестра — уже локация: подсказка о тегах не нужна.
        self.assertNotIn("Задайте её кнопкой", answer)
        self.assertEqual(self.bot.routes.route("gate"), self.bot.routes.route("porch"))
        self.assertIn("Режим уже", await self.bot.on_mode(OWNER, ["location"], chat_id=GROUP))
        self.assertIn("Неизвестный режим", await self.bot.on_mode(OWNER, ["topics"], chat_id=GROUP))

    async def test_topics_need_a_forum_group(self) -> None:
        self.bot.routes.set_preset("flat")
        await self.register()
        self.tg.forum = False
        answer = await self.bot.on_mode(OWNER, ["camera"], chat_id=GROUP)
        self.assertIn(self.bot._t("wizard.need_forum"), answer)
        self.assertEqual("flat", self.bot.routes.preset)


class NoGroupModeTest(Harness, unittest.IsolatedAsyncioTestCase):
    PRESET = "flat"
    GROUP_BOUND = False

    async def test_topic_presets_need_a_group(self) -> None:
        answer = await self.bot.on_mode(OWNER, ["camera"], chat_id=OWNER)
        self.assertIn("Группа ещё не привязана", answer)
        self.assertEqual("flat", self.bot.routes.preset)


class InviteTest(Harness, unittest.IsolatedAsyncioTestCase):
    """/invite: несколько людей без группы — у каждого своя лента, карта и звук."""
    PRESET = "flat"
    GROUP_BOUND = False

    async def invite(self) -> str:
        self.assertIsNone(await self.bot.on_invite(OWNER, chat_id=OWNER))
        text = self.tg.of("send_message")[-1]["text"]
        link = next(word for word in text.split() if word.startswith("https://t.me/"))
        self.assertTrue(link.startswith("https://t.me/dozor_test_bot?start=inv"), link)
        return link.split("start=", 1)[1]

    async def test_invite_link_admits_one_person(self) -> None:
        await self.register()
        start = await self.invite()
        self.assertFalse(self.bot.allowed(GUEST))
        answer = await self.bot.on_start(GUEST, GUEST, "private", [start], name="Гость")
        self.assertIn("События камер будут приходить сюда", answer)
        self.assertTrue(self.bot.allowed(GUEST))
        self.assertIn(GUEST, self.bot.routes.map_places() and [d.chat_id for d in self.bot.routes.map_places()])
        self.assertIsNotNone(self.home(Dest(GUEST)), "своя карта в личке нового человека")
        self.assertIn("По приглашению вошли: Гость", " ".join(c["text"] for c in self.tg.of("send_message")
                                                              if c["chat_id"] == OWNER))
        # Ссылка одноразовая.
        self.assertEqual("Приглашение устарело или уже использовано. Попросите новое.",
                         await self.bot.on_start(STRANGER, STRANGER, "private", [start]))
        self.assertFalse(self.bot.allowed(STRANGER))
        # События — и гостю, со своим звуком.
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual({OWNER, FRIEND, GUEST}, {c["chat_id"] for c in self.tg.of("send_photo")})

    async def test_expired_invite_is_refused(self) -> None:
        start = await self.invite()
        self.clock[0] += 86400 + 1
        self.assertIn("устарело", await self.bot.on_start(GUEST, GUEST, "private", [start]))
        self.assertFalse(self.bot.allowed(GUEST))

    async def test_owner_removes_a_member(self) -> None:
        await self.register()
        start = await self.invite()
        await self.bot.on_start(GUEST, GUEST, "private", [start], name="Гость")
        await self.invite()
        listing = self.tg.of("send_message")[-1]
        self.assertIn("Приглашены:", listing["text"])
        kick = self.button(listing, "Убрать Гость")
        self.assertEqual("Это может только владелец установки.",
                         await self.bot.on_callback(FRIEND, None, kick, chat_id=FRIEND))
        self.assertEqual("Доступ снят.", await self.bot.on_callback(OWNER, None, kick, chat_id=OWNER))
        self.assertFalse(self.bot.allowed(GUEST))
        before = len(self.tg.of("send_photo"))
        await self.bot.on_event(self.motion("m2"))
        self.assertNotIn(GUEST, [c["chat_id"] for c in self.tg.of("send_photo")[before:]])

    async def test_invite_only_by_owner_and_only_in_private(self) -> None:
        self.assertEqual("Это может только владелец установки.", await self.bot.on_invite(FRIEND, chat_id=FRIEND))
        self.assertIn("в личку", await self.bot.on_invite(OWNER, chat_id=GROUP, chat_type="supergroup"))
        self.assertEqual("Нет доступа.", await self.bot.on_invite(STRANGER, chat_id=STRANGER))


class InviteForumTest(Harness, unittest.IsolatedAsyncioTestCase):
    PRESET = "camera"

    async def test_member_in_forum_mode_is_told_about_the_group(self) -> None:
        self.assertIsNone(await self.bot.on_invite(OWNER, chat_id=OWNER))
        start = self.tg.of("send_message")[-1]["text"].split("start=", 1)[1].split()[0]
        answer = await self.bot.on_start(GUEST, GUEST, "private", [start], name="Гость")
        self.assertIn("попросите владельца добавить вас", answer)
        self.assertTrue(self.bot.allowed(GUEST))


class MapPagesTest(Harness, unittest.TestCase):
    """Больше MAP_PAGE_LIMIT камер в нескольких локациях — кнопки локаций страницами."""

    def test_many_cameras_page_by_location(self) -> None:
        cameras = []
        for index in range(MAP_PAGE_LIMIT + 2):
            camera_id = f"cam{index}"
            self.state.note_camera(camera_id, f"Камера {index}", "")
            self.state.set_camera_location(camera_id, "Дом" if index % 2 else "Дача")
            cameras.append(Camera(camera_id, f"Камера {index}", "", "online", None))
        root = self.bot.map_markup(cameras)
        labels = [b.text for row in root.inline_keyboard for b in row]
        self.assertIn("Дача (7) ▸", labels)
        self.assertIn("Дом (7) ▸", labels)
        self.assertFalse(any(b.callback_data.startswith("cv:card:") for row in root.inline_keyboard for b in row))
        page = next(b.callback_data for row in root.inline_keyboard for b in row if b.text.startswith("Дом"))
        self.assertEqual(("console", "map", "@Дом"), self.state.resolve_callback(page.split(":")[2]))
        inner = self.bot.map_markup(cameras, "Дом")
        cards = [b.text for row in inner.inline_keyboard for b in row if b.callback_data.startswith("cv:card:")]
        self.assertEqual(7, len(cards))
        self.assertTrue(any(b.text == "◀ К карте" for row in inner.inline_keyboard for b in row))

    def test_few_cameras_show_all_buttons(self) -> None:
        cameras = [Camera("a", "А", "", "online", None), Camera("b", "Б", "", "paused", None)]
        labels = [b.text for row in self.bot.map_markup(cameras).inline_keyboard for b in row]
        self.assertIn("🟢 А", labels)
        self.assertIn("⏸ Б", labels)


class FindCameraTest(Harness, unittest.TestCase):
    def test_lookup(self) -> None:
        for camera in CAMERAS:
            self.state.note_camera(camera["camera_id"], camera["title"], camera["site"])
        self.assertEqual("gate", self.bot.find_camera("Калитка"))
        self.assertEqual("gate", self.bot.find_camera("кал"))
        self.assertEqual("gate", self.bot.find_camera("gate"))
        self.assertEqual("garage", self.bot.find_camera("#гараж"))
        self.assertIsNone(self.bot.find_camera("к"))  # Калитка и Крыльцо
        self.assertIsNone(self.bot.find_camera("дом"))
        self.assertIsNone(self.bot.find_camera(""))


if __name__ == "__main__":
    unittest.main()
