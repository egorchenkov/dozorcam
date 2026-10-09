#!/usr/bin/env python3
"""Плоский режим и шаг мастера «куда присылать» (0.3.0, проект 0.3 разделы 2.1, 2.4, 2.5).

Путь с нуля, как на чистой машине: владелец по коду, затем выбор —
«📱 Сюда, в этот чат» (плоско в личке, установка за одно нажатие) или
«👥 В группу с темами» (как до 0.3.0). Группу без тем можно подключить и позже:
в плоском режиме она забирает ленту у личек, когда готова (админ, удаление,
закрепление), а темы ей не нужны. Совет «темы» — от 4 камер.

Настоящие CctvBot, State и Bridge; подменены только Bot API и HTTP моста.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import tempfile
import unittest
from types import SimpleNamespace

import httpx

from cctv import i18n
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.events import normalize_event
from cctv.bot.routes import Dest
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_wizard_20261002 import WizardTelegram

OWNER = 4242
FRIEND = 4343
STRANGER = 99
GROUP = -1001234567890
PLAIN_GROUP = -4001234567
PHOTO = b"jpeg-bytes"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


def camera(camera_id: str, title: str) -> dict:
    return {"camera_id": camera_id, "title": title, "site": title, "status": "online",
            "last_frame_at": "2026-10-09T10:00:00Z",
            "motion": {"state": "watching", "reason": "", "last_motion_at": None}}


class FlatWizardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = dataclasses.replace(make_config(self.tmp), chat_id=None,
                                       allowed_user_ids=frozenset(), owner_ids=frozenset(), lang="")
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.sent: list[tuple[str, dict]] = []
        self.cameras: list[dict] = []

        def handler(request: httpx.Request):
            body = json.loads(request.content) if request.content else {}
            self.sent.append((request.url.path, body))
            if request.url.path == "/v1/cameras" and request.method == "GET":
                return httpx.Response(200, json={"cameras": self.cameras})
            if request.url.path == "/v1/discovery/probes":
                return httpx.Response(200, json={"ok": True, "probe_token": "tok", "summary": {
                    "host": "127.0.0.1", "source": "manual", "verified": True,
                    "main_url": "rtsp://***@127.0.0.1:8554/testsrc"}})
            if request.url.path == "/v1/media-requests":
                return httpx.Response(202, json={"request_id": body.get("request_id"),
                                                 "status": "accepted"})
            if request.url.path.startswith("/v1/media/"):
                return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = WizardTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    # --- помощники ---------------------------------------------------------------
    def t(self, key: str, **params) -> str:
        return i18n.t(key, "ru", **params)

    async def claim(self) -> dict:
        """Владелец по коду; вернуть сообщение мастера с выбором."""
        answer = await self.bot.on_start(OWNER, OWNER, "private", [self.bot.setup_code()], "ru")
        self.assertIsNone(answer)
        return self.tg.of("send_message")[-1]

    @staticmethod
    def buttons(message: dict) -> list[tuple[str, str]]:
        markup = message["reply_markup"]
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    async def choose(self, message: dict, index: int, user: int = OWNER) -> str:
        data = self.buttons(message)[index][1]
        return await self.bot.on_callback(user, None, data, chat_id=user, message_id=777)

    def motion(self, event_id: str, camera_id: str = "gate"):
        at = "2026-10-09T10:01:00Z"
        return normalize_event({
            "event_id": event_id, "type": "motion.detected", "camera_id": camera_id,
            "occurred_at": at, "captured_at": at, "source": "recorded_main_person_detector",
            "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA,
                         "bytes": len(PHOTO)}})

    def photo_chats(self) -> list[tuple[int, int | None]]:
        return [(p["chat_id"], p.get("message_thread_id")) for p in self.tg.of("send_photo")]

    def pinned(self) -> list[int]:
        return [p["chat_id"] for p in self.tg.of("pin_chat_message")]

    # --- шаг мастера ---------------------------------------------------------------
    async def test_owner_gets_where_choice_with_two_buttons(self) -> None:
        message = await self.claim()
        self.assertEqual(OWNER, message["chat_id"])
        self.assertIn(self.t("wizard.owner_set"), message["text"])
        self.assertIn(self.t("wizard.where"), message["text"])
        labels = [text for text, _ in self.buttons(message)]
        # Камер ещё нет — совет «сюда».
        self.assertEqual([self.t("wizard.where_here") + " ⭐", self.t("wizard.where_group")], labels)
        self.assertNotIn(self.t("wizard.where_many", count=0), message["text"])
        # В callback_data — непрозрачный токен, а не выбор открытым текстом.
        self.assertTrue(all(data.startswith("cv:where:") and data.split(":", 2)[2] not in ("here", "group")
                            for _, data in self.buttons(message)))
        self.assertEqual([], self.tg.of("create_forum_topic"))

    async def test_four_cameras_recommend_topics(self) -> None:
        self.cameras = [camera(f"c{i}", f"Камера {i}") for i in range(4)]
        message = await self.claim()
        self.assertIn(self.t("wizard.where_many", count=4), message["text"])
        labels = [text for text, _ in self.buttons(message)]
        self.assertEqual([self.t("wizard.where_here"), self.t("wizard.where_group") + " ⭐"], labels)
        self.cameras = self.cameras[:3]
        self.state.delete_service("route_preset")
        await self.bot.on_start(OWNER, OWNER, "private", [], "ru")
        self.assertNotIn(self.t("wizard.where_many", count=3), self.tg.of("send_message")[-1]["text"])

    async def test_here_finishes_setup_in_one_tap(self) -> None:
        self.cameras = [camera("gate", "Калитка")]
        message = await self.claim()
        self.assertEqual("✅", await self.choose(message, 0))
        self.assertEqual("flat", self.bot.routes.preset)
        self.assertIsNone(self.bot.chat_id)
        # Сообщение выбора стало «готово» без кнопок, карта закреплена в личке.
        edit = [e for e in self.tg.of("edit_message_text") if e["message_id"] == 777][-1]
        self.assertEqual(self.t("wizard.here_ready"), edit["text"])
        self.assertIsNone(edit["reply_markup"])
        # Закреплены в личке прогресс мастера и карта; камера уже есть — все три
        # шага пройдены, прогресс откреплён, закреп чата остаётся за картой.
        self.assertEqual([OWNER, OWNER], self.pinned())
        home = self.bot.routes.home_screen(Dest(OWNER))
        self.assertIsNotNone(home)
        self.assertEqual(home.message_id, self.tg.of("pin_chat_message")[-1]["message_id"])
        self.assertEqual([self.tg.of("pin_chat_message")[0]["message_id"]],
                         [u["message_id"] for u in self.tg.of("unpin_chat_message")])
        self.assertEqual([], self.tg.of("create_forum_topic"))
        # Событие камеры — в личку владельца, без темы, с именем камеры и хэштегом.
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual([(OWNER, None)], self.photo_chats())
        caption = self.tg.of("send_photo")[0]["caption"]
        self.assertIn("Калитка", caption)
        self.assertIn("#калитка", caption)
        # /start в личке теперь — справка плоского режима, а не «создайте группу».
        self.assertEqual(self.t("wizard.private_help_here"),
                         await self.bot.on_start(OWNER, OWNER, "private", [], "ru"))

    async def test_add_in_private_after_here(self) -> None:
        message = await self.claim()
        await self.choose(message, 0)
        self.assertEqual(self.t("add.searching_here"), await self.bot.on_add(OWNER, [], chat_id=OWNER))
        answer = await self.bot.on_add(OWNER, ["rtsp://127.0.0.1:8554/testsrc"], chat_id=OWNER)
        self.assertIn("rtsp://127.0.0.1:8554/testsrc", answer)
        # Логин и пароль — reply на просьбу бота: в личке так отвечают чаще, чем в теме.
        reply = await self.bot.on_text(OWNER, None, "admin s3cr3t", 31, 555, chat_id=OWNER)
        self.assertEqual([555], self.tg.deleted)
        self.assertEqual([OWNER], [d["chat_id"] for d in self.tg.of("delete_message")])
        self.assertNotIn("s3cr3t", reply)
        self.assertEqual(1, len([p for p, _ in self.sent if p == "/v1/discovery/probes"]))
        self.assertEqual("name|tok|rtsp://127.0.0.1:8554/testsrc", self.state.take_input(OWNER)[1])

    async def test_foreign_camera_events_do_not_hammer_the_registry(self) -> None:
        message = await self.claim()
        await self.choose(message, 0)
        calls = lambda: len([p for p, _ in self.sent if p == "/v1/cameras"])  # noqa: E731
        before = calls()
        await self.bot.on_event(self.motion("g1", "ghost"))
        after_first = calls()
        self.assertGreater(after_first, before)  # первое событие — сверка
        await self.bot.on_event(self.motion("g2", "ghost"))
        self.assertEqual(after_first, calls())  # второе в ту же минуту — без сверки
        self.assertEqual([], self.photo_chats())

    async def test_reply_without_pending_input_is_still_a_frame_reply(self) -> None:
        message = await self.claim()
        await self.choose(message, 0)
        self.assertEqual(self.t("frame.unknown_flat"),
                         await self.bot.on_text(OWNER, None, "клип", 31, 556, chat_id=OWNER))

    async def test_flat_texts_do_not_point_to_topics(self) -> None:
        """Без тем бот не отсылает «в тему камеры» — её нет."""
        self.cameras = [camera("gate", "Калитка")]
        message = await self.claim()
        await self.choose(message, 0)
        self.assertEqual(self.t("hint.unknown_flat"),
                         await self.bot.on_text(OWNER, None, "привет", None, 557, chat_id=OWNER))
        await self.bot.first_frame("gate")
        texts = [m["text"] for m in self.tg.of("send_message")]
        self.assertIn(self.t("add.first_frame_feed", title="Калитка"), texts)
        self.assertNotIn(self.t("add.first_frame", title="Калитка"), texts)
        snapshot = [b for path, b in self.sent if path == "/v1/media-requests"]
        self.assertEqual(["snapshot"], [b["kind"] for b in snapshot])
        # Снять и переименовать с карточки в личке: своей темы нет — закрывать нечего.
        self.assertEqual(self.t("input.retire_map"),
                         await self.bot._control(OWNER, "gate", Dest(OWNER), "retire"))
        self.assertEqual(self.t("input.rename_here", minutes=5),
                         await self.bot._control(OWNER, "gate", Dest(OWNER), "rename"))
        self.assertTrue((await self.bot.menu_text()).startswith(self.t("menu.header_flat")))

    async def test_group_choice_keeps_forum_path(self) -> None:
        message = await self.claim()
        await self.choose(message, 1)
        self.assertEqual("camera", self.bot.routes.preset)
        edit = [e for e in self.tg.of("edit_message_text") if e["message_id"] == 777][-1]
        self.assertEqual(self.t("wizard.add_to_group") + "\n\n" + self.t("client.pick"), edit["text"])
        self.assertEqual(self.t("wizard.add_to_group"),
                         await self.bot.on_start(OWNER, OWNER, "private", [], "ru"))
        self.cameras = [camera("gate", "Калитка")]
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.assertEqual(GROUP, self.bot.chat_id)
        names = [c["name"] for c in self.tg.of("create_forum_topic")]
        self.assertEqual([self.t("console.title"), "Калитка"], names)
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual(1, len(self.photo_chats()))
        self.assertEqual(GROUP, self.photo_chats()[0][0])
        self.assertIsNotNone(self.photo_chats()[0][1])  # в тему камеры

    async def test_skipped_choice_is_offered_again(self) -> None:
        await self.claim()
        before = len(self.tg.of("send_message"))
        self.assertIsNone(await self.bot.on_start(OWNER, OWNER, "private", [], "ru"))
        self.assertEqual(before + 1, len(self.tg.of("send_message")))
        self.assertIn(self.t("wizard.where"), self.tg.of("send_message")[-1]["text"])
        self.assertEqual(self.t("wizard.where_first"), await self.bot.on_add(OWNER, [], chat_id=OWNER))

    async def test_only_owner_chooses(self) -> None:
        message = await self.claim()
        self.state.add_member(FRIEND, "Друг")
        self.assertEqual(self.t("access.owner_only"), await self.choose(message, 0, user=FRIEND))
        self.assertEqual(self.t("no_access"), await self.choose(message, 0, user=STRANGER))
        self.assertFalse(self.bot.routes.chosen)

    async def test_choice_after_group_is_bound_changes_nothing(self) -> None:
        message = await self.claim()
        await self.bot.bind_group(GROUP, OWNER)
        self.assertEqual(self.t("wizard.where_done"), await self.choose(message, 0))
        self.assertEqual("camera", self.bot.routes.preset)

    async def test_configured_group_skips_the_choice(self) -> None:
        self.bot.cfg = dataclasses.replace(self.cfg, chat_id=GROUP)
        answer = await self.bot.on_start(OWNER, OWNER, "private", [self.bot.setup_code()], "ru")
        self.assertEqual(self.t("wizard.owner_set"), answer)
        self.assertEqual([], self.tg.of("send_message"))

    # --- плоско в группе без тем -------------------------------------------------------
    async def test_plain_group_after_here_takes_the_feed_when_ready(self) -> None:
        self.cameras = [camera("gate", "Калитка")]
        message = await self.claim()
        await self.choose(message, 0)
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="member")
        # Бот обычный участник: группа не привязана, лента остаётся в личке.
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "member")
        text = self.tg.of("send_message")[-1]["text"]
        self.assertIn(self.t("wizard.need_admin"), text)
        self.assertNotIn(self.t("wizard.need_forum"), text)
        self.assertIsNone(self.bot.chat_id)
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual([(OWNER, None)], self.photo_chats())
        # Админ без права «Управление темами» — плоскому режиму хватает.
        self.tg.member = SimpleNamespace(status="administrator", can_manage_topics=False,
                                         can_delete_messages=True, can_pin_messages=True)
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "administrator")
        self.assertEqual(PLAIN_GROUP, self.bot.chat_id)
        self.assertEqual(self.t("wizard.group_ready_flat"), self.tg.of("send_message")[-1]["text"])
        self.assertEqual([], self.tg.of("create_forum_topic"))
        self.assertIn(PLAIN_GROUP, self.pinned())
        # Карта в личке честно говорит, что переехала в группу и почему, и
        # открепляется (аудит 09.10.2026, Б-10).
        moved = [e for e in self.tg.of("edit_message_text") if e["chat_id"] == OWNER
                 and e["text"] == self.t("map.moved_group")]
        self.assertEqual(1, len(moved))
        self.assertIn((OWNER, moved[0]["message_id"]),
                      [(u["chat_id"], u["message_id"]) for u in self.tg.of("unpin_chat_message")])
        self.assertIsNone(self.bot.routes.home_screen(Dest(OWNER)))
        await self.bot.on_event(self.motion("m2"))
        self.assertEqual((PLAIN_GROUP, None), self.photo_chats()[-1])
        self.assertEqual(self.t("wizard.private_help_flat"),
                         await self.bot.on_start(OWNER, OWNER, "private", [], "ru"))
        # Повтор (/setup) не плодит вторую карту.
        self.assertEqual(self.t("wizard.group_already"), await self.bot.bind_group(PLAIN_GROUP, OWNER))
        self.assertEqual(1, self.pinned().count(PLAIN_GROUP))

    async def test_plain_group_with_topics_choice_offers_flat(self) -> None:
        self.cameras = [camera("gate", "Калитка")]
        message = await self.claim()
        await self.choose(message, 1)
        self.tg.forum = False
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "administrator")
        text = self.tg.of("send_message")[-1]["text"]
        self.assertIn(self.t("wizard.need_forum"), text)
        self.assertIn(self.t("wizard.or_flat"), text)
        # /mode flat в этой же группе — лента одной группой, без тем.
        answer = await self.bot.on_mode(OWNER, ["flat"], chat_id=PLAIN_GROUP)
        self.assertEqual(self.t("mode.changed", preset=self.t("mode.preset.flat"),
                                where=self.t("mode.where.flat_group")), answer)
        self.assertEqual([], self.tg.of("create_forum_topic"))
        self.assertIn(PLAIN_GROUP, self.pinned())
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual([(PLAIN_GROUP, None)], self.photo_chats())
        self.assertEqual(self.t("add.searching_here"),
                         await self.bot.on_add(OWNER, [], chat_id=PLAIN_GROUP))

    async def test_mode_flat_in_group_needs_admin(self) -> None:
        message = await self.claim()
        await self.choose(message, 1)
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="member")
        await self.bot.bind_group(PLAIN_GROUP, OWNER)
        answer = await self.bot.on_mode(OWNER, ["flat"], chat_id=PLAIN_GROUP)
        self.assertIn(self.t("wizard.need_admin"), answer)
        self.assertNotIn(self.t("wizard.need_forum"), answer)
        self.assertEqual("camera", self.bot.routes.preset)

    async def test_mode_without_cameras_suggests_add(self) -> None:
        message = await self.claim()
        await self.choose(message, 1)
        await self.bot.bind_group(GROUP, OWNER)
        answer = await self.bot.on_mode(OWNER, ["flat"], chat_id=GROUP)
        self.assertIn(self.t("mode.add_first"), answer)

    async def test_plain_group_migration_drops_old_map(self) -> None:
        message = await self.claim()
        await self.choose(message, 0)
        self.tg.forum = False
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "administrator")
        self.assertIsNotNone(self.bot.routes.home_screen(Dest(PLAIN_GROUP)))
        new_id = GROUP - 1  # супергруппа после миграции; литерал похожего id гейт экспорта не пропускает
        await self.bot.on_chat_migrated(PLAIN_GROUP, new_id)
        self.assertEqual(new_id, self.bot.chat_id)
        self.assertEqual([], [s for s in self.state.screens() if s.chat_id == PLAIN_GROUP])
        await self.bot.sync_registry()
        self.assertIsNotNone(self.bot.routes.home_screen(Dest(new_id)))

    # --- несколько людей без группы -------------------------------------------------------
    async def test_invited_person_gets_own_feed_and_map(self) -> None:
        self.cameras = [camera("gate", "Калитка")]
        message = await self.claim()
        await self.choose(message, 0)
        await self.bot.on_invite(OWNER, chat_id=OWNER, chat_type="private")
        start = re.search(r"start=(inv\S+)", self.tg.of("send_message")[-1]["text"]).group(1)
        answer = await self.bot.on_start(FRIEND, FRIEND, "private", [start], "ru", name="Друг")
        self.assertEqual(self.t("invite.welcome_private"), answer)
        self.assertIn(FRIEND, self.pinned())
        await self.bot.on_event(self.motion("m1"))
        self.assertEqual({OWNER, FRIEND}, {chat for chat, _ in self.photo_chats()})
        self.assertTrue(all(thread is None for _, thread in self.photo_chats()))


if __name__ == "__main__":
    unittest.main()
