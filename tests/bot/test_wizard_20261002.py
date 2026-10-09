"""Э4: мастер первого запуска — код владельца, группа, /add, язык.

Путь с нуля: в конфиге нет ни группы, ни allow-list. Владелец назначается
одноразовым кодом из журнала, группа — добавлением бота, пульт и темы бот
заводит сам; /add принимает и адрес потока для камеры вне поиска.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

import httpx

from cctv import i18n
from cctv.bot import bot as bot_module
from cctv.bot.bot import CctvBot, resolve_lang
from cctv.bot.bridge import Bridge, Storage
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_provisioning_20260904 import DeletingTelegram

OWNER = 4242
STRANGER = 99
GROUP = -1001234567890


class WizardTelegram(DeletingTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.forum = True
        self.member = SimpleNamespace(status="administrator", can_manage_topics=True,
                                      can_delete_messages=True, can_pin_messages=True)

    async def get_chat(self, **kwargs):
        self._record("get_chat", kwargs)
        return SimpleNamespace(is_forum=self.forum)

    async def get_me(self):
        return SimpleNamespace(id=1, username="ExampleCctvBot")

    async def unpin_chat_message(self, **kwargs):
        self._record("unpin_chat_message", kwargs)

    async def get_chat_member(self, **kwargs):
        self._record("get_chat_member", kwargs)
        return self.member

    async def set_my_commands(self, commands, language_code=None):
        self._record("set_my_commands", {"language_code": language_code,
                                         "commands": [(c.command, c.description) for c in commands]})

    async def delete_my_commands(self, language_code=None):
        self._record("delete_my_commands", {"language_code": language_code})


class WizardTest(unittest.IsolatedAsyncioTestCase):
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
                self.tg.calls.append(("bridge_probe", {}))  # порядок: удаление → мост
                return httpx.Response(200, json={"ok": True, "probe_token": "tok", "summary": {
                    "host": "127.0.0.1", "source": "manual", "verified": True,
                    "main_url": "rtsp://***@127.0.0.1:28680/replay"}})
            if request.url.path == "/v1/media-requests":
                return httpx.Response(202, json={"status": "accepted"})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = WizardTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    def t(self, key: str, **params) -> str:
        return i18n.t(key, self.bot.lang, **params)

    async def claim(self, language_code: str = "ru") -> str:
        code = self.bot.setup_code()
        return await self.bot.on_start(OWNER, OWNER, "private", [code], language_code)

    # --- владелец --------------------------------------------------------
    async def test_code_is_stable_until_used_and_single_use(self) -> None:
        code = self.bot.setup_code()
        self.assertRegex(code, r"^[A-Z2-9]{10}$")
        self.assertEqual(code, self.bot.setup_code())  # перезапуск печатает тот же код
        self.assertFalse(self.bot.allowed(OWNER))
        answer = await self.bot.on_start(OWNER, OWNER, "private", [code.lower()], "ru")
        self.assertTrue(self.bot.allowed(OWNER))
        self.assertEqual("ru", self.bot.lang)  # язык — по language_code владельца
        # С 0.3.0 «владелец назначен» приходит одним сообщением с шагом «куда присылать».
        self.assertIsNone(answer)
        self.assertIn(i18n.t("wizard.owner_set", "ru"), self.tg.of("send_message")[-1]["text"])
        self.assertIsNone(self.bot.setup_code())
        # Тот же код второй раз владельца не меняет.
        await self.bot.on_start(STRANGER, STRANGER, "private", [code], "en")
        self.assertFalse(self.bot.allowed(STRANGER))

    async def test_wrong_code_and_brute_force(self) -> None:
        self.bot.setup_code()
        answer = await self.bot.on_start(STRANGER, STRANGER, "private", ["WRONGCODE1"], "en")
        self.assertEqual(i18n.t("wizard.bad_code", "en"), answer)
        for _ in range(bot_module.SETUP_CODE_ATTEMPTS):
            await self.bot.on_start(STRANGER, STRANGER, "private", ["WRONGCODE1"], "en")
        # Исчерпал попытки — даже верный код больше не принимается.
        code = self.state.get_service(bot_module.SETUP_CODE_KEY)
        self.assertIsNone(await self.bot.on_start(STRANGER, STRANGER, "private", [code], "en"))
        self.assertFalse(self.bot.allowed(STRANGER))
        self.assertEqual(i18n.t("wizard.need_code", "en"),
                         await self.bot.on_start(OWNER, OWNER, "private", [], "en"))

    async def test_configured_allow_list_disables_the_code(self) -> None:
        self.bot.cfg = dataclasses.replace(self.cfg, allowed_user_ids=frozenset({7}))
        self.assertIsNone(self.bot.setup_code())

    # --- группа ----------------------------------------------------------
    async def test_adding_bot_to_group_creates_console(self) -> None:
        await self.claim()
        await self.bot.on_bot_membership(GROUP, "supergroup", OWNER, "administrator")
        self.assertEqual(GROUP, self.bot.chat_id)
        created = self.tg.of("create_forum_topic")
        self.assertEqual([i18n.t("console.title", "ru")], [c["name"] for c in created])
        texts = [m["text"] for m in self.tg.of("send_message")]
        self.assertIn(i18n.t("wizard.group_ready", "ru"), texts)
        self.assertIn(i18n.t("wizard.console_ready", "ru"), texts)
        # Повтор (/setup) не плодит второй пульт.
        self.assertEqual(i18n.t("wizard.group_already", "ru"),
                         await self.bot.bind_group(GROUP, OWNER))
        self.assertEqual(1, len(self.tg.of("create_forum_topic")))

    async def test_stranger_cannot_bind_group(self) -> None:
        await self.claim()
        await self.bot.on_bot_membership(GROUP, "supergroup", STRANGER, "member")
        self.assertIsNone(self.bot.chat_id)
        self.assertEqual([], self.tg.of("create_forum_topic"))

    async def test_group_without_topics_and_rights_gets_instructions(self) -> None:
        await self.claim("en")
        self.tg.forum = False
        self.tg.member = SimpleNamespace(status="member")
        await self.bot.on_bot_membership(GROUP, "group", OWNER, "member")
        text = self.tg.of("send_message")[-1]["text"]
        self.assertIn(i18n.t("wizard.need_forum", "en"), text)
        self.assertIn(i18n.t("wizard.need_admin", "en"), text)
        self.assertEqual([], self.tg.of("create_forum_topic"))
        # Включили темы и повысили бота — повышение само доводит настройку.
        self.tg.forum = True
        self.tg.member = SimpleNamespace(status="administrator", can_manage_topics=True,
                                         can_delete_messages=False)
        await self.bot.on_chat_migrated(GROUP, GROUP - 1)
        self.assertEqual(GROUP - 1, self.bot.chat_id)
        await self.bot.on_bot_membership(GROUP - 1, "supergroup", OWNER, "administrator")
        self.assertIn(i18n.t("wizard.need_delete_right", "en"), self.tg.of("send_message")[-1]["text"])
        self.tg.member.can_delete_messages = True
        # Без «Закрепления» панели камер не закрепить — это тоже просьба, а не молчание
        # (прогон на чистой VM 09.10.2026: бот без права, тема камеры падала на pin).
        self.tg.member.can_pin_messages = False
        await self.bot.on_bot_membership(GROUP - 1, "supergroup", OWNER, "administrator")
        self.assertIn(i18n.t("wizard.need_pin_right", "en"), self.tg.of("send_message")[-1]["text"])
        self.assertEqual([], self.tg.of("create_forum_topic"))
        self.tg.member.can_pin_messages = True
        await self.bot.on_bot_membership(GROUP - 1, "supergroup", OWNER, "administrator")
        self.assertEqual(1, len(self.tg.of("create_forum_topic")))

    async def test_camera_topic_survives_missing_pin_right(self) -> None:
        await self.claim()
        await self.bot.bind_group(GROUP, OWNER)

        async def no_pin(**kwargs):
            raise RuntimeError("not enough rights to pin a message")

        self.tg.pin_chat_message = no_pin
        self.cameras = [{"camera_id": "replay", "title": "Стенд", "site": "", "status": "online",
                         "last_frame_at": None, "motion": {"state": "watching"}}]
        await self.bot.sync_registry()
        self.assertIsNotNone(self.state.topic_for("replay"))
        self.assertIsNotNone(self.state.panel_for("replay"))

    async def test_other_group_is_refused(self) -> None:
        await self.claim()
        await self.bot.bind_group(GROUP, OWNER)
        self.assertEqual(i18n.t("wizard.other_group", "ru"), await self.bot.bind_group(-1, OWNER))

    async def test_nothing_touches_telegram_before_group(self) -> None:
        await self.bot.sync_registry()
        await self.bot.refresh_console()
        self.assertIsNone(await self.bot.ensure_console())
        self.assertEqual([], self.tg.calls)

    # --- /add --------------------------------------------------------------
    async def test_add_by_address_deletes_password_and_probes_bridge(self) -> None:
        await self.claim()
        await self.bot.bind_group(GROUP, OWNER)
        console = (await self.bot.ensure_console()).thread_id
        answer = await self.bot.on_add(OWNER, ["rtsp://127.0.0.1:28680/replay",
                                               "rtsp://127.0.0.1:28680/replay-detect"])
        self.assertIn("rtsp://127.0.0.1:28680/replay", answer)
        # Просьба уходит одним ответом на /add, без второй копии в тему (прогон на VM 09.10).
        self.assertNotIn(answer, [m["text"] for m in self.tg.of("send_message")])
        reply = await self.bot.on_text(OWNER, console, "stand s3cr3t", None, 555)
        self.assertEqual([555], self.tg.deleted)  # удалено до запроса к мосту
        order = [name for name, _ in self.tg.calls]
        self.assertLess(order.index("delete_message"), order.index("bridge_probe"))
        probe = [body for path, body in self.sent if path == "/v1/discovery/probes"]
        self.assertEqual("rtsp://127.0.0.1:28680/replay-detect", probe[0]["detect_url"])
        self.assertNotIn("s3cr3t", reply)
        self.assertNotIn("s3cr3t", json.dumps(self.tg.calls, default=str))
        pending = self.state.take_input(OWNER)
        self.assertEqual("name|tok|rtsp://127.0.0.1:28680/replay", pending[1])

    async def test_address_with_password_inside_is_refused(self) -> None:
        await self.claim("en")
        await self.bot.bind_group(GROUP, OWNER)
        answer = await self.bot.on_add(OWNER, ["rtsp://u:p@127.0.0.1/x"])
        self.assertEqual(i18n.t("add.no_password_in_url", "en"), answer)
        self.assertIsNone(self.state.take_input(OWNER))

    async def test_add_before_group_points_to_the_group_step(self) -> None:
        await self.claim("en")
        # Шаг «куда присылать» не пройден — /add отсылает к нему, а выбрана группа — к группе.
        self.assertEqual(i18n.t("wizard.where_first", "en"), await self.bot.on_add(OWNER, []))
        self.bot.routes.set_preset("camera")
        self.assertEqual(i18n.t("wizard.add_to_group", "en"), await self.bot.on_add(OWNER, []))
        self.assertEqual(i18n.t("no_access", "en"), await self.bot.on_add(STRANGER, []))

    async def test_first_frame_is_requested_when_camera_goes_online(self) -> None:
        await self.claim()
        await self.bot.bind_group(GROUP, OWNER)
        self.cameras = [{"camera_id": "replay", "title": "Стенд", "site": "", "status": "online",
                         "last_frame_at": None, "motion": {"state": "watching"}}]
        await self.bot.sync_registry()
        await self.bot.first_frame("replay")
        requests = [body for path, body in self.sent if path == "/v1/media-requests"]
        self.assertEqual(["snapshot"], [r["kind"] for r in requests])
        self.assertIn(i18n.t("add.first_frame", "ru", title="Стенд"),
                      [m["text"] for m in self.tg.of("send_message")])

    # --- язык --------------------------------------------------------------
    async def test_language_switch_by_command(self) -> None:
        await self.claim("ru")
        answer = await self.bot.set_language(OWNER, ["en"])
        self.assertEqual("en", self.bot.lang)
        self.assertEqual(i18n.t("lang.changed", "en", lang="en"), answer)
        self.assertEqual(i18n.t("lang.unknown", "en", available=", ".join(i18n.available())),
                         await self.bot.set_language(OWNER, ["xx"]))
        await self.bot.set_language(OWNER, ["pt-br"])
        self.assertEqual("pt-BR", self.bot.lang)
        self.assertEqual(i18n.t("no_access", "pt-BR"), await self.bot.set_language(STRANGER, ["ru"]))

    def menu(self, lang: str) -> list[tuple[str, str]]:
        return [(name, i18n.t(f"command.{name}", lang)) for name in bot_module.MENU_COMMANDS]

    async def test_command_menu_follows_explicit_language(self) -> None:
        # Явного языка нет — меню по языку клиента: общее en, ru — для русского клиента.
        await self.bot.set_command_menu()
        self.assertEqual([{"language_code": None, "commands": self.menu("en")},
                          {"language_code": "ru", "commands": self.menu("ru")}],
                         self.tg.of("set_my_commands"))
        self.assertEqual([], self.tg.of("delete_my_commands"))
        # lang = "ru" в конфиге — общее меню русское, список под ru снят: его видят все.
        self.tg.calls.clear()
        self.bot.cfg = dataclasses.replace(self.cfg, lang="ru")
        await self.bot.set_command_menu()
        self.assertEqual([{"language_code": None, "commands": self.menu("ru")}],
                         self.tg.of("set_my_commands"))
        self.assertEqual([{"language_code": "ru"}], self.tg.of("delete_my_commands"))
        # /lang сильнее конфига и сразу меняет меню.
        self.tg.calls.clear()
        await self.claim()
        self.tg.calls.clear()
        await self.bot.set_language(OWNER, ["en"])
        self.assertEqual([{"language_code": None, "commands": self.menu("en")}],
                         self.tg.of("set_my_commands"))
        self.assertEqual([{"language_code": "ru"}], self.tg.of("delete_my_commands"))

    def test_language_code_mapping(self) -> None:
        cases = {"ru": "ru", "en-US": "en", "pt-br": "pt-BR", "pt": "pt-BR", "uk": "uk",
                 "id": "id", "es-419": "es", "de": "en", None: "en", "": "en"}
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(expected, resolve_lang(code))


class StorageThresholdTest(unittest.IsolatedAsyncioTestCase):
    """Дефект Э2/Э3: тревога «мало места» сразу после старта при tmpfs-транзите."""

    async def asyncSetUp(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.tg = WizardTelegram()
        cfg = make_config(tmp)
        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"cameras": []})))
        self.addCleanup(client.close)
        self.bot = CctvBot(cfg, self.state, Bridge(cfg, client=client), self.tg)
        await self.bot.ensure_console()

    async def test_tmpfs_transit_above_engine_threshold_is_quiet(self) -> None:
        mib = 1024 ** 2
        await self.bot.watch_storage(Storage(10 * mib, 1024 * mib, 240 * mib, 64 * mib))
        self.assertEqual([], self.tg.of("send_message"))
        await self.bot.watch_storage(Storage(10 * mib, 1024 * mib, 32 * mib, 64 * mib))
        self.assertEqual(1, len(self.tg.of("send_message")))

    async def test_old_bridge_without_threshold_keeps_two_gib(self) -> None:
        await self.bot.watch_storage(Storage(0, 8 * 1024 ** 3, 1024 ** 3))
        self.assertEqual(1, len(self.tg.of("send_message")))


if __name__ == "__main__":
    unittest.main()
