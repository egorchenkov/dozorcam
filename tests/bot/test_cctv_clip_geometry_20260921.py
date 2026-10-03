#!/usr/bin/env python3
"""Пропорции клипа в теме: Telegram обязан получить геометрию от нас.

Регрессия 21.09.2026 (камеры Дача-2 и -3, 2688x1520): клип тяжелее ~10 МБ
Telegram не разбирает и возвращает video 320x320 duration=0 — клиент рисует
квадрат вместо кадра 16:9. Замер на живом боте: 9.96 МБ разобран верно, 11.4 МБ
— нет, а тот же файл с явными width/height/duration принят правильно.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


import httpx  # noqa: E402

from cctv.bot import bot as bot_module  # noqa: E402
from cctv.bot.bot import CctvBot, make_thumbnail, probe_video  # noqa: E402
from cctv.bot.bridge import Bridge  # noqa: E402
from cctv.bot.events import normalize_event  # noqa: E402
from cctv.bot.state import State  # noqa: E402
from test_cctv_contract_20260824 import BRIDGE, make_config  # noqa: E402
from test_cctv_flow_20260824 import FakeTelegram  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def sample_clip(target: pathlib.Path, width: int = 640, height: int = 360) -> bytes:
    """Настоящий mp4: подделка не докажет, что ffprobe читает именно геометрию."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc=size={width}x{height}:rate=10:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(target)], check=True)
    return target.read_bytes()


@unittest.skipUnless(HAVE_FFMPEG, "нужны ffmpeg и ffprobe")
class ProbeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.clip = self.tmp / "clip.mp4"
        sample_clip(self.clip)

    async def test_probe_reads_geometry_and_duration(self):
        self.assertEqual({"width": 640, "height": 360, "duration": 2},
                         await probe_video(str(self.clip), lambda _msg: None))

    async def test_thumbnail_fits_telegram_limits(self):
        thumb = await make_thumbnail(str(self.clip), lambda _msg: None)
        self.assertIsNotNone(thumb)
        self.assertLessEqual(pathlib.Path(thumb).stat().st_size, 200 * 1024)

    async def test_broken_file_degrades_to_empty_meta(self):
        """Без метаданных клип всё равно уезжает — доставка важнее геометрии."""
        broken = self.tmp / "broken.mp4"
        broken.write_bytes(b"not a video")
        self.assertEqual({}, await probe_video(str(broken), lambda _msg: None))
        self.assertIsNone(await make_thumbnail(str(broken), lambda _msg: None))


@unittest.skipUnless(HAVE_FFMPEG, "нужны ffmpeg и ffprobe")
class ClipDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        # Конфиг заморожен, а тестовый клип крупнее дефолтного лимита в 128 байт.
        self.cfg = dataclasses.replace(make_config(self.tmp), max_clip_bytes=8 * 1024 * 1024)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.clip_bytes = sample_clip(self.tmp / "source.mp4")
        self.motion_health = {"state": "watching", "reason": "", "last_motion_at": None}

        def handler(request: httpx.Request):
            if request.url.path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    {"camera_id": "dacha2", "title": "Гостиная Дача", "site": "dacha",
                     "status": "online", "last_frame_at": "2026-09-21T10:00:00Z",
                     "motion": self.motion_health}]})
            return httpx.Response(200, content=self.clip_bytes,
                                  headers={"content-type": "video/mp4"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = FakeTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    def clip_event(self):
        return normalize_event(
            {"event_id": "clip-1", "type": "media.ready", "camera_id": "dacha2",
             "request_id": "m-1", "source_event_id": "m-1", "kind": "clip",
             "occurred_at": "2026-09-21T10:01:00Z", "captured_at": "2026-09-21T10:01:00Z",
             "download": {"url": f"{BRIDGE}/v1/media/opaque",
                          "sha256": hashlib.sha256(self.clip_bytes).hexdigest(),
                          "bytes": len(self.clip_bytes)}})

    async def register(self):
        await self.bot.on_event(normalize_event(
            {"event_id": "reg", "type": "camera.registered", "camera_id": "dacha2",
             "title": "Гостиная Дача", "site": "dacha",
             "occurred_at": "2026-09-21T10:00:00Z"}))

    async def test_clip_carries_geometry_and_thumbnail(self):
        await self.register()
        await self.bot.on_event(self.clip_event())
        video = self.tg.of("send_video")[0]
        self.assertEqual((640, 360, 2), (video["width"], video["height"], video["duration"]))
        self.assertIsNotNone(video["thumbnail"])
        self.assertTrue(video["supports_streaming"])

    async def test_clip_is_uploaded_as_mp4(self):
        """Регрессия 23.09.2026: временный файл «.bin» давал mime octet-stream,
        Bot API сохранял клип документом, и Android показывал неизвестный файл."""
        await self.register()
        await self.bot.on_event(self.clip_event())
        video = self.tg.of("send_video")[0]
        self.assertTrue(video["filename"].endswith(".mp4"))
        self.assertTrue(video["video"].name.endswith(".mp4"))

    async def test_clip_waits_for_telegram_longer_than_ptb_default(self):
        """Регрессия стенда 02.10.2026: Telegram отвечал на клип 2688x1520 дольше 5 с
        (дефолт PTB), бот ловил TimedOut и слал повтор — в теме 2–3 копии клипа."""
        await self.register()
        await self.bot.on_event(self.clip_event())
        video = self.tg.of("send_video")[0]
        self.assertGreaterEqual(video["read_timeout"], 60)
        self.assertGreaterEqual(video["write_timeout"], 60)

    async def test_clip_still_flies_without_ffmpeg(self):
        """Сломанный ffprobe не имеет права отменить доставку события."""
        async def refuse(*_args, **_kwargs):
            raise OSError("ffprobe отсутствует")

        original = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = refuse
        self.addCleanup(lambda: setattr(asyncio, "create_subprocess_exec", original))
        await self.register()
        await self.bot.on_event(self.clip_event())
        video = self.tg.of("send_video")[0]
        self.assertNotIn("width", video)
        self.assertIsNone(video["thumbnail"])

    async def test_thumbnail_file_does_not_outlive_delivery(self):
        await self.register()
        await self.bot.on_event(self.clip_event())
        leftovers = list(pathlib.Path(tempfile.gettempdir()).glob("*.thumb.jpg"))
        self.assertEqual([], leftovers)


if __name__ == "__main__":
    unittest.main()
