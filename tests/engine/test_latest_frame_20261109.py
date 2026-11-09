#!/usr/bin/env python3
"""Кадр для мозаики «📷 Все камеры» — из буфера движка, камера не опрашивается.

`Bridge.latest_frame` берёт первый ключевой кадр новейшего ЗАКРЫТОГО сегмента
кольцевого буфера (открытый пишется прямо сейчас и дал бы рваный кадр), время
кадра — начало сегмента. Сегменты старше FRAME_MAX_AGE_SEC — «нет кадров»:
умерший рекордер оставляет в кольце старое видео. Проверяется и HTTP-путь
`GET /v1/cameras/<id>/frame` вместе с клиентом бота.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import cv2
import numpy as np

from cctv.bot import bridge as bot_bridge
from cctv.engine import cctv_bridge
from cctv.engine.cctv_bridge import Bridge, BridgeError

CONFIG = {"cameras": [
    {"camera_id": "porch", "title": "Крыльцо", "site": "Дача",
     "rtsp_url": "rtsp://user:pw@192.0.2.10:554/Streaming/Channels/101",
     "snapshot_url": "http://192.0.2.10/ISAPI/Streaming/channels/101/picture"},
    {"camera_id": "gate", "title": "Калитка", "site": "Дача",
     "rtsp_url": "rtsp://user:pw@192.0.2.11:554/Streaming/Channels/101"},
]}


def make_segment(path: pathlib.Path, color: str, size: str = "640x360", sar: str = "1") -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:size={size}:rate=10", "-t", "1", "-vf", f"setsar={sar}",
                    "-c:v", "libx264", "-g", "10", "-f", "mpegts", str(path)], check=True, timeout=60)


@unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")
class LatestFrameFromBuffer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.samples = pathlib.Path(tempfile.mkdtemp())
        for color in ("red", "blue", "green"):
            make_segment(cls.samples / f"{color}.ts", color)
        make_segment(cls.samples / "pal.ts", "red", size="704x576", sar="12/11")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.dict(os.environ, {"CCTV_BUFFER_DIR": "", "CCTV_STATE_DIR": ""}):
            self.bridge = Bridge(CONFIG, pathlib.Path(self.tmp.name), "https://cctv-bridge:9443")
        self.dir = self.bridge.buffer_dir / "porch"
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, stamp: str, sample: str, age: float) -> pathlib.Path:
        """Сегмент `<stamp>.ts` с mtime `age` секунд назад."""
        target = self.dir / f"{stamp}.ts"
        shutil.copyfile(self.samples / f"{sample}.ts", target)
        moment = time.time() - age
        os.utime(target, (moment, moment))
        return target

    @staticmethod
    def color(body: bytes) -> tuple[int, int, int]:
        image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        b, g, r = (int(v) for v in image[image.shape[0] // 2, image.shape[1] // 2])
        return r, g, b

    def test_newest_closed_segment_and_its_start_time(self) -> None:
        self.put("2026-11-09T10:00:00Z", "green", age=15)
        self.put("2026-11-09T10:00:05Z", "red", age=10)
        self.put("2026-11-09T10:00:10Z", "blue", age=0)  # открыт: ffmpeg пишет в него
        body, captured_at = self.bridge.latest_frame("porch", 320)
        self.assertEqual(b"\xff\xd8", body[:2])
        self.assertEqual("2026-11-09T10:00:05+00:00", captured_at)
        r, g, b = self.color(body)
        self.assertGreater(r, 200)
        self.assertLess(b, 60)
        image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual((180, 320), image.shape[:2])  # ширина по запросу, пропорции потока

    def test_camera_is_never_contacted(self) -> None:
        """Ни RTSP, ни ISAPI камеры: ffmpeg читает только файл буфера."""
        self.put("2026-11-09T10:00:05Z", "red", age=10)
        self.put("2026-11-09T10:00:10Z", "blue", age=0)
        real = subprocess.run
        calls: list[list[str]] = []

        def spy(args, *a, **kw):
            calls.append(list(args))
            return real(args, *a, **kw)

        with mock.patch.object(cctv_bridge.subprocess, "run", side_effect=spy), \
                mock.patch.object(cctv_bridge.urllib.request, "build_opener",
                                  side_effect=AssertionError("камеру опрашивать нельзя")):
            self.bridge.latest_frame("porch")
        self.assertEqual(1, len(calls))
        joined = " ".join(calls[0])
        self.assertNotIn("rtsp://", joined)
        self.assertNotIn("192.0.2.", joined)
        self.assertIn(str(self.dir / "2026-11-09T10:00:05Z.ts"), calls[0])

    def test_non_square_pixels_are_corrected(self) -> None:
        self.put("2026-11-09T10:00:05Z", "pal", age=10)
        self.put("2026-11-09T10:00:10Z", "blue", age=0)
        body, _ = self.bridge.latest_frame("porch", 480)
        image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual((360, 480), image.shape[:2])  # 704×576 при SAR 12/11 — это 4:3

    def test_stale_buffer_means_no_frames(self) -> None:
        self.put("2026-11-09T09:50:00Z", "red", age=cctv_bridge.FRAME_MAX_AGE_SEC + 30)
        self.put("2026-11-09T09:50:05Z", "blue", age=cctv_bridge.FRAME_MAX_AGE_SEC + 25)
        with self.assertRaises(BridgeError) as caught:
            self.bridge.latest_frame("porch")
        self.assertEqual("camera_offline", caught.exception.code)

    def test_only_open_segment_or_empty_buffer_means_no_frames(self) -> None:
        for setup in (lambda: None, lambda: self.put("2026-11-09T10:00:10Z", "blue", age=0)):
            setup()
            with self.assertRaises(BridgeError) as caught:
                self.bridge.latest_frame("porch")
            self.assertEqual("camera_offline", caught.exception.code)
        with self.assertRaises(BridgeError) as caught:
            self.bridge.latest_frame("gate")  # буфера камеры нет вовсе
        self.assertEqual("camera_offline", caught.exception.code)

    def test_broken_segment_falls_back_to_previous(self) -> None:
        self.put("2026-11-09T10:00:00Z", "green", age=15)
        broken = self.put("2026-11-09T10:00:05Z", "red", age=10)
        broken.write_bytes(b"\x00" * 4096)
        os.utime(broken, (time.time() - 10, time.time() - 10))
        self.put("2026-11-09T10:00:10Z", "blue", age=0)
        body, captured_at = self.bridge.latest_frame("porch", 320)
        self.assertEqual("2026-11-09T10:00:00+00:00", captured_at)
        r, g, b = self.color(body)
        self.assertGreater(g, 80)
        self.assertLess(r, 60)

    def test_unknown_camera(self) -> None:
        with self.assertRaises(BridgeError) as caught:
            self.bridge.latest_frame("nope")
        self.assertEqual("not_found", caught.exception.code)

    def test_http_endpoint_with_bot_client(self) -> None:
        """GET /v1/cameras/<id>/frame: JPEG и время кадра в заголовке; отказ — код ошибки."""
        self.put("2026-11-09T10:00:05Z", "red", age=10)
        self.put("2026-11-09T10:00:10Z", "blue", age=0)
        server = cctv_bridge.build_server(self.bridge, {"CCTV_BIND": "127.0.0.1", "CCTV_PORT": "0"})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        from cctv.bot.config import Config

        fields = {f.name for f in dataclasses.fields(Config)}
        cfg = mock.Mock(spec=sorted(fields))
        cfg.bridge_base_url = f"http://127.0.0.1:{server.server_address[1]}"
        cfg.internal_tls = False
        cfg.bridge_timeout_sec = 15
        cfg.max_bytes_for = lambda _kind: 8 * 1024 * 1024
        client = bot_bridge.Bridge(cfg)
        self.addCleanup(client.close)
        body, captured_at = client.latest_frame("porch", 320)
        self.assertEqual(b"\xff\xd8", body[:2])
        self.assertEqual("2026-11-09T10:00:05+00:00", captured_at)
        with self.assertRaises(bot_bridge.BridgeError) as caught:
            client.latest_frame("gate")
        self.assertEqual("camera_offline", caught.exception.code)
        with self.assertRaises(bot_bridge.BridgeError) as caught:
            client.latest_frame("nope")
        self.assertEqual("not_found", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
