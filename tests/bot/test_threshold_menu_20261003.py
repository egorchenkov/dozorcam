#!/usr/bin/env python3
"""«🎯 Пороги»: порог детектора по камерам в боте — сквозь настоящий мост до движка.

Бот, HTTP-мост движка, ModelManager и ThresholdManager — настоящие; подменены Bot
API и детекторы (метки с заданной оценкой). Сама калибровка — в
tests/engine/test_threshold_calibration_20261003.py; здесь — показ, «Откалибровать»,
ручной порог (en+ru) и то, что он переживает перезапуск движка и бота.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import httpx

from cctv import i18n
from cctv.bot.bot import CONSOLE_KEY, CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.state import State
from cctv.engine import cctv_bridge, model_switch, threshold_calibration as tc
from test_cctv_contract_20260824 import make_config
from test_cctv_flow_20260824 import OWNER, FakeTelegram

CONSOLE = 55
CAMERAS = {"cameras": [
    {"camera_id": "dacha", "title": "Дача", "rtsp_url": "rtsp://192.0.2.10/1", "person_detection": True},
    {"camera_id": "city", "title": "Город", "rtsp_url": "rtsp://192.0.2.11/1", "person_detection": True},
    {"camera_id": "plain", "title": "Без YOLO", "rtsp_url": "rtsp://192.0.2.12/1"},
]}


class FakeDetector:
    def __init__(self, settings) -> None:
        self.confidence = settings.confidence

    def detect(self, frame):
        return float(frame) >= self.confidence, float(frame), (0.1, 0.1, 0.2, 0.5)


def fake_build(settings, probe=False):
    return FakeDetector(settings)


class ThresholdMenuTest(unittest.IsolatedAsyncioTestCase):
    lang = "ru"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = pathlib.Path(tmp.name)
        models = root / "models"
        models.mkdir()
        for name in ("yolov5n.onnx", "yolox_tiny.onnx"):
            (models / name).write_bytes(b"x")
        env = {key: value for key, value in os.environ.items()
               if key not in ("CCTV_STATE_DIR", "CCTV_BUFFER_DIR")}
        env.update({"CCTV_MODEL_DIRS": str(models), "CCTV_PERSON_MODEL_FAMILY": "yolov5"})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patch = mock.patch.object(tc, "CALIBRATION_FRAMES", 50)
        patch.start()
        self.addCleanup(patch.stop)

        self.engine = cctv_bridge.Bridge(CAMERAS, root / "storage", "http://127.0.0.1:0")
        server = cctv_bridge.build_server(self.engine, {"CCTV_BIND": "127.0.0.1", "CCTV_PORT": "0"})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.start_engine()

        self.cfg = dataclasses.replace(make_config(root), lang=self.lang)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.state.set_service(CONSOLE_KEY, str(CONSOLE))
        client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}")
        self.addCleanup(client.close)
        self.tg = FakeTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    def start_engine(self) -> None:
        """Процесс конвейера: модели и пороги поверх того же каталога состояния."""
        self.models = model_switch.ModelManager(self.engine.state_dir, build=fake_build, log=lambda _l: None)
        self.thresholds = tc.ThresholdManager(self.engine.state_dir, self.models, log=lambda _l: None)
        self.holders, self.cals = {}, {}
        for camera_id in ("dacha", "city"):
            self.holders[camera_id] = holder = self.models.register(camera_id)
            self.cals[camera_id] = self.thresholds.camera(camera_id, holder)

    def t(self, key: str, **params) -> str:
        return i18n.t(key, self.lang, **params)

    def messages(self) -> list[dict]:
        return [m for m in self.tg.of("send_message") if m.get("message_thread_id") == CONSOLE]

    async def next_message(self, count: int, timeout: float = 5.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.messages()) < count:
            if loop.time() > deadline:
                self.fail(f"нет сообщения №{count} на пульте: {[m['text'] for m in self.messages()]}")
            await asyncio.sleep(0.02)
        return self.messages()[count - 1]

    @staticmethod
    def buttons(message: dict) -> list[tuple[str, str]]:
        markup = message.get("reply_markup")
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] if markup else []

    async def press(self, message: dict, label: str) -> str:
        data = next(d for text, d in self.buttons(message) if label in text)
        return await self.bot.on_callback(OWNER, CONSOLE, data)

    async def open_menu(self) -> dict:
        """Меню с пульта; ответ — первое новое сообщение (просьбы о вводе тоже идут на пульт)."""
        count = len(self.messages()) + 1
        markup = await self.bot.console_markup()
        data = next(b.callback_data for row in markup.inline_keyboard for b in row
                    if b.callback_data.startswith("cv:thr:"))
        self.assertEqual(await self.bot.on_callback(OWNER, CONSOLE, data), self.t("thr.menu_here"))
        return await self.next_message(count)

    async def test_menu_shows_threshold_per_camera(self):
        menu = await self.open_menu()
        self.assertIn(self.t("thr.header", model="yolov5/yolov5n.onnx"), menu["text"])
        self.assertIn(self.t("thr.line_start", title="Дача", threshold=0.35), menu["text"])
        self.assertIn(self.t("thr.line_start", title="Город", threshold=0.35), menu["text"])
        self.assertNotIn("Без YOLO", menu["text"], "камера без детекции людей порога не имеет")
        self.assertIn(self.t("thr.explain"), menu["text"])
        labels = [text for text, _ in self.buttons(menu)]
        self.assertEqual(labels, [self.t("thr.button_calibrate_all"), self.t("thr.button_set", title="Дача"),
                                  self.t("thr.button_set", title="Город")])

    async def test_calibrate_button_starts_calibration_and_menu_shows_result(self):
        menu = await self.open_menu()
        answer = await self.press(menu, self.t("thr.button_calibrate_all"))
        self.assertEqual(answer, self.t("thr.calibrating", frames=50))
        self.assertIn(self.t("thr.pending"), (await self.open_menu())["text"])
        self.assertEqual(self.thresholds.poll(), "calibrating")
        menu = await self.open_menu()
        self.assertIn(self.t("thr.collecting", collected=0, needed=50), menu["text"])
        # Дача: фон с одиночными всплесками 0.45 — порог встаёт над ними.
        for index in range(80):
            score = 0.45 if index % 10 == 9 else 0.1
            found, value, _ = self.holders["dacha"].detect(score)
            self.cals["dacha"].observe(1000.0 + index, value, found)
        menu = await self.open_menu()
        self.assertIn(self.t("thr.line_auto", title="Дача", threshold=0.55, noise=0.45, frames=50, passes=0),
                      menu["text"])
        self.assertEqual(self.holders["dacha"].confidence, 0.55)

    async def test_manual_threshold_is_applied_and_survives_restart(self):
        menu = await self.open_menu()
        ask = await self.press(menu, self.t("thr.button_set", title="Город"))
        self.assertEqual(ask, self.t("thr.ask", title="Город", threshold=0.35, min=0.05, max=0.95))
        self.assertEqual(await self.bot.on_text(OWNER, CONSOLE, "2"), self.t("thr.bad_value", min=0.05, max=0.95))
        await self.press(menu, self.t("thr.button_set", title="Город"))
        done = await self.bot.on_text(OWNER, CONSOLE, "0,6")
        self.assertEqual(done, self.t("thr.set_done", title="Город", value=0.6, model="yolov5/yolov5n.onnx"))
        self.thresholds.poll()
        self.assertEqual(self.holders["city"].confidence, 0.6)
        self.assertIsNone(self.holders["dacha"].threshold)
        # Перезапуск движка: порог берётся из каталога состояния, бот его показывает.
        self.start_engine()
        self.assertEqual(self.holders["city"].confidence, 0.6)
        menu = await self.open_menu()
        self.assertIn(self.t("thr.line_manual", title="Город", threshold=0.6), menu["text"])
        # Снять ручной: кнопка «Авто» только у камеры с ручным порогом.
        self.assertEqual(sum(1 for text, _ in self.buttons(menu) if text == self.t("thr.button_auto")), 1)
        answer = await self.press(menu, self.t("thr.button_auto"))
        self.assertEqual(answer, self.t("thr.auto_done", title="Город", threshold=0.35))
        self.thresholds.poll()
        self.assertEqual(self.holders["city"].confidence, 0.35)

    async def test_manual_threshold_belongs_to_its_model(self):
        menu = await self.open_menu()
        await self.press(menu, self.t("thr.button_set", title="Дача"))
        await self.bot.on_text(OWNER, CONSOLE, "0.7")
        self.thresholds.poll()
        self.assertEqual(self.holders["dacha"].confidence, 0.7)
        model_switch.request_switch(self.engine.state_dir, "yolox", "yolox_tiny.onnx")
        self.assertEqual(self.models.poll(), "ok")
        self.assertEqual(self.holders["dacha"].confidence, 0.30, "после смены модели — стартовый порог")
        menu = await self.open_menu()
        self.assertIn(self.t("thr.line_start", title="Дача", threshold=0.30), menu["text"])
        self.assertIn(self.t("thr.collecting", collected=0, needed=50), menu["text"])

    async def test_auto_word_removes_manual(self):
        menu = await self.open_menu()
        await self.press(menu, self.t("thr.button_set", title="Дача"))
        await self.bot.on_text(OWNER, CONSOLE, "0.5")
        await self.press(menu, self.t("thr.button_set", title="Дача"))
        word = "авто" if self.lang == "ru" else "auto"
        self.assertEqual(await self.bot.on_text(OWNER, CONSOLE, word),
                         self.t("thr.auto_done", title="Дача", threshold=0.35))


class ThresholdMenuEnglishTest(ThresholdMenuTest):
    lang = "en"


if __name__ == "__main__":
    unittest.main()
