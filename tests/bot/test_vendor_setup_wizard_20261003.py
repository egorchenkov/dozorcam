"""Мастер /add: новая камера марки без автоактивации — инструкция, а не тупик.

Мост — подставной (httpx.MockTransport), кандидаты с activation="manual" — как
их отдаёт опрос (tests/engine/test_vendor_setup_20261003.py). Бот показывает
по каждой марке инструкцию, как задать первый пароль вручную, кнопку «пароль
уже задан» на камеру (обычный путь логина) и не предлагает активацию, которую
не умеет. Hikvision-активация рядом работает как раньше.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import httpx

from cctv import i18n
from cctv.bot import bot as bot_module
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER
from test_cctv_provisioning_20260904 import DeletingTelegram


def candidate(host: str, brand: str, *, activated=None, activation="manual", ports=(80,)):
    label = f"{host} · {brand}" if brand else host
    return {"host": host, "ports": list(ports), "vendor": brand, "brand": brand,
            "onvif": False, "label": label, "activated": activated, "activation": activation}


class VendorSetupWizardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.candidates: list[dict] = []

        def handler(request: httpx.Request):
            path = request.url.path
            if path == "/v1/cameras" and request.method == "GET":
                return httpx.Response(200, json={"cameras": []})
            if path == "/v1/discovery/scans" and request.method == "POST":
                return httpx.Response(200, json={"ok": True, "scan_id": "s1", "status": "running"})
            if path.startswith("/v1/discovery/scans/"):
                return httpx.Response(200, json={"ok": True, "scan_id": "s1", "status": "done",
                                                 "networks": ["192.0.2.0/24"],
                                                 "candidates": self.candidates})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = DeletingTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg,
                           log=lambda _: None)
        self.addCleanup(setattr, bot_module, "SCAN_POLL_SEC", bot_module.SCAN_POLL_SEC)
        bot_module.SCAN_POLL_SEC = 0

    async def discover(self) -> list[dict]:
        await self.bot.ensure_console()
        before = len(self.tg.of("send_message"))
        await self.bot._discover()
        return self.tg.of("send_message")[before:]

    @staticmethod
    def buttons(message: dict) -> list:
        return [b for row in message["reply_markup"].inline_keyboard for b in row]

    async def test_unsupported_vendor_gets_instruction_not_dead_end(self) -> None:
        self.candidates = [candidate("192.0.2.30", "dahua"),
                           candidate("192.0.2.31", "axis", activated=False, ports=(80, 554))]
        posted = [m for m in await self.discover() if "Искать" not in m["text"]
                  or "настройки" in m["text"]]
        message = next(m for m in posted if "ждут первичной настройки" in m["text"])
        text = message["text"]
        self.assertIn("ждут первичной настройки: 2", text)
        self.assertIn("Dahua: 192.0.2.30", text)
        self.assertIn("Axis: 192.0.2.31", text)
        self.assertIn(i18n.t("setup.dahua", "ru"), text)
        self.assertIn(i18n.t("setup.axis", "ru"), text)
        labels = [b.text for b in self.buttons(message)]
        # Никакой «🔐 Активировать» — бот этого не умеет для этих марок.
        self.assertFalse([label for label in labels if label.startswith("🔐")])
        self.assertIn("🔄 Искать снова", labels)
        # Кнопка камеры ведёт в обычный путь «логин и пароль».
        cand = [b for b in self.buttons(message) if b.callback_data.startswith("cv:cand:")]
        self.assertEqual(2, len(cand))
        token = cand[0].callback_data.split(":", 2)[2]
        self.assertEqual((bot_module.CONSOLE_CAMERA, "cand", "192.0.2.30"),
                         self.state.resolve_callback(token))
        asked = await self.bot.on_callback(OWNER, await self.bot.ensure_console(),
                                           cand[0].callback_data)
        self.assertIn("192.0.2.30", asked)
        # И «Камер не нашлось» при этом не говорится.
        self.assertFalse([m for m in posted if "не нашлось" in m["text"]])

    async def test_mixed_with_hikvision_activation_and_regular_camera(self) -> None:
        self.candidates = [
            candidate("192.0.2.11", "hikvision", activated=True, activation="v3", ports=(80, 554)),
            candidate("192.0.2.64", "hikvision", activated=False, activation="v3"),
            candidate("192.0.2.40", "reolink"),
        ]
        posted = await self.discover()
        manual = next(m for m in posted if "ждут первичной настройки" in m["text"])
        self.assertIn("Reolink: 192.0.2.40", manual["text"])
        self.assertNotIn("192.0.2.64", manual["text"])
        listing = posted[-1]
        self.assertIn("Найдено новых камер: 1", listing["text"])
        labels = [b.text for b in self.buttons(listing)]
        self.assertIn("🔐 Активировать все (1)", labels)
        self.assertNotIn("192.0.2.40", " ".join(labels))
        # «Искать снова» — один раз, в основном списке.
        self.assertNotIn("🔄 Искать снова", [b.text for b in self.buttons(manual)])

    async def test_unknown_brand_falls_back_to_generic_instruction(self) -> None:
        self.candidates = [candidate("192.0.2.50", "", activated=False),
                           candidate("192.0.2.51", "noname-oem")]
        posted = await self.discover()
        message = next(m for m in posted if "ждут первичной настройки" in m["text"])
        self.assertIn(i18n.t("setup.generic", "ru"), message["text"])
        self.assertIn("192.0.2.50, 192.0.2.51", message["text"])
        self.assertEqual(1, message["text"].count(i18n.t("setup.generic", "ru")))

    async def test_english_instruction(self) -> None:
        self.state.set_service(bot_module.LANG_KEY, "en")
        self.candidates = [candidate("192.0.2.60", "uniview")]
        posted = await self.discover()
        message = next(m for m in posted if "Uniview: 192.0.2.60" in m["text"])
        self.assertIn(i18n.t("setup.uniview", "en"), message["text"])

    def test_every_brand_has_instruction_in_both_languages(self) -> None:
        for lang in ("en", "ru"):
            catalog = json.loads((i18n.LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
            for brand in bot_module.MANUAL_SETUP_BRANDS + ("generic",):
                with self.subTest(lang=lang, brand=brand):
                    self.assertTrue(catalog.get(f"setup.{brand}"))


if __name__ == "__main__":
    unittest.main()
