"""Мастер /add: активация новых Hikvision — кнопки, подтверждение словом, пароль.

Мост здесь — подставной (httpx.MockTransport): проверяется то, за что отвечает
бот. Сообщение с паролем стирается раньше обращения к мосту; пароль из моста
уходит владельцу в личку ровно один раз и никогда — в группу; при частичном
сбое пакета итог есть по каждой камере, удачные записываются в реестр.
Протокол и камеры — в tests/engine/test_hikvision_activation_20261003.py.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import httpx

from cctv.bot import bot as bot_module
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER
from test_cctv_provisioning_20260904 import DeletingTelegram

PASSWORD = "Kamera-2026"
GENERATED = "Gx7-Lq2_Rt9.Pz"
HOSTS = ["192.0.2.64", "192.0.2.65", "192.0.2.66"]


class FailingDmTelegram(DeletingTelegram):
    async def send_message(self, **kwargs):
        if "message_thread_id" not in kwargs and kwargs.get("chat_id") == OWNER:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        return await super().send_message(**kwargs)


class ActivationWizardTest(unittest.IsolatedAsyncioTestCase):
    telegram = DeletingTelegram

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.sent: list[tuple[str, dict]] = []
        self.scan_status = {"ok": True, "scan_id": "s1", "status": "done", "networks": ["192.0.2.0/24"],
                            "candidates": [
                                {"host": "192.0.2.11", "ports": [80, 554], "vendor": "hikvision",
                                 "onvif": True, "label": "192.0.2.11 · hikvision",
                                 "activated": True, "activation": "v3"},
                                {"host": HOSTS[0], "ports": [80, 554], "vendor": "hikvision",
                                 "onvif": False, "label": f"{HOSTS[0]} · hikvision",
                                 "activated": False, "activation": "v3"},
                                {"host": HOSTS[1], "ports": [80], "vendor": "hikvision",
                                 "onvif": False, "label": f"{HOSTS[1]} · hikvision",
                                 "activated": False, "activation": "legacy"}]}
        self.start_reply: dict = {"ok": True, "activation_id": "a1", "status": "running",
                                  "hosts": HOSTS}
        self.job = {"ok": True, "activation_id": "a1", "status": "done", "hosts": HOSTS,
                    "generated": False, "password": PASSWORD, "vault": True, "results": [
                        {"host": HOSTS[0], "outcome": "activated", "stage": "", "onvif": True,
                         "stream": True, "protocol": "v3", "model": "DS-2CD2543G2-IS",
                         "probe_token": "tok-64",
                         "summary": {"host": HOSTS[0], "model": "DS-2CD2543G2-IS"}},
                        {"host": HOSTS[1], "outcome": "unverified", "stage": "login_refused",
                         "onvif": False, "stream": False, "protocol": "legacy"},
                        {"host": HOSTS[2], "outcome": "failed", "stage": "activate_refused",
                         "onvif": False, "stream": False, "protocol": "v3"}]}

        def handler(request: httpx.Request):
            path = request.url.path
            body = json.loads(request.content) if request.content else {}
            self.sent.append((path, body))
            if path == "/v1/cameras" and request.method == "GET":
                return httpx.Response(200, json={"cameras": []})
            if path == "/v1/discovery/scans" and request.method == "POST":
                return httpx.Response(200, json={"ok": True, "scan_id": "s1", "status": "running"})
            if path.startswith("/v1/discovery/scans/"):
                return httpx.Response(200, json=self.scan_status)
            if path == "/v1/activations":
                self.tg.calls.append(("bridge_activation", {}))  # порядок: удаление → мост
                return httpx.Response(200, json=self.start_reply)
            if path.startswith("/v1/activations/"):
                reply = dict(self.job)
                self.job.pop("password", None)  # мост отдаёт пароль один раз
                return httpx.Response(200, json=reply)
            if path == "/v1/cameras" and request.method == "POST":
                return httpx.Response(200, json={"ok": True, "action": "added",
                                                 "camera_id": body.get("camera_id")})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = self.telegram()
        self.logged: list[str] = []
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg,
                           log=self.logged.append)
        for name, value in (("RESTART_WAIT_SEC", 600), ("SCAN_POLL_SEC", 0),
                            ("ACTIVATION_POLL_SEC", 0)):
            self.addCleanup(setattr, bot_module, name, getattr(bot_module, name))
            setattr(bot_module, name, value)

    def button(self, action: str, payload: str) -> str:
        return f"cv:{action}:{self.state.issue_callback(bot_module.CONSOLE_CAMERA, action, 600, payload)}"

    def group_texts(self) -> str:
        """Всё, что ушло в группу (не в личку владельцу)."""
        return json.dumps([kw for name, kw in self.tg.calls if kw.get("chat_id") != OWNER],
                          ensure_ascii=False, default=str)

    def dms(self) -> list[str]:
        return [kw["text"] for name, kw in self.tg.calls
                if name == "send_message" and kw.get("chat_id") == OWNER]

    def activation_posts(self) -> list[dict]:
        return [body for path, body in self.sent if path == "/v1/activations"]

    async def start(self, thread: int, word: str = "активировать") -> str:
        await self.bot.on_callback(OWNER, thread, self.button("actall", ",".join(HOSTS)))
        return await self.bot.on_text(OWNER, thread, word, None, 500)

    # --- поиск -------------------------------------------------------------
    async def test_scan_offers_activation_instead_of_credentials(self) -> None:
        await self.bot.ensure_console()
        await self.bot._discover()
        listed = self.tg.of("send_message")[-1]
        self.assertIn("Новых Hikvision без пароля (не активированы): 2", listed["text"])
        self.assertIn("Найдено новых камер: 1", listed["text"])
        labels = [b.text for row in listed["reply_markup"].inline_keyboard for b in row]
        self.assertIn("🔐 Активировать все (2)", labels)
        self.assertIn(f"🔐 Активировать {HOSTS[0]} · hikvision", labels)
        all_button = next(b for row in listed["reply_markup"].inline_keyboard for b in row
                          if b.text == "🔐 Активировать все (2)")
        token = all_button.callback_data.split(":", 2)[2]
        self.assertEqual((bot_module.CONSOLE_CAMERA, "actall", f"{HOSTS[0]},{HOSTS[1]}"),
                         self.state.resolve_callback(token))
        # Неактивированные не предлагаются кнопкой «логин и пароль».
        cand = [b for row in listed["reply_markup"].inline_keyboard for b in row
                if b.callback_data.startswith("cv:cand:")]
        self.assertEqual(["192.0.2.11 · hikvision"], [b.text for b in cand])

    async def test_only_inactive_cameras_still_get_buttons(self) -> None:
        self.scan_status["candidates"] = self.scan_status["candidates"][1:]
        await self.bot.ensure_console()
        await self.bot._discover()
        listed = self.tg.of("send_message")[-1]
        labels = [b.text for row in listed["reply_markup"].inline_keyboard for b in row]
        self.assertIn("🔐 Активировать все (2)", labels)
        self.assertIn("🔄 Искать снова", labels)

    # --- подтверждение и пароль -----------------------------------------------
    async def test_confirmation_word_is_required(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        asked = await self.bot.on_callback(OWNER, thread, self.button("actall", ",".join(HOSTS)))
        self.assertIn("необратимо", asked)
        self.assertIn("«активировать»", asked)
        answer = await self.bot.on_text(OWNER, thread, "да", None, 501)
        self.assertIn("отменена", answer)
        self.assertEqual([], self.activation_posts())
        self.assertIsNone(self.state.take_input(OWNER))

    async def test_full_batch_with_partial_failure(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        asked = await self.start(thread)
        self.assertIn("8–16", asked)
        self.assertIn("сгенерировать", asked)
        started = await self.bot.on_text(OWNER, thread, PASSWORD, None, 4242)
        self.assertIn("Активирую камер: 3", started)
        # Сообщение с паролем стёрто ДО обращения к мосту.
        names = [name for name, _ in self.tg.calls]
        self.assertLess(names.index("delete_message"), names.index("bridge_activation"))
        self.assertEqual([4242], self.tg.deleted)
        self.assertEqual([{"hosts": HOSTS, "password": PASSWORD}], self.activation_posts())

        await self.bot._activation_task
        # Пароль — в личку владельцу, один раз; в группе его нет нигде.
        dms = self.dms()
        self.assertEqual(1, len(dms))
        self.assertIn(PASSWORD, dms[0])
        self.assertIn(HOSTS[0], dms[0])
        self.assertIn(HOSTS[1], dms[0])  # «не проверено» — пароль мог примениться
        self.assertNotIn(HOSTS[2], dms[0])
        self.assertNotIn(PASSWORD, self.group_texts())
        self.assertNotIn(PASSWORD, " ".join(self.logged))
        # Итог по каждой камере.
        summary = self.tg.of("send_message")[-1]["text"]
        self.assertIn("готово 1 из 3", summary)
        self.assertIn(f"✅ {HOSTS[0]}", summary)
        self.assertIn("hik-192-0-2-64", summary)
        self.assertIn(f"⚠️ {HOSTS[1]}", summary)
        self.assertIn("Повторов не было", summary)
        self.assertIn(f"❌ {HOSTS[2]}", summary)
        self.assertIn("отправлен владельцу в личку", summary)
        # В реестр — только активированная с пробой потока.
        added = [body for path, body in self.sent if path == "/v1/cameras" and body]
        self.assertEqual([{"camera_id": "hik-192-0-2-64", "title": f"DS-2CD2543G2-IS {HOSTS[0]}",
                           "site": f"DS-2CD2543G2-IS {HOSTS[0]}", "probe_token": "tok-64"}], added)

    async def test_generate_word_lets_the_engine_make_the_password(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        await self.start(thread)
        await self.bot.on_text(OWNER, thread, "Сгенерировать", None, 600)
        self.assertEqual([{"hosts": HOSTS, "generate": True}], self.activation_posts())
        self.job["password"] = GENERATED
        await self.bot._activation_task
        self.assertEqual(1, len(self.dms()))
        self.assertIn(GENERATED, self.dms()[0])
        self.assertNotIn(GENERATED, self.group_texts())

    async def test_english_words_work_too(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        await self.start(thread, word="ACTIVATE")
        await self.bot.on_text(OWNER, thread, "generate", None, 601)
        self.assertEqual([{"hosts": HOSTS, "generate": True}], self.activation_posts())

    async def test_weak_password_is_deleted_and_asked_again(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        await self.start(thread)
        self.start_reply = {"ok": False, "error_code": "password_weak"}
        answer = await self.bot.on_text(OWNER, thread, "abcdefghij", None, 700)
        self.assertEqual([700], self.tg.deleted)
        self.assertIn("Слишком простой", answer)
        self.assertNotIn("abcdefghij", answer)
        self.assertEqual((bot_module.CONSOLE_CAMERA, f"actpw|{','.join(HOSTS)}"),
                         self.state.take_input(OWNER))

    async def test_nothing_activated_no_password_anywhere(self) -> None:
        self.job.pop("password")
        for result in self.job["results"]:
            result.update(outcome="failed", stage="activate_refused")
        thread = (await self.bot.ensure_console()).thread_id
        await self.start(thread)
        await self.bot.on_text(OWNER, thread, PASSWORD, None, 800)
        await self.bot._activation_task
        self.assertEqual([], self.dms())
        summary = self.tg.of("send_message")[-1]["text"]
        self.assertIn("готово 0 из 3", summary)
        self.assertNotIn("личк", summary)

    async def test_busy_engine(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        await self.start(thread)
        self.start_reply = {"ok": False, "error_code": "busy"}
        self.assertIn("другая активация", await self.bot.on_text(OWNER, thread, PASSWORD, None, 9))
        self.assertEqual([9], self.tg.deleted)

    async def test_stranger_cannot_activate(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        answer = await self.bot.on_callback(12345, thread, self.button("actall", ",".join(HOSTS)))
        self.assertEqual([], self.activation_posts())
        self.assertIsNone(self.state.take_input(12345))
        self.assertNotIn("необратимо", answer)


class DmFailureTest(ActivationWizardTest):
    """Личка не дошла: пароль не уходит в группу, владелец знает, где он."""

    telegram = FailingDmTelegram

    async def test_full_batch_with_partial_failure(self) -> None:
        thread = (await self.bot.ensure_console()).thread_id
        await self.start(thread)
        await self.bot.on_text(OWNER, thread, PASSWORD, None, 4242)
        await self.bot._activation_task
        summary = self.tg.of("send_message")[-1]["text"]
        self.assertIn("activation-vault.json", summary)
        self.assertNotIn(PASSWORD, self.group_texts())

    async def test_generate_word_lets_the_engine_make_the_password(self) -> None:
        pass

    async def test_nothing_activated_no_password_anywhere(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
