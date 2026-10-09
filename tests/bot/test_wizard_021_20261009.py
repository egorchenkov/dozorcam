#!/usr/bin/env python3
"""Состав 0.2.1 в 0.3.0 (план юзер-френдли, разделы 2.2–2.4).

- Закреплённое сообщение-прогресс мастера в личке владельца «Шаг 1/3…3/3».
- Кнопки «Android / iPhone / Desktop» с инструкцией по темам и правам (i18n).
- Группа без тем/прав: бот сам перепроверяет её — по my_chat_member и по таймеру
  (включение тем приходит без апдейта), без ручного /setup; тот же список
  недостач второй раз не пишет.
- /help, /version; проверка GitHub Releases раз в сутки (CCTV_UPDATE_CHECK=0 —
  выкл.), строка «Доступна X — dozorcam update» на карте.
- Пустой /add: причина, CCTV_DISCOVERY_NETWORKS и кнопка ввода адреса.
- Адрес с паролем в пути (XMEye) от человека не принимается; сработавший путь
  RTSP бот показывает.

Настоящие CctvBot, State и Bridge; подменены только Bot API и HTTP моста.
Каждый сценарий — в обоих режимах, где он к ним относится (форум и плоско).
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

import httpx

from cctv import __version__, i18n
from cctv.bot import bot as bot_module
from cctv.bot import config, updates
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.routes import Dest
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_wizard_20261002 import WizardTelegram

OWNER = 4242
FRIEND = 4343
GROUP = -1001234567890
PLAIN_GROUP = -4001234567
MIGRATED = GROUP - 7


def camera(camera_id: str, title: str) -> dict:
    return {"camera_id": camera_id, "title": title, "site": title, "status": "online",
            "last_frame_at": "2026-10-09T10:00:00Z",
            "motion": {"state": "watching", "reason": "", "last_motion_at": None}}


class Clock:
    def __init__(self, start: float = 1_791_500_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = dataclasses.replace(make_config(self.tmp), chat_id=None,
                                       allowed_user_ids=frozenset(), owner_ids=frozenset(), lang="")
        self.clock = Clock()
        self.state = State(":memory:", now=self.clock)
        self.addCleanup(self.state.close)
        self.sent: list[tuple[str, dict]] = []
        self.cameras: list[dict] = []
        self.scan = {"ok": True, "scan_id": "s1", "status": "done",
                     "networks": ["192.0.2.0/24"], "candidates": []}

        def handler(request: httpx.Request):
            body = json.loads(request.content) if request.content else {}
            self.sent.append((request.url.path, body))
            path = request.url.path
            if path == "/v1/cameras" and request.method == "GET":
                return httpx.Response(200, json={"cameras": self.cameras})
            if path == "/v1/discovery/scans" and request.method == "POST":
                return httpx.Response(200, json={"ok": True, "scan_id": "s1",
                                                 "networks": self.scan["networks"]})
            if path.startswith("/v1/discovery/scans/"):
                return httpx.Response(200, json=self.scan)
            if path == "/v1/discovery/probes":
                return httpx.Response(200, json={"ok": True, "probe_token": "tok", "summary": {
                    "host": "192.0.2.30", "source": "template", "verified": True,
                    "template": "Xiongmai/XMEye",
                    "path": "/user=admin&password=***&channel=1&stream=0.sdp",
                    "main_url": "rtsp://***@192.0.2.30:554/user=admin&password=***&channel=1&stream=0.sdp",
                    "sub_url": "rtsp://***@192.0.2.30:554/user=admin&password=***&channel=1&stream=1.sdp"}})
            if path == "/v1/media-requests":
                return httpx.Response(202, json={"request_id": body.get("request_id"),
                                                 "status": "accepted"})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = WizardTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)
        self.fetched: list[str] = []
        self.release = "0.0.1"
        self.bot.updates = self.checker(enabled=True)
        self.original_poll = bot_module.SCAN_POLL_SEC
        bot_module.SCAN_POLL_SEC = 0
        self.addCleanup(setattr, bot_module, "SCAN_POLL_SEC", self.original_poll)

    def checker(self, *, enabled: bool) -> updates.UpdateChecker:
        def fetch(url: str) -> str:
            self.fetched.append(url)
            if isinstance(self.release, Exception):
                raise self.release
            return self.release
        return updates.UpdateChecker(self.state, enabled=enabled, fetch=fetch, now=self.clock)

    def t(self, key: str, **params) -> str:
        return i18n.t(key, "ru", **params)

    async def claim(self) -> dict:
        answer = await self.bot.on_start(OWNER, OWNER, "private", [self.bot.setup_code()], "ru")
        self.assertIsNone(answer)
        return self.tg.of("send_message")[-1]

    @staticmethod
    def buttons(message: dict) -> list[tuple[str, str]]:
        markup = message["reply_markup"]
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    async def choose(self, message: dict, index: int) -> str:
        data = self.buttons(message)[index][1]
        return await self.bot.on_callback(OWNER, None, data, chat_id=OWNER, message_id=777)

    def progress(self) -> dict:
        """Сообщение-прогресс: первое отправленное в личку владельца."""
        return self.tg.of("send_message")[0]

    def progress_now(self) -> str:
        """Текущий текст прогресса: последняя правка его сообщения или он сам."""
        first = self.progress()
        message_id = self.tg.of("pin_chat_message")[0]["message_id"]
        edits = [e["text"] for e in self.tg.of("edit_message_text") if e["message_id"] == message_id
                 and e["chat_id"] == OWNER]
        return edits[-1] if edits else first["text"]

    def step(self, n: int, done: bool, what: str) -> str:
        return self.t("progress.step", n=n, mark="✅" if done else "⬜", what=what)


class ProgressTest(Base):
    async def test_progress_is_pinned_before_the_where_choice(self) -> None:
        where = await self.claim()
        progress = self.progress()
        self.assertEqual(OWNER, progress["chat_id"])
        self.assertIsNot(where, progress)
        self.assertEqual("\n".join([self.t("progress.header"),
                                    self.step(1, True, self.t("progress.owner")),
                                    self.step(2, False, self.t("progress.where")),
                                    self.step(3, False, self.t("progress.camera_wait"))]),
                         progress["text"])
        pin = self.tg.of("pin_chat_message")[0]
        self.assertEqual(OWNER, pin["chat_id"])
        self.assertTrue(pin["disable_notification"])
        # Выбор места — последним сообщением, под ним кнопки.
        self.assertIn(self.t("wizard.where"), where["text"])

    async def test_forum_path_walks_all_three_steps_and_unpins(self) -> None:
        where = await self.claim()
        await self.choose(where, 1)  # в группу с темами
        self.assertIn(self.step(2, False, self.t("progress.group_wait")), self.progress_now())
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.assertIn(self.step(2, True, self.t("progress.group")), self.progress_now())
        self.assertIn(self.step(3, False, self.t("progress.camera_wait")), self.progress_now())
        self.assertEqual([], self.tg.of("unpin_chat_message"))
        self.cameras = [camera("gate", "Калитка")]
        await self.bot.sync_registry()
        final = self.progress_now()
        self.assertIn(self.step(3, True, self.t("progress.camera", title="Калитка")), final)
        self.assertTrue(final.endswith(self.t("progress.done")))
        unpinned = self.tg.of("unpin_chat_message")
        self.assertEqual([(OWNER, self.tg.of("pin_chat_message")[0]["message_id"])],
                         [(u["chat_id"], u["message_id"]) for u in unpinned])
        # Дальше прогресс не трогается: новые камеры его не правят.
        edits = len(self.tg.of("edit_message_text"))
        self.cameras.append(camera("yard", "Двор"))
        await self.bot.sync_registry()
        self.assertEqual([], [e for e in self.tg.of("edit_message_text")[edits:]
                              if e["message_id"] == unpinned[0]["message_id"]])

    async def test_here_path_two_steps_at_once_then_camera(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)  # сюда, в этот чат
        self.assertIn(self.step(2, True, self.t("progress.here")), self.progress_now())
        self.assertIn(self.step(3, False, self.t("progress.camera_wait")), self.progress_now())
        self.cameras = [camera("gate", "Калитка")]
        await self.bot.sync_registry()
        self.assertTrue(self.progress_now().endswith(self.t("progress.done")))
        self.assertEqual(1, len(self.tg.of("unpin_chat_message")))
        # Закреп лички остаётся за картой камер.
        home = self.bot.routes.home_screen(Dest(OWNER))
        self.assertEqual(home.message_id, self.tg.of("pin_chat_message")[-1]["message_id"])

    async def test_no_progress_without_wizard(self) -> None:
        """Группа и люди из конфига (как в проде до 0.3.0) — мастера нет, прогресса тоже."""
        self.bot.cfg = dataclasses.replace(self.cfg, chat_id=GROUP)
        await self.bot.on_start(OWNER, OWNER, "private", [self.bot.setup_code()], "ru")
        self.assertEqual([], self.tg.of("pin_chat_message"))
        await self.bot.refresh_progress()
        self.assertEqual([], self.tg.of("edit_message_text"))

    async def test_unchanged_progress_is_not_edited(self) -> None:
        await self.claim()
        await self.bot.refresh_progress()
        await self.bot.refresh_progress()
        self.assertEqual([], [e for e in self.tg.of("edit_message_text") if e["chat_id"] == OWNER])


class ClientHelpTest(Base):
    async def test_group_choice_offers_three_clients(self) -> None:
        where = await self.claim()
        await self.choose(where, 1)
        edit = [e for e in self.tg.of("edit_message_text") if e["message_id"] == 777][-1]
        labels = [b.text for row in edit["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([self.t("client.android.button"), self.t("client.ios.button"),
                          self.t("client.desktop.button")], labels)
        datas = [b.callback_data for row in edit["reply_markup"].inline_keyboard for b in row]
        self.assertTrue(all(d.startswith("cv:client:") and "android" not in d for d in datas))
        for index, client in enumerate(("android", "ios", "desktop")):
            with self.subTest(client):
                answer = await self.bot.on_callback(OWNER, None, datas[index], chat_id=OWNER)
                self.assertEqual(self.t("client.sent"), answer)
                text = self.tg.of("send_message")[-1]["text"]
                self.assertEqual(OWNER, self.tg.of("send_message")[-1]["chat_id"])
                self.assertTrue(text.startswith(self.t(f"client.{client}.title")))
                self.assertIn(self.t(f"client.{client}.topics"), text)
                self.assertIn(self.t(f"client.{client}.admin", bot="@ExampleCctvBot",
                                     rights=self.t("client.rights_forum")), text)
                self.assertIn("@ExampleCctvBot", text)
                self.assertTrue(text.endswith(self.t("client.after")))

    async def test_here_choice_has_no_client_buttons(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        edit = [e for e in self.tg.of("edit_message_text") if e["message_id"] == 777][-1]
        self.assertIsNone(edit["reply_markup"])

    async def test_flat_group_problems_get_instructions_without_topics(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="member")
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "member")
        message = self.tg.of("send_message")[-1]
        self.assertEqual(PLAIN_GROUP, message["chat_id"])
        datas = [b.callback_data for row in message["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(3, len(datas))
        await self.bot.on_callback(OWNER, None, datas[1], chat_id=PLAIN_GROUP)
        text = self.tg.of("send_message")[-1]["text"]
        self.assertEqual(PLAIN_GROUP, self.tg.of("send_message")[-1]["chat_id"])
        self.assertNotIn(self.t("client.ios.topics"), text)
        self.assertIn(self.t("client.rights_flat"), text)
        self.assertNotIn(self.t("client.rights_forum"), text)

    async def test_strangers_cannot_press(self) -> None:
        where = await self.claim()
        await self.choose(where, 1)
        edit = [e for e in self.tg.of("edit_message_text") if e["message_id"] == 777][-1]
        data = edit["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertEqual(self.t("no_access"), await self.bot.on_callback(99, None, data, chat_id=99))

    async def test_setup_reply_markup_only_for_problems(self) -> None:
        await self.claim()
        self.assertIsNone(self.bot.group_help_markup(self.t("wizard.group_ready")))
        self.assertIsNone(self.bot.group_help_markup(None))
        needs = "\n".join([self.t("wizard.group_needs"), self.t("wizard.need_forum")])
        self.assertIsNotNone(self.bot.group_help_markup(needs))


class AutoRetryTest(Base):
    """Бот сам доводит группу: без ручного /setup и без повторов одного и того же."""

    async def group_mode(self) -> None:
        where = await self.claim()
        await self.choose(where, 1)

    def group_messages(self, chat: int = GROUP) -> list[str]:
        return [m["text"] for m in self.tg.of("send_message") if m["chat_id"] == chat]

    async def test_promotion_retries_and_same_problems_are_not_repeated(self) -> None:
        await self.group_mode()
        self.tg.member = SimpleNamespace(status="member")
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "member")
        self.assertEqual(1, len(self.group_messages()))
        self.assertIn(self.t("wizard.need_admin"), self.group_messages()[0])
        # Ещё один my_chat_member с теми же недостачами — молчим.
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "member")
        self.assertEqual(1, len(self.group_messages()))
        # Админ, но без «Закрепления» — новая недостача, говорим.
        self.tg.member = SimpleNamespace(status="administrator", can_manage_topics=True,
                                         can_delete_messages=True, can_pin_messages=False)
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.assertEqual(2, len(self.group_messages()))
        self.assertIn(self.t("wizard.need_pin_right"), self.group_messages()[-1])
        # Дали право — группа готова сама, без /setup.
        self.tg.member = SimpleNamespace(status="administrator", can_manage_topics=True,
                                         can_delete_messages=True, can_pin_messages=True)
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.assertEqual(self.t("wizard.group_ready"), self.group_messages()[-1])
        self.assertIsNone(self.state.get_service(bot_module.GROUP_PENDING_KEY))
        self.assertEqual([self.t("console.title")], [c["name"] for c in self.tg.of("create_forum_topic")])

    async def test_topics_turned_on_without_update_are_noticed_by_the_timer(self) -> None:
        await self.group_mode()
        self.tg.forum = False
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.assertIn(self.t("wizard.need_forum"), self.group_messages()[-1])
        # Тик таймера, темы ещё выключены — тихо.
        self.assertFalse(await self.bot.recheck_pending_group())
        self.assertEqual(1, len(self.group_messages()))
        # Темы включили — Telegram боту об этом не пишет; следующий тик замечает сам.
        self.tg.forum = True
        self.clock.now += 60
        self.assertTrue(await self.bot.recheck_pending_group())
        self.assertEqual(self.t("wizard.group_ready"), self.group_messages()[-1])
        console = self.bot.routes.console()
        self.assertIn(self.t("wizard.console_ready"),
                      [m["text"] for m in self.tg.of("send_message") if m.get("message_thread_id") == console.thread_id])
        self.assertIn(self.step(2, True, self.t("progress.group")), self.progress_now())
        # Больше перепроверять нечего.
        self.assertFalse(await self.bot.recheck_pending_group())
        self.assertEqual(1, self.group_messages().count(self.t("wizard.group_ready")))

    async def test_timer_already_made_the_console_still_says_ready(self) -> None:
        """Сверка по таймеру успела завести «Пульт» — человек всё равно слышит «готово»."""
        await self.group_mode()
        self.tg.forum = False
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.tg.forum = True
        await self.bot.refresh_console()  # как panel_refresh раньше перепроверки
        self.assertTrue(await self.bot.recheck_pending_group())
        self.assertEqual(self.t("wizard.group_ready"), self.group_messages()[-1])

    async def test_migration_to_supergroup_rechecks_at_once(self) -> None:
        await self.group_mode()
        self.tg.forum = False
        await self.bot.on_bot_membership(GROUP, "group", OWNER, "administrator")
        self.tg.forum = True
        await self.bot.on_chat_migrated(GROUP, MIGRATED)
        self.assertEqual(MIGRATED, self.bot.chat_id)
        self.assertEqual(self.t("wizard.group_ready"), self.group_messages(MIGRATED)[-1])

    async def test_recheck_gives_up_after_a_day(self) -> None:
        await self.group_mode()
        self.tg.forum = False
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.clock.now += bot_module.GROUP_RECHECK_FOR_SEC + 1
        self.tg.forum = True
        self.assertFalse(await self.bot.recheck_pending_group())
        self.assertIsNone(self.state.get_service(bot_module.GROUP_PENDING_KEY))
        # /setup по-прежнему работает руками.
        self.assertEqual(self.t("wizard.group_ready"), await self.bot.bind_group(GROUP, OWNER))

    async def test_flat_plain_group_is_picked_up_by_the_timer(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="administrator", can_manage_topics=False,
                                         can_delete_messages=True, can_pin_messages=False)
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "administrator")
        self.assertIsNone(self.bot.chat_id)
        self.tg.member = SimpleNamespace(status="administrator", can_manage_topics=False,
                                         can_delete_messages=True, can_pin_messages=True)
        self.assertTrue(await self.bot.recheck_pending_group())
        self.assertEqual(PLAIN_GROUP, self.bot.chat_id)
        self.assertEqual(self.t("wizard.group_ready_flat"), self.group_messages(PLAIN_GROUP)[-1])

    async def test_removed_bot_stops_waiting(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="member")
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "member")
        self.assertIsNotNone(self.state.get_service(bot_module.GROUP_PENDING_KEY))
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "left")
        self.assertIsNone(self.state.get_service(bot_module.GROUP_PENDING_KEY))
        self.assertFalse(await self.bot.recheck_pending_group())
        self.assertIsNone(self.bot.chat_id)

    async def test_unreachable_group_is_never_bound_blindly(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="member")
        await self.bot.on_bot_membership(PLAIN_GROUP, "group", OWNER, "member")

        async def gone(**_kwargs):
            raise RuntimeError("Forbidden: bot was kicked")
        self.tg.get_chat = gone
        self.tg.get_chat_member = gone
        self.assertFalse(await self.bot.recheck_pending_group())
        self.assertIsNone(self.bot.chat_id)

    async def test_nothing_pending_nothing_asked(self) -> None:
        await self.group_mode()
        calls = len(self.tg.calls)
        self.assertFalse(await self.bot.recheck_pending_group())
        self.assertEqual(calls, len(self.tg.calls))


class UpdateCheckTest(Base):
    async def ready_here(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)

    def map_text(self) -> str:
        home = self.bot.routes.home_screen(Dest(OWNER))
        edits = [e["text"] for e in self.tg.of("edit_message_text") if e["message_id"] == home.message_id]
        sent = [m["text"] for m in self.tg.of("send_message") if m["chat_id"] == OWNER
                and m["text"].startswith(self.t("map.header", cameras=0, online=0, events=0)[:4])]
        return (edits or sent)[-1]

    async def test_once_a_day_and_the_map_line(self) -> None:
        await self.ready_here()
        self.release = "99.0.0"
        self.assertTrue(await self.bot.check_updates())
        self.assertEqual(1, len(self.fetched))
        self.assertEqual(updates.DEFAULT_URL, self.fetched[0])
        await self.bot.refresh_console(force=True)
        self.assertIn(self.t("update.available", version="99.0.0"), self.map_text())
        # В те же сутки — без запроса.
        self.clock.now += 3600
        self.assertFalse(await self.bot.check_updates())
        self.assertEqual(1, len(self.fetched))
        # Через сутки — снова; та же версия — не новость.
        self.clock.now += updates.CHECK_EVERY_SEC
        self.assertFalse(await self.bot.check_updates())
        self.assertEqual(2, len(self.fetched))

    async def test_latest_installed_shows_no_line(self) -> None:
        await self.ready_here()
        self.release = __version__
        self.assertFalse(await self.bot.check_updates())
        await self.bot.refresh_console(force=True)
        self.assertNotIn("dozorcam update", self.map_text())
        self.assertIn(self.t("version.latest"), await self.bot.version_text())

    async def test_switched_off_never_asks(self) -> None:
        self.bot.updates = self.checker(enabled=False)
        self.state.set_service(updates.LATEST_KEY, "99.0.0")  # знание из прошлого — не показываем
        await self.ready_here()
        self.assertFalse(await self.bot.check_updates())
        text = await self.bot.version_text()
        self.assertEqual([], self.fetched)
        self.assertIn(self.t("version.current", version=__version__), text)
        self.assertIn(self.t("version.check_off"), text)
        self.assertNotIn("dozorcam update", self.map_text())

    async def test_failure_retries_after_an_hour_not_before(self) -> None:
        await self.claim()
        self.release = OSError("no network")
        self.assertFalse(await self.bot.check_updates())
        self.assertIn(self.t("version.unknown"), await self.bot.version_text())
        self.assertEqual(1, len(self.fetched))  # /version в тот же час не спрашивает снова
        self.clock.now += updates.RETRY_AFTER_FAIL_SEC
        self.release = "99.1.0"
        text = await self.bot.version_text()
        self.assertEqual(2, len(self.fetched))
        self.assertIn(self.t("update.available", version="99.1.0"), text)
        self.assertIn(self.t("version.how_off"), text)

    async def test_odd_tags_are_not_offered(self) -> None:
        self.assertTrue(updates.newer("v99.0.0", "0.3.0"))
        self.assertTrue(updates.newer("0.3.1", "0.3.0"))
        self.assertTrue(updates.newer("0.10.0", "0.9.9"))
        self.assertFalse(updates.newer("0.3.0", "0.3.0"))
        self.assertFalse(updates.newer("0.2.9", "0.3.0"))
        self.assertFalse(updates.newer("0.4.0-rc1", "0.3.0"))
        self.assertFalse(updates.newer("latest", "0.3.0"))
        self.assertFalse(updates.newer(None, "0.3.0"))

    def test_config_switch(self) -> None:
        base = {"CCTV_BOT_TOKEN": "0:x", "CCTV_INTERNAL_TLS": "0"}
        self.assertTrue(config.load(base).update_check)
        for off in ("0", "false", "OFF", "no"):
            with self.subTest(off):
                self.assertFalse(config.load({**base, "CCTV_UPDATE_CHECK": off}).update_check)
        self.assertTrue(config.load({**base, "CCTV_UPDATE_CHECK": "1"}).update_check)
        self.assertEqual("http://127.0.0.1:9/x",
                         config.load({**base, "CCTV_UPDATE_URL": "http://127.0.0.1:9/x"}).update_url)


class HelpTest(Base):
    async def test_help_in_both_modes(self) -> None:
        where = await self.claim()
        await self.choose(where, 1)
        topics = self.bot.help_text(OWNER)
        self.assertIn(self.t("help.map_topics"), topics)
        self.assertIn(self.t("help.card_topics"), topics)
        self.assertIn("/setup — ", topics)
        self.assertNotIn("/help — ", topics)
        self.assertIn("/version — " + self.t("command.version"), topics)
        self.assertIn(self.t("help.version", version=__version__), topics)
        self.assertNotIn(self.t("help.owner_only"), topics)
        self.bot.routes.set_preset("flat")
        flat = self.bot.help_text(FRIEND)
        self.assertIn(self.t("help.map_flat"), flat)
        self.assertIn(self.t("help.card_flat"), flat)
        self.assertNotIn("/setup — ", flat)
        self.assertIn(self.t("help.owner_only"), flat)

    async def test_command_menu_lists_help_and_version(self) -> None:
        await self.bot.set_command_menu()
        names = [name for name, _ in self.tg.of("set_my_commands")[0]["commands"]]
        self.assertIn("help", names)
        self.assertIn("version", names)


class EmptyAddTest(Base):
    async def test_empty_scan_names_the_reason_and_the_way_out(self) -> None:
        for mode in (0, 1):
            with self.subTest("here" if mode == 0 else "group"):
                self.setUp()
                where = await self.claim()
                await self.choose(where, mode)
                if mode == 1:
                    await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
                await self.bot._discover()
                message = self.tg.of("send_message")[-1]
                self.assertIn(self.t("scan.none"), message["text"])
                self.assertIn(self.t("scan.none_hint", networks="192.0.2.0/24"), message["text"])
                self.assertIn("CCTV_DISCOVERY_NETWORKS", message["text"])
                labels = [b.text for row in message["reply_markup"].inline_keyboard for b in row]
                self.assertIn(self.t("add.manual_button"), labels)

    async def test_only_known_cameras_keep_the_short_hint(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        self.scan["candidates"] = [{"host": "192.0.2.5", "registered_camera_id": "gate"}]
        await self.bot._discover()
        text = self.tg.of("send_message")[-1]["text"]
        self.assertIn(self.t("add.manual_hint"), text)
        self.assertNotIn("CCTV_DISCOVERY_NETWORKS", text)


class AddressTest(Base):
    async def test_password_in_the_path_is_refused_like_userinfo(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        for address in ("rtsp://192.0.2.30:554/user=admin&password=x&channel=1&stream=0.sdp",
                        "rtsp://admin:x@192.0.2.30/stream1",
                        "rtsp://192.0.2.30/live?PWD=x"):
            with self.subTest(address):
                self.assertEqual(self.t("add.no_password_in_url"),
                                 await self.bot.on_add(OWNER, [address], chat_id=OWNER))
        self.assertEqual([], [p for p, _ in self.sent if p == "/v1/discovery/probes"])

    async def test_found_path_is_shown(self) -> None:
        where = await self.claim()
        await self.choose(where, 0)
        await self.bot.on_add(OWNER, ["192.0.2.30"], chat_id=OWNER)
        answer = await self.bot.on_text(OWNER, None, "admin s3cr3t", None, 555, chat_id=OWNER)
        self.assertNotIn("s3cr3t", answer)
        self.assertIn(self.t("detected.path", path="/user=admin&password=***&channel=1&stream=0.sdp",
                             template="Xiongmai/XMEye"), answer)
        self.assertIn("stream=1.sdp", answer)  # второй поток — под детектор


if __name__ == "__main__":
    unittest.main()
