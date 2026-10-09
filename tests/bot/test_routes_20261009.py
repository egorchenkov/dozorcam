#!/usr/bin/env python3
"""Маршруты доставки (0.3.0): один контракт бота — во всех пресетах.

Контракт тот же, что у форума до 0.3.0 (test_cctv_flow): событие доезжает ровно
один раз и туда, куда ведёт маршрут камеры; кнопка под ним работает там же и
отказывает в чужом месте; кадр по reply и заказанное медиа возвращаются тому,
кто просил; посторонний не получает ничего. Сверх того — хэштеги в подписи и
склейка событий за 60 с, в том числе в режиме с темами.

Пресеты: «тема на камеру» (форум, как в проде), «тема на локацию» (форум),
«плоско» в обычной группе и «плоско» без группы — в личку каждому допущенному.
Настоящие CctvBot, State и Bridge; подменены только Bot API и HTTP моста.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest

import httpx

from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.events import normalize_event
from cctv.bot.routes import Dest, hashtag
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER, STRANGER, FakeTelegram

GROUP = -100500
FRIEND = 8
OTHER_CHAT = -100777
PHOTO = b"jpeg-bytes"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()
# Две камеры одной локации (площадка «Дача») и одна без локации: её площадка
# равна имени — так мастер заводит камеры, и локацией это не считается.
CAMERAS = [
    {"camera_id": "gate", "title": "Калитка", "site": "Дача"},
    {"camera_id": "porch", "title": "Крыльцо", "site": "Дача"},
    {"camera_id": "garage", "title": "Гараж", "site": "Гараж"},
]


class FlakyTelegram(FakeTelegram):
    """Telegram, у которого правка поста может не пройти (пост удалили руками)."""

    def __init__(self) -> None:
        super().__init__()
        self.edit_fails = False

    async def edit_message_media(self, **kwargs):
        if self.edit_fails:
            self._record("edit_message_media_failed", kwargs)
            raise RuntimeError("message to edit not found")
        return await super().edit_message_media(**kwargs)


class RouteContract:
    """Сценарии, общие для всех пресетов. Подкласс задаёт PRESET и ожидания места."""

    PRESET = "camera"
    GROUP_BOUND = True

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        cfg = make_config(self.tmp)
        if not self.GROUP_BOUND:
            cfg = dataclasses.replace(cfg, chat_id=None, allowed_user_ids=frozenset({OWNER, FRIEND}))
        self.cfg = cfg
        self.clock = [1_800_000_000.0]
        self.state = State(":memory:", now=lambda: self.clock[0])
        self.addCleanup(self.state.close)
        self.media_requests: list[dict] = []
        self.status = {c["camera_id"]: "online" for c in CAMERAS}

        def handler(request: httpx.Request):
            if request.url.path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    dict(c, status=self.status[c["camera_id"]], last_frame_at="2026-10-09T10:00:00Z",
                         motion={"state": "watching", "reason": "", "last_motion_at": None})
                    for c in CAMERAS]})
            if request.url.path == "/v1/media-requests":
                body = json.loads(request.content)
                self.media_requests.append(body)
                return httpx.Response(202, json={"request_id": body["request_id"], "status": "accepted"})
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = FlakyTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)
        self.bot.routes.set_preset(self.PRESET)

    # --- помощники -----------------------------------------------------------
    async def register(self, *camera_ids: str) -> None:
        for camera in CAMERAS:
            if not camera_ids or camera["camera_id"] in camera_ids:
                await self.bot.on_event(normalize_event({
                    "event_id": f"reg-{camera['camera_id']}", "type": "camera.registered",
                    "occurred_at": "2026-10-09T10:00:00Z", **camera}))

    def motion(self, event_id: str, camera_id: str = "gate", *, person: bool = True,
               at: str = "2026-10-09T10:01:00Z"):
        payload = {"event_id": event_id, "type": "motion.detected", "camera_id": camera_id,
                   "occurred_at": at, "captured_at": at,
                   "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                                "bytes": len(PHOTO)}}
        if person:
            payload["source"] = "recorded_main_person_detector"
        return normalize_event(payload)

    def ready(self, request_id: str, camera_id: str = "gate"):
        return normalize_event({"event_id": f"ready-{request_id}", "type": "media.ready",
                                "camera_id": camera_id, "request_id": request_id, "kind": "snapshot",
                                "captured_at": "2026-10-09T10:02:00Z",
                                "download": {"url": f"{BRIDGE}/v1/media/opaque",
                                             "sha256": PHOTO_SHA, "bytes": len(PHOTO)}})

    def where(self, call: dict) -> Dest:
        return Dest(call["chat_id"], call.get("message_thread_id"))

    def photos(self) -> list[dict]:
        return self.tg.of("send_photo")

    def expected_dests(self, camera_id: str) -> list[Dest]:
        return self.bot.routes.route(camera_id)

    # --- контракт ------------------------------------------------------------
    async def test_registration_builds_route_once(self) -> None:
        await self.register()
        await self.register()  # повтор event_id — ничего
        await self.bot.sync_registry()  # сверка реестра — тоже ничего нового
        for camera in CAMERAS:
            self.assertTrue(self.expected_dests(camera["camera_id"]), camera["camera_id"])
        self.assertEqual(self.TOPICS_CREATED,
                         sorted(c["name"] for c in self.tg.of("create_forum_topic")))

    async def test_event_goes_once_to_every_route_place(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        await self.bot.on_event(self.motion("m1"))  # повтор моста
        places = [self.where(c) for c in self.photos()]
        self.assertEqual(self.expected_dests("gate"), places)
        self.assertEqual(self.PLACES, len(places))

    async def test_caption_names_camera_and_carries_hashtags(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        caption = self.photos()[0]["caption"]
        self.assertIn("#калитка #дача", caption)
        first = caption.splitlines()[0]
        if self.PRESET == "camera":
            # Своя тема — имя даёт тема; первая строка как до 0.3.0.
            self.assertTrue(first.startswith("Обнаружен человек: "), first)
        else:
            self.assertTrue(first.startswith("Калитка · Обнаружен человек: "), first)

    async def test_camera_without_location_has_only_own_hashtag(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1", "garage"))
        caption = self.photos()[0]["caption"]
        self.assertEqual("#гараж", caption.splitlines()[-1])

    async def test_frame_button_and_reply_return_to_asker(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        photo = self.photos()[-1]
        place = self.where(photo)
        data = photo["reply_markup"].inline_keyboard[0][0].callback_data
        await self.bot.on_callback(OWNER, place.thread_id, data, chat_id=place.chat_id)
        self.assertEqual("clip", self.media_requests[-1]["kind"])
        self.assertEqual("2026-10-09T10:01:00Z", self.media_requests[-1]["center_at"])
        # Текстовый reply на кадр — тот же клип вокруг кадра (id фото у FakeTelegram — номер вызова).
        frame_id = [i + 1 for i, (name, _) in enumerate(self.tg.calls) if name == "send_photo"][-1]
        await self.bot.on_text(OWNER, place.thread_id, "клип", frame_id, 900, chat_id=place.chat_id)
        self.assertEqual(["clip", "clip"], [r["kind"] for r in self.media_requests])

    async def test_requested_snapshot_comes_back_to_the_same_place(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        place = self.where(self.photos()[-1])
        data = self.photos()[-1]["reply_markup"].inline_keyboard[0][0].callback_data
        token = self.state.issue_callback("gate", "snap", 600)
        await self.bot.on_callback(OWNER, place.thread_id, f"cv:snap:{token}", chat_id=place.chat_id)
        request = self.media_requests[-1]
        self.assertEqual("snapshot", request["kind"])
        before = len(self.photos())
        await self.bot.on_event(self.ready(request["request_id"]))
        self.assertEqual(before + 1, len(self.photos()))
        self.assertEqual(place, self.where(self.photos()[-1]))
        self.assertTrue(data.startswith("cv:clip:"))

    async def test_stranger_and_foreign_place_are_refused(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        place = self.where(self.photos()[-1])
        data = self.photos()[-1]["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual("Нет доступа.",
                         await self.bot.on_callback(STRANGER, place.thread_id, data, chat_id=place.chat_id))
        # Чужое место: тема другой камеры (форум) или посторонний чат (плоский режим).
        other = self.foreign_place()
        self.assertEqual("Кнопка не относится к этой теме.",
                         await self.bot.on_callback(OWNER, other.thread_id, data, chat_id=other.chat_id))
        self.assertEqual([], self.media_requests)

    def foreign_place(self) -> Dest:
        return Dest(OTHER_CHAT)

    async def test_buttons_carry_no_camera_id(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        datas = [b.callback_data for call in self.photos() + self.tg.of("send_message")
                 if call.get("reply_markup") is not None and hasattr(call["reply_markup"], "inline_keyboard")
                 for row in call["reply_markup"].inline_keyboard for b in row]
        self.assertTrue(datas)
        self.assertTrue(all(d.startswith("cv:") for d in datas))
        self.assertFalse(any(c["camera_id"] in d for d in datas for c in CAMERAS))

    async def test_motion_for_unknown_camera_is_ignored(self) -> None:
        await self.register("gate")
        await self.bot.on_event(self.motion("m1", "ghost"))  # нет ни у бота, ни в реестре
        self.assertEqual([], self.photos())

    async def test_first_motion_of_new_camera_is_delivered(self) -> None:
        """Движение новой камеры обогнало сверку реестра (стенд 09.10.2026) — доставить."""
        await self.register("gate")
        await self.bot.on_event(self.motion("m1", "porch"))
        self.assertEqual(self.PLACES, len(self.photos()))

    async def test_retired_camera_is_told_once(self) -> None:
        await self.register()
        self.status["gate"] = "retired"
        await self.bot.sync_registry()
        await self.bot.sync_registry()  # сверка повторяется — сообщение нет
        told = [c for c in self.tg.of("send_message") if "снята с эксплуатации" in c["text"]]
        self.assertEqual(self.PLACES, len(told))

    async def test_events_within_a_minute_are_merged_into_one_post(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1", person=False, at="2026-10-09T10:01:00Z"))
        self.clock[0] += 20
        await self.bot.on_event(self.motion("m2", person=True, at="2026-10-09T10:01:20Z"))
        self.clock[0] += 20
        await self.bot.on_event(self.motion("m3", person=False, at="2026-10-09T10:01:40Z"))
        self.assertEqual(self.PLACES, len(self.photos()))
        edits = self.tg.of("edit_message_media")
        self.assertEqual(2 * self.PLACES, len(edits))
        last = edits[-1]
        caption = last["media"].caption
        lines = caption.splitlines()
        # Начало окна — время первого события; человек, найденный внутри окна, не теряется.
        self.assertIn("Обнаружен человек: ", lines[0])
        self.assertIn("13:01:00", lines[0])
        self.assertEqual("+2 за минуту · последнее 13:01:40", lines[1])
        self.assertIn("#калитка", lines[-1])
        # Кнопка «клип» склеенного поста — вокруг свежего кадра.
        data = last["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual("2026-10-09T10:01:40Z", self.state.resolve_callback(data.split(":")[2])[2])
        posted = self.photos()[-1]
        self.assertEqual(self.where(posted).chat_id, last["chat_id"])

    async def test_merge_window_counts_from_first_post(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        self.clock[0] += 59
        await self.bot.on_event(self.motion("m2", at="2026-10-09T10:01:59Z"))
        self.clock[0] += 2  # 61 с от первого поста — окно закрыто, хоть прошлое событие и свежее
        await self.bot.on_event(self.motion("m3", at="2026-10-09T10:02:01Z"))
        self.assertEqual(2 * self.PLACES, len(self.photos()))
        self.assertEqual(self.PLACES, len(self.tg.of("edit_message_media")))

    async def test_other_camera_is_never_merged(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1", "gate"))
        await self.bot.on_event(self.motion("m2", "porch"))
        self.assertEqual([], self.tg.of("edit_message_media"))
        self.assertEqual(len(self.expected_dests("gate")) + len(self.expected_dests("porch")),
                         len(self.photos()))

    async def test_failed_merge_falls_back_to_a_new_post(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        self.tg.edit_fails = True
        await self.bot.on_event(self.motion("m2"))
        self.assertEqual(2 * self.PLACES, len(self.photos()))
        self.tg.edit_fails = False
        await self.bot.on_event(self.motion("m3"))  # окно — уже от нового поста
        self.assertEqual(self.PLACES, len(self.tg.of("edit_message_media")))

    async def test_merge_can_be_switched_off(self) -> None:
        self.bot.merge_sec = 0
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        await self.bot.on_event(self.motion("m2"))
        self.assertEqual(2 * self.PLACES, len(self.photos()))
        self.assertEqual([], self.tg.of("edit_message_media"))

    async def test_health_transition_is_told_on_the_route(self) -> None:
        await self.register()
        await self.bot.watch_health()
        self.status["gate"] = "offline"
        await self.bot.watch_health()
        told = [c for c in self.tg.of("send_message") if "нет кадров" in c["text"].lower()
                or "Калитка" in c["text"] and "🔴" in c["text"]]
        self.assertEqual(self.expected_dests("gate"), [self.where(c) for c in told])

    async def test_retired_camera_gets_no_more_events(self) -> None:
        await self.register()
        await self.bot.on_event(normalize_event({"event_id": "ret", "type": "camera.retired",
                                                 "camera_id": "gate", "occurred_at": "2026-10-09T10:05:00Z"}))
        closed = self.tg.of("close_forum_topic")
        self.assertEqual(1 if self.PRESET == "camera" else 0, len(closed))
        await self.bot.on_event(self.motion("m9"))
        self.assertEqual([], self.photos())
        self.assertEqual([], self.bot.routes.route("gate"))
        # Соседняя камера той же локации продолжает жить.
        await self.bot.on_event(self.motion("m10", "porch"))
        self.assertEqual(self.PLACES, len(self.photos()))


class CameraTopicPresetTest(RouteContract, unittest.IsolatedAsyncioTestCase):
    """«Тема на камеру» — как в проде до 0.3.0: тема и паспорт на каждую камеру."""
    PRESET = "camera"
    PLACES = 1
    TOPICS_CREATED = ["Гараж", "Калитка", "Крыльцо", "Пульт"]

    def foreign_place(self) -> Dest:
        return self.bot.routes.route("porch")[0]  # тема соседней камеры

    async def test_each_camera_has_its_own_topic(self) -> None:
        await self.register()
        threads = {d.thread_id for c in CAMERAS for d in self.bot.routes.route(c["camera_id"])}
        self.assertEqual(3, len(threads))


class LocationTopicPresetTest(RouteContract, unittest.IsolatedAsyncioTestCase):
    """«Тема на локацию» — камеры одной локации в одной теме, без локации — «Камеры»."""
    PRESET = "location"
    PLACES = 1
    TOPICS_CREATED = ["Дача", "Камеры", "Пульт"]

    def foreign_place(self) -> Dest:
        return self.bot.routes.route("garage")[0]  # тема другой локации

    async def test_cameras_of_one_location_share_a_topic(self) -> None:
        await self.register()
        self.assertEqual(self.bot.routes.route("gate"), self.bot.routes.route("porch"))
        self.assertNotEqual(self.bot.routes.route("gate"), self.bot.routes.route("garage"))
        # Панель камеры не заводится и не закрепляется в общей теме (её место —
        # карточка по запросу); после сверки закреплён только пульт.
        self.assertEqual([], self.tg.of("pin_chat_message"))
        await self.bot.sync_registry()
        pins = self.tg.of("pin_chat_message")
        self.assertEqual([int(self.state.get_service("console_panel"))], [p["message_id"] for p in pins])
        self.assertIsNone(self.state.panel_for("gate"))

    async def test_location_tag_overrides_registry_site(self) -> None:
        await self.register()
        self.state.set_camera_location("garage", "Дача")
        self.assertEqual(self.bot.routes.route("gate"), self.bot.routes.route("garage"))
        await self.bot.on_event(self.motion("m1", "garage"))
        self.assertEqual("#гараж #дача", self.photos()[0]["caption"].splitlines()[-1])


class FlatGroupPresetTest(RouteContract, unittest.IsolatedAsyncioTestCase):
    """«Плоско» в группе: одна лента, тем нет, имя камеры и хэштеги в подписи."""
    PRESET = "flat"
    PLACES = 1
    TOPICS_CREATED: list[str] = []

    async def test_everything_lands_in_the_group_itself(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual(Dest(GROUP), self.where(self.photos()[0]))
        self.assertEqual([], self.tg.of("create_forum_topic"))
        self.assertTrue(all(c.get("message_thread_id") is None for c in self.tg.of("send_message")))

    async def test_press_in_reply_thread_of_plain_group_still_works(self) -> None:
        """У обычной супергруппы reply-цепочка тоже даёт message_thread_id — это не тема."""
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        data = self.photos()[0]["reply_markup"].inline_keyboard[0][0].callback_data
        await self.bot.on_callback(OWNER, 4242, data, chat_id=GROUP)
        self.assertEqual(1, len(self.media_requests))


class FlatPrivatePresetTest(RouteContract, unittest.IsolatedAsyncioTestCase):
    """«Плоско» без группы: личка каждому допущенному, звук — по своей подписке."""
    PRESET = "flat"
    GROUP_BOUND = False
    PLACES = 2
    TOPICS_CREATED: list[str] = []

    async def test_each_person_gets_own_copy_and_own_sound(self) -> None:
        await self.register()
        self.state.toggle_motion(FRIEND, "gate")
        await self.bot.on_event(self.motion("m1"))
        by_chat = {c["chat_id"]: c for c in self.photos()}
        self.assertEqual({OWNER, FRIEND}, set(by_chat))
        self.assertTrue(by_chat[OWNER]["disable_notification"])
        self.assertFalse(by_chat[FRIEND]["disable_notification"])

    async def test_clip_goes_only_to_who_asked(self) -> None:
        await self.register()
        await self.bot.on_event(self.motion("m1"))
        token = self.state.issue_callback("gate", "snap", 600)
        await self.bot.on_callback(FRIEND, None, f"cv:snap:{token}", chat_id=FRIEND)
        before = len(self.photos())
        await self.bot.on_event(self.ready(self.media_requests[-1]["request_id"]))
        self.assertEqual(before + 1, len(self.photos()))
        self.assertEqual(FRIEND, self.photos()[-1]["chat_id"])

    async def test_frame_ids_of_two_chats_do_not_collide(self) -> None:
        """id сообщений в личках у разных людей совпадают — reply ищется в своём чате."""
        await self.register()
        self.bot.routes.remember_frame(500, "gate", Dest(OWNER), "2026-10-09T10:00:00Z", 600)
        self.bot.routes.remember_frame(500, "porch", Dest(FRIEND), "2026-10-09T10:00:30Z", 600)
        await self.bot.on_text(FRIEND, None, "клип", 500, 901, chat_id=FRIEND)
        self.assertEqual(("porch", "2026-10-09T10:00:30Z"),
                         (self.media_requests[-1]["camera_id"], self.media_requests[-1]["center_at"]))


class MixedModesTest(unittest.IsolatedAsyncioTestCase):
    """Маршрут задаётся на камеру: пресет лишь заполняет; одна камера может жить иначе."""

    async def test_one_camera_flat_others_in_topics(self) -> None:
        contract = CameraTopicPresetTest("test_registration_builds_route_once")
        contract.setUp()
        self.addCleanup(contract.doCleanups)
        await contract.register()
        contract.state.set_camera_mode("garage", "flat")
        self.assertEqual([Dest(GROUP)], contract.bot.routes.route("garage"))
        self.assertEqual(1, len(contract.bot.routes.route("gate")))
        self.assertIsNotNone(contract.bot.routes.route("gate")[0].thread_id)
        await contract.bot.on_event(contract.motion("m1", "garage"))
        caption = contract.photos()[0]["caption"]
        self.assertTrue(caption.startswith("Гараж · "), caption)


class HashtagTest(unittest.TestCase):
    def test_words(self) -> None:
        self.assertEqual("#крыльцо_2", hashtag("Крыльцо-2"))
        self.assertEqual("#калитка", hashtag("  Калитка "))
        self.assertEqual("#front_door", hashtag("Front door!"))
        self.assertEqual("#cam42", hashtag("42"))
        self.assertEqual("", hashtag(" -- "))
        self.assertEqual("", hashtag(None))
        self.assertLessEqual(len(hashtag("а" * 100)), 33)


class LegacyStateMigrationTest(unittest.IsolatedAsyncioTestCase):
    """Существующая установка (база до 0.3.0) без настроек остаётся «тема на камеру»."""

    LEGACY_SCHEMA = """
    CREATE TABLE topics (camera_id TEXT PRIMARY KEY, thread_id INTEGER NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL, closed_at REAL);
    CREATE TABLE panels (camera_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL,
        rendered TEXT NOT NULL DEFAULT '');
    CREATE TABLE media_requests (request_id TEXT PRIMARY KEY, camera_id TEXT NOT NULL,
        kind TEXT NOT NULL, thread_id INTEGER NOT NULL, created_at REAL NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE frame_replies (message_id INTEGER PRIMARY KEY, camera_id TEXT NOT NULL,
        thread_id INTEGER NOT NULL, center_at TEXT NOT NULL, expires_at REAL NOT NULL);
    CREATE TABLE service (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO topics VALUES ('porch2', 41, 'Крыльцо-2', 'active', 1.0, NULL);
    INSERT INTO topics VALUES ('old', 42, 'Старая', 'retired', 1.0, 2.0);
    INSERT INTO panels VALUES ('porch2', 4100, '');
    INSERT INTO media_requests VALUES ('r-old', 'porch2', 'clip', 41, 1.0, 0);
    INSERT INTO frame_replies VALUES (777, 'porch2', 41, '2026-10-08T10:00:00Z', 9e12);
    INSERT INTO service VALUES ('console_thread', '40');
    """

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.path = self.tmp / "bot.sqlite3"
        db = sqlite3.connect(self.path)
        db.executescript(self.LEGACY_SCHEMA)
        db.commit()
        db.close()

    async def test_legacy_install_keeps_camera_topics(self) -> None:
        state = State(str(self.path))
        self.addCleanup(state.close)
        tg = FakeTelegram()
        bot = CctvBot(make_config(self.tmp), state, Bridge(make_config(self.tmp),
                      client=httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(
                          lambda r: httpx.Response(200, content=PHOTO)))), tg)
        self.assertIsNone(state.get_service("route_preset"))
        self.assertEqual("camera", bot.routes.preset)
        self.assertEqual("camera", bot.routes.mode("porch2"))
        self.assertEqual([Dest(GROUP, 41)], bot.routes.route("porch2"))
        self.assertEqual([], bot.routes.route("old"))
        self.assertEqual(Dest(GROUP, 40), bot.routes.console())
        # Камеры перенесены из тем, архивная — архивной.
        self.assertEqual(["porch2"], [c.camera_id for c in state.active_cameras()])
        self.assertEqual("retired", state.camera("old").status)
        # Заявка и кадр до миграции отдаются в прежнюю тему группы.
        self.assertEqual(("porch2", "clip", Dest(GROUP, 41)), bot.routes.take_request("r-old"))
        found = bot.routes.resolve_frame(777, Dest(GROUP, 41))
        self.assertEqual("porch2", found[0].camera_id)
        self.assertEqual(Dest(GROUP, 41), found[1])
        # Событие — в прежнюю тему, новых тем не заводится.
        await bot.on_event(normalize_event({
            "event_id": "m-legacy", "type": "motion.detected", "camera_id": "porch2",
            "occurred_at": "2026-10-09T10:01:00Z",
            "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}}))
        self.assertEqual([Dest(GROUP, 41)], [Dest(c["chat_id"], c["message_thread_id"])
                                             for c in tg.of("send_photo")])
        self.assertEqual([], tg.of("create_forum_topic"))

    def test_migration_is_idempotent_and_keeps_rows(self) -> None:
        State(str(self.path)).close()
        state = State(str(self.path))
        self.addCleanup(state.close)
        columns = {r["name"]: r for r in state.db.execute("PRAGMA table_info(media_requests)")}
        self.assertEqual(0, columns["thread_id"]["notnull"])
        self.assertIn("chat_id", columns)
        self.assertEqual(1, state.db.execute("SELECT COUNT(*) FROM frame_replies").fetchone()[0])
        self.assertEqual(1, state.db.execute("SELECT COUNT(*) FROM media_requests").fetchone()[0])
        tables = {r[0] for r in state.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any(t.endswith("_pre030") for t in tables))


if __name__ == "__main__":
    unittest.main()
