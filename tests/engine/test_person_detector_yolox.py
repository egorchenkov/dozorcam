#!/usr/bin/env python3
"""Контракт адаптера YOLOX-Tiny: тот же detect(frame) -> (found, score, box), что у YOLOv5n."""
from __future__ import annotations

import math
import unittest

import numpy as np

from cctv.engine.person_detector_yolox import GRID, GRID_STRIDE, YoloxPersonDetector


class DummyNet:
    def __init__(self, rows):
        self.rows = np.array([rows], dtype=np.float32)
        self.blob = None

    def setInput(self, blob):
        self.blob = blob

    def forward(self):
        return self.rows


def rows_with(index: int, row: list[float]) -> np.ndarray:
    rows = np.zeros((len(GRID), 85), dtype=np.float32)
    rows[index, :len(row)] = row
    return rows


class YoloxPersonTest(unittest.TestCase):
    def detector(self, rows, stretch=True):
        detector = YoloxPersonDetector.__new__(YoloxPersonDetector)
        detector.net, detector.confidence, detector.stretch = DummyNet(rows), 0.35, stretch
        return detector

    def test_person_uses_objectness_times_person_class_and_decodes_grid(self):
        # Ячейка (x=10, y=5) сетки страйда 8: центр (10+0.5)*8=84, (5+0.5)*8=44 пикселя
        # входа 416; размер exp(ln 4)*8 = 32 пикселя → доли кадра при растяжении.
        index = 5 * 52 + 10
        self.assertEqual(GRID_STRIDE[index, 0], 8)
        rows = rows_with(index, [0.5, 0.5, math.log(4), math.log(4), 0.9, 0.8])
        rows[0, 4:7] = [0.99, 0.01, 0.99]  # уверенный не-человек — не тревога
        found, score, box = self.detector(rows).detect(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertTrue(found)
        self.assertAlmostEqual(score, 0.72, places=5)
        self.assertTrue(all(type(value) is float for value in (score, *box)))
        for got, want in zip(box, ((84 - 16) / 416, (44 - 16) / 416, 32 / 416, 32 / 416)):
            self.assertAlmostEqual(got, want, places=5)

    def test_non_person_is_not_an_alert(self):
        rows = rows_with(0, [0, 0, 0, 0, 0.99, 0.01, 0.99])
        found, score = self.detector(rows).detects_person(np.zeros((32, 32, 3), dtype=np.uint8))
        self.assertFalse(found)
        self.assertLess(score, 0.35)

    def test_letterbox_box_is_in_frame_fractions(self):
        # 1280×720 в 416: масштаб 0.325, картинка занимает 416×234 сверху слева.
        index = 5 * 52 + 10
        rows = rows_with(index, [0.5, 0.5, math.log(4), math.log(4), 0.9, 0.8])
        detector = self.detector(rows, stretch=False)
        _, _, box = detector.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertAlmostEqual(box[0], (84 - 16) / 416, places=4)
        self.assertAlmostEqual(box[1], (44 - 16) / 234, places=4)
        self.assertEqual(detector.net.blob.shape, (1, 3, 416, 416))

    def test_unexpected_output_shape_is_an_error(self):
        detector = self.detector(np.zeros((100, 85), dtype=np.float32))
        with self.assertRaisesRegex(RuntimeError, "person_model_output_invalid"):
            detector.detect(np.zeros((32, 32, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
