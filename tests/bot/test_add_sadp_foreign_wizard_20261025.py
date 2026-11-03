"""Мастер /add: новая Hikvision на заводском адресе чужой подсети — в чате.

Мост подставной (как в test_activation_wizard_20261003): проверяется бот.
Кандидат «вне сети» показан с пометкой, моделью и MAC и кнопкой активации;
после слова подтверждения бот спрашивает сеть (DHCP или статика) и только
потом пароль; в мост уходит `network`; итог и запись в реестр — по новому
адресу камеры. Движок и камеры — tests/engine/test_add_sadp_foreign_subnet_20261025.py.
"""
from __future__ import annotations

import unittest

from cctv.bot import bot as bot_module
import test_activation_wizard_20261003 as base  # модулем: иначе pytest соберёт его тесты здесь
from test_cctv_flow_20260824 import OWNER

MAC = "-".join(("00", "00", "5e", "00", "53", "01"))
LINK = "fe80::200:5eff:fe00:5301%eth0"
LINK2 = "fe80::200:5eff:fe00:5302%eth0"


class ForeignSubnetWizardTest(unittest.IsolatedAsyncioTestCase):
    telegram = base.ActivationWizardTest.telegram
    setUp = base.ActivationWizardTest.setUp
    button = base.ActivationWizardTest.button
    group_texts = base.ActivationWizardTest.group_texts
    dms = base.ActivationWizardTest.dms
    activation_posts = base.ActivationWizardTest.activation_posts

    def factory(self, host: str = LINK, mac: str = MAC) -> dict:
        return {"host": host, "ports": [], "vendor": "hikvision", "brand": "hikvision",
                "model": "DS-2CD2043G2-I", "onvif": False,
                "label": "192.168.1.64 · hikvision DS-2CD2043G2-I", "activated": False,
                "activation": "v3", "factory_host": "192.168.1.64", "mac": mac,
                "foreign_subnet": True, "link_local": True}

    def use_factory_job(self, address: str = "192.0.2.50") -> None:
        self.job["results"] = [{"host": LINK, "address": address, "outcome": "activated",
                                "stage": "", "onvif": True, "stream": True, "protocol": "v3",
                                "model": "DS-2CD2043G2-I", "probe_token": "tok-50",
                                "summary": {"host": address, "model": "DS-2CD2043G2-I"}}]
        self.job["hosts"] = [LINK]

    async def start(self, hosts: str = LINK) -> int:
        thread = (await self.bot.ensure_console()).thread_id
        await self.bot.on_callback(OWNER, thread, self.button("actall", hosts))
        return thread

    async def test_scan_shows_foreign_candidate_with_mark_model_and_mac(self) -> None:
        self.scan_status["candidates"] = [self.factory()]
        await self.bot.ensure_console()
        await self.bot._discover()
        listed = self.tg.of("send_message")[-1]
        self.assertIn(f"192.168.1.64 — заводской адрес, чужая подсеть · DS-2CD2043G2-I · MAC {MAC}",
                      listed["text"])
        self.assertIn("IPv6 link-local", listed["text"])
        buttons = {b.text: b for row in listed["reply_markup"].inline_keyboard for b in row}
        one = buttons[f"🔐 Активировать DS-2CD2043G2-I · MAC {MAC}"]
        self.assertEqual((bot_module.CONSOLE_CAMERA, "act", LINK),
                         self.state.resolve_callback(one.callback_data.split(":", 2)[2]))

    async def test_without_link_local_it_gets_manual_setup(self) -> None:
        self.scan_status["candidates"] = [dict(self.factory(host="192.168.1.64"), activation="",
                                               link_local=False)]
        await self.bot.ensure_console()
        await self.bot._discover()
        text = self.tg.of("send_message")[-1]["text"]
        self.assertIn("заводской адрес, чужая подсеть", text)
        self.assertIn("SADP Tool", text)

    async def test_static_network_is_asked_before_password(self) -> None:
        self.use_factory_job()
        thread = await self.start()
        asked = await self.bot.on_text(OWNER, thread, "активировать", None, 10)
        self.assertIn("заводском адресе чужой подсети", asked)
        self.assertIn("«dhcp»", asked)
        # Неразборчивая сеть — переспрос, камеры не тронуты.
        again = await self.bot.on_text(OWNER, thread, "192.0.2.50/24 198.51.100.1", None, 11)
        self.assertIn("Сеть не разобрана", again)
        self.assertEqual([], self.activation_posts())
        asked = await self.bot.on_text(OWNER, thread, "192.0.2.50 255.255.255.0 192.0.2.1", None, 12)
        self.assertIn("8–16", asked)
        await self.bot.on_text(OWNER, thread, base.PASSWORD, None, 13)
        self.assertEqual([13], self.tg.deleted)
        self.assertEqual([{"hosts": [LINK], "password": base.PASSWORD,
                           "network": {"mode": "static", "address": "192.0.2.50", "prefix": 24,
                                       "gateway": "192.0.2.1"}}], self.activation_posts())
        await self.bot._activation_task
        summary = self.tg.of("send_message")[-1]["text"]
        self.assertIn("✅ 192.0.2.50", summary)
        self.assertIn("hik-192-0-2-50", summary)
        added = [body for path, body in self.sent if path == "/v1/cameras" and body]
        self.assertEqual("hik-192-0-2-50", added[0]["camera_id"])
        self.assertIn("192.0.2.50", self.dms()[0])
        self.assertNotIn(base.PASSWORD, self.group_texts())

    async def test_dhcp_for_a_batch(self) -> None:
        thread = await self.start(f"{LINK},{LINK2}")
        await self.bot.on_text(OWNER, thread, "активировать", None, 20)
        refused = await self.bot.on_text(OWNER, thread, "192.0.2.50/24 192.0.2.1", None, 21)
        self.assertIn("Один статический адрес нескольким камерам не задать", refused)
        await self.bot.on_text(OWNER, thread, "DHCP", None, 22)
        await self.bot.on_text(OWNER, thread, "сгенерировать", None, 23)
        self.assertEqual([{"hosts": [LINK, LINK2], "generate": True, "network": {"mode": "dhcp"}}],
                         self.activation_posts())

    async def test_engine_refusing_network_asks_it_again(self) -> None:
        thread = await self.start()
        await self.bot.on_text(OWNER, thread, "activate", None, 30)
        await self.bot.on_text(OWNER, thread, "dhcp", None, 31)
        self.start_reply = {"ok": False, "error_code": "bad_network"}
        answer = await self.bot.on_text(OWNER, thread, base.PASSWORD, None, 32)
        self.assertEqual([32], self.tg.deleted)
        self.assertIn("Сеть не разобрана", answer)
        self.assertEqual((bot_module.CONSOLE_CAMERA, f"actnet|{LINK}"), self.state.take_input(OWNER))

    async def test_weak_password_keeps_the_network(self) -> None:
        thread = await self.start()
        await self.bot.on_text(OWNER, thread, "активировать", None, 40)
        await self.bot.on_text(OWNER, thread, "dhcp", None, 41)
        self.start_reply = {"ok": False, "error_code": "password_weak"}
        await self.bot.on_text(OWNER, thread, "abcdefghij", None, 42)
        self.assertEqual((bot_module.CONSOLE_CAMERA, f"actpw|{LINK};dhcp"),
                         self.state.take_input(OWNER))

    async def test_ipv4_cameras_skip_the_network_step(self) -> None:
        thread = await self.start("192.0.2.64")
        asked = await self.bot.on_text(OWNER, thread, "активировать", None, 50)
        self.assertIn("8–16", asked)

    def test_network_parsing(self) -> None:
        words = {"dhcp"}
        parse = bot_module.network_from_text
        self.assertEqual("dhcp", parse(" DHCP ", words))
        self.assertEqual("192.0.2.50/24@192.0.2.1", parse("192.0.2.50/24 192.0.2.1", words))
        self.assertEqual("192.0.2.50/24@192.0.2.1", parse("192.0.2.50, 255.255.255.0, 192.0.2.1", words))
        for bad in ("", "192.0.2.50", "192.0.2.50/24 198.51.100.1", "192.0.2.255/24 192.0.2.1",
                    "192.0.2.1/24 192.0.2.1", "8.8.8.8/24 8.8.8.1", "192.0.2.50/32 192.0.2.50",
                    "a b c"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse(bad, words))
        self.assertEqual({"mode": "static", "address": "192.0.2.50", "prefix": 24,
                          "gateway": "192.0.2.1"}, bot_module.network_request("192.0.2.50/24@192.0.2.1"))
        self.assertEqual({"mode": "dhcp"}, bot_module.network_request("dhcp"))


if __name__ == "__main__":
    unittest.main()
