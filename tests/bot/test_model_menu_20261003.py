#!/usr/bin/env python3
"""/model: меню модели детектора в боте — сквозь настоящий мост до менеджера моделей.

Бот, HTTP-мост движка (на loopback) и ModelManager конвейера — настоящие; подменены
только Bot API и сборка детекторов (метки вместо ONNX: что именно грузится и как
откатывается битый файл, проверяет tests/engine/test_model_switch_20261003.py).
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
from cctv.bot import bot as bot_module
from cctv.bot.bot import CONSOLE_KEY, CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.state import State
from cctv.engine import cctv_bridge, model_switch
from test_cctv_contract_20260824 import make_config
from test_cctv_flow_20260824 import OWNER, STRANGER, FakeTelegram

CONSOLE = 55


class FakeDetector:
    def __init__(self, settings) -> None:
        self.confidence = settings.confidence

    def detect(self, frame):
        return False, 0.1, (0.0, 0.0, 0.0, 0.0)


def fake_build(settings, probe=False):
    if settings.model.name == "broken.onnx":
        raise RuntimeError("cv2.error: failed to parse ONNX")
    return FakeDetector(settings)


class ModelMenuTest(unittest.IsolatedAsyncioTestCase):
    lang = "ru"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        models = root / "models"
        models.mkdir()
        for name in ("yolov5n.onnx", "yolox_tiny.onnx", "broken.onnx"):
            (models / name).write_bytes(b"x")
        env = {key: value for key, value in os.environ.items()
               if key not in ("CCTV_STATE_DIR", "CCTV_BUFFER_DIR")}
        env.update({"CCTV_MODEL_DIRS": str(models), "CCTV_PERSON_MODEL_FAMILY": "yolov5"})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("POLL_SEC", 0.02),):
            patch = mock.patch.object(model_switch, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(bot_module, "MODEL_POLL_SEC", 0.02)
        patch.start()
        self.addCleanup(patch.stop)

        # Движок: мост на свободном порту loopback и менеджер моделей конвейера.
        self.engine = cctv_bridge.Bridge({"cameras": []}, root / "storage", "http://127.0.0.1:0")
        server = cctv_bridge.build_server(self.engine, {"CCTV_BIND": "127.0.0.1", "CCTV_PORT": "0"})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.models = model_switch.ModelManager(self.engine.state_dir, build=fake_build, log=lambda _l: None)
        self.holder = self.models.register("dacha")
        self.models.start()

        self.cfg = dataclasses.replace(make_config(root), lang=self.lang)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.state.set_service(CONSOLE_KEY, str(CONSOLE))
        client = httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}")
        self.addCleanup(client.close)
        self.tg = FakeTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

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
        return [(b.text, b.callback_data) for row in message["reply_markup"].inline_keyboard for b in row]

    async def press(self, message: dict, label: str) -> str:
        data = next(d for text, d in self.buttons(message) if label in text)
        return await self.bot.on_callback(OWNER, CONSOLE, data)

    async def test_menu_shows_families_files_and_active_model(self):
        self.assertEqual(await self.bot.on_model(OWNER), self.t("model.menu_sent"))
        menu = await self.next_message(1)
        self.assertIn(self.t("model.active", family="YOLOv5", file="yolov5n.onnx", confidence=0.35), menu["text"])
        for title in ("YOLOv5", "YOLOX", "YOLOv8-style"):
            self.assertIn(title, menu["text"])
        self.assertIn(self.t("model.files", files="broken.onnx, yolov5n.onnx, yolox_tiny.onnx"), menu["text"])
        self.assertEqual([text for text, _ in self.buttons(menu)], ["✅ YOLOv5", "YOLOX", "YOLOv8-style", self.t("console.thresholds")])
        self.assertEqual(await self.bot.on_model(STRANGER), self.t("no_access"))

    async def test_console_has_model_button(self):
        markup = await self.bot.console_markup()
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        model = next(d for d in datas if d.startswith("cv:model:"))
        self.assertEqual(await self.bot.on_callback(OWNER, CONSOLE, model), self.t("model.menu_here"))
        self.assertIn(self.t("model.header"), (await self.next_message(1))["text"])

    async def test_switch_reports_success_and_engine_runs_new_model(self):
        await self.bot.on_model(OWNER)
        menu = await self.next_message(1)
        self.assertEqual(await self.press(menu, "YOLOX"), self.t("model.files_here"))
        files = await self.next_message(2)
        labels = [text for text, _ in self.buttons(files)]
        self.assertEqual(labels[0], "yolox_tiny.onnx", "файл своего семейства — первым")
        self.assertIn("⚠️ yolov5n.onnx", labels)
        answer = await self.press(files, "yolox_tiny.onnx")
        self.assertEqual(answer, self.t("model.switching", family="YOLOX", file="yolox_tiny.onnx"))
        done = await self.next_message(3)
        self.assertEqual(done["text"], self.t("model.switched", family="YOLOX", file="yolox_tiny.onnx",
                                              confidence=0.30))
        self.assertEqual((self.holder.settings.family, self.holder.generation), ("yolox", 1))
        # Повтор того же выбора — без заявки.
        self.assertEqual(await self.press(files, "yolox_tiny.onnx"), self.t("model.already"))

    async def test_broken_file_rolls_back_with_message(self):
        await self.bot.on_model(OWNER)
        await self.press(await self.next_message(1), "YOLOX")
        await self.press(await self.next_message(2), "broken.onnx")
        rolled = await self.next_message(3)
        active = self.t("model.active", family="YOLOv5", file="yolov5n.onnx", confidence=0.35)
        self.assertEqual(rolled["text"], self.t(
            "model.rolled_back", family="YOLOX", file="broken.onnx",
            reason=self.t("model.error.person_model_load_failed"), active=active))
        self.assertEqual((self.holder.settings.family, self.holder.generation), ("yolov5", 0))
        self.assertEqual(self.holder.detect(None)[1], 0.1, "детектор работает на прежней модели")
        # В меню — и прежняя модель, и след неудачной смены.
        await self.bot.on_model(OWNER)
        menu = await self.next_message(4)
        self.assertIn(active, menu["text"])
        self.assertIn(self.t("model.last_rollback", family="YOLOX", file="broken.onnx",
                             reason=self.t("model.error.person_model_load_failed")), menu["text"])

    async def test_engine_silence_is_reported_not_swallowed(self):
        with mock.patch.object(bot_module, "MODEL_WAIT_SEC", 0.2), \
                mock.patch.object(self.models, "poll", lambda: None):
            await self.bot.on_model(OWNER)
            await self.press(await self.next_message(1), "YOLOX")
            await self.press(await self.next_message(2), "yolox_tiny.onnx")
            timeout = await self.next_message(3)
        self.assertEqual(timeout["text"], self.t(
            "model.timeout", seconds=0.2,
            active=self.t("model.active", family="YOLOv5", file="yolov5n.onnx", confidence=0.35)))
        self.assertEqual(self.holder.settings.family, "yolov5")


class ModelMenuEnglishTest(ModelMenuTest):
    lang = "en"


if __name__ == "__main__":
    unittest.main()
