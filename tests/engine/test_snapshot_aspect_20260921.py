#!/usr/bin/env python3
"""Пропорции снимка: камера снимает 16:9, а JPEG отдаёт 704x576."""
from __future__ import annotations

import pathlib
import sys
import unittest

from cctv.engine import cctv_bridge  # noqa: E402


def jpeg(width: int, height: int) -> bytes:
    cv2, numpy = cctv_bridge.cv2, cctv_bridge.numpy
    frame = numpy.zeros((height, width, 3), dtype="uint8")
    frame[: height // 2] = 200  # не сплошная заливка: иначе не видно растяжения
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


def size(body: bytes) -> tuple[int, int]:
    cv2, numpy = cctv_bridge.cv2, cctv_bridge.numpy
    frame = cv2.imdecode(numpy.frombuffer(body, dtype="uint8"), cv2.IMREAD_COLOR)
    return frame.shape[1], frame.shape[0]


@unittest.skipIf(cctv_bridge.cv2 is None, "без opencv снимок отдаётся как есть")
class SnapshotAspectTest(unittest.TestCase):
    def test_squashed_snapshot_is_stretched_to_the_scene(self):
        """Tantos дачи: сцена 2880x1620, снимок 704x576 (1.22:1)."""
        fixed = cctv_bridge.correct_aspect(jpeg(704, 576), 16 / 9)
        self.assertEqual((1024, 576), size(fixed))

    def test_correct_snapshot_is_returned_untouched(self):
        """Hikvision отдаёт 2688x1520 — трогать нечего, пережатия быть не должно."""
        body = jpeg(2688, 1520)
        self.assertIs(body, cctv_bridge.correct_aspect(body, 16 / 9))

    def test_no_aspect_configured_means_no_change(self):
        body = jpeg(704, 576)
        self.assertIs(body, cctv_bridge.correct_aspect(body, None))

    def test_broken_jpeg_is_not_an_exception(self):
        self.assertEqual(b"not a jpeg", cctv_bridge.correct_aspect(b"not a jpeg", 16 / 9))

    def test_registry_carries_the_aspect(self):
        camera = cctv_bridge.Camera(camera_id="dacha", title="Дача", site="Дача",
                                    rtsp_url="rtsp://127.0.0.1:18556/dacha", snapshot_aspect=1.778)
        self.assertAlmostEqual(1.778, camera.snapshot_aspect)


if __name__ == "__main__":
    unittest.main(verbosity=2)
