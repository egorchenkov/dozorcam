#!/usr/bin/env python3
"""Рваный первый кадр RTSP-сессии не должен уезжать в Telegram.

Регрессия 23.09.2026, Дача-3 (DS-2CD2043G2): в начале каждого TCP-подключения
к main камера шлёт рваный interleave, первый I-кадр обрезан, и error concealment
тянет последнюю целую строку вниз — кадр с вертикальными полосами. Снимок по
кнопке брал ровно первый кадр сессии: 8 из 8 проб были такими.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest.mock import patch

from cctv.engine import cctv_bridge  # noqa: E402
from cctv.engine import cctv_pipeline  # noqa: E402

try:
    import numpy as np
except ImportError:  # системный python моста без numpy
    np = None


def scene(height: int = 360, width: int = 640):
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, (height, width, 3), dtype=np.uint8)


@unittest.skipIf(np is None, "нужен numpy")
class SmearDetectorTest(unittest.TestCase):
    def test_concealed_frame_is_rejected(self):
        frame = scene()
        frame[45:] = frame[44]  # целые 45 строк, ниже — копия последней
        self.assertTrue(cctv_pipeline.is_smeared(frame))

    def test_live_scene_passes(self):
        self.assertFalse(cctv_pipeline.is_smeared(scene()))

    def test_flat_frame_is_not_mistaken_for_smear(self):
        """Закрытый объектив или ночь без подсветки — ровный кадр, но без полос."""
        self.assertFalse(cctv_pipeline.is_smeared(np.zeros((360, 640, 3), np.uint8)))


class SnapshotSkipsFirstKeyframeTest(unittest.TestCase):
    def test_rtsp_snapshot_takes_second_keyframe(self):
        camera = cctv_bridge.Camera(camera_id="c", title="c", site="s",
                                    rtsp_url="rtsp://127.0.0.1:18560/c")
        seen = []

        def fake_run(command, **_kwargs):
            seen.append(command)
            pathlib.Path(command[-1]).write_bytes(b"jpeg")
            return type("R", (), {"returncode": 0})()

        bridge = cctv_bridge.Bridge.__new__(cctv_bridge.Bridge)
        bridge.storage = pathlib.Path(__import__("tempfile").mkdtemp())
        (bridge.storage / "tmp").mkdir()
        with patch.object(cctv_bridge.subprocess, "run", fake_run):
            body, _ = bridge.snapshot(camera)
        self.assertEqual(b"jpeg", body)
        command = seen[0]
        self.assertEqual("nokey", command[command.index("-skip_frame") + 1])
        self.assertLess(command.index("-skip_frame"), command.index("-i"))
        self.assertIn(r"select=gte(n\,1)", command)


if __name__ == "__main__":
    unittest.main()
