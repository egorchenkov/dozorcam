#!/usr/bin/env python3
"""Контракт адаптера YOLOv8-стиля: тот же detect(frame) -> (found, score, box), что у YOLOv5n."""
from __future__ import annotations

import unittest

import numpy as np

from cctv.engine.person_detector_yolov8 import Yolov8PersonDetector


class DummyNet:
    def __init__(self, rows):
        self.rows = np.array([rows], dtype=np.float32)
        self.blob = None

    def setInput(self, blob):
        self.blob = blob

    def forward(self):
        return self.rows


def channels_first(points: list[list[float]], classes: int = 80) -> np.ndarray:
    """Выход YOLOv8 (4 + классы, N) из списка точек [cx, cy, w, h, p_class0, p_class1, …]."""
    out = np.zeros((4 + classes, 8400), dtype=np.float32)
    for index, point in enumerate(points):
        out[:len(point), index] = point
    return out


class Yolov8PersonTest(unittest.TestCase):
    def detector(self, output):
        detector = Yolov8PersonDetector.__new__(Yolov8PersonDetector)
        detector.net, detector.confidence, detector.input_size = DummyNet(output), 0.35, 640
        return detector

    def test_person_class_without_objectness_and_box_in_frame_fractions(self):
        # Рамка 64×128 с центром (320, 160) пикселей входа 640 → доли кадра при растяжении.
        output = channels_first([
            [100, 100, 50, 50, 0.01, 0.99],   # уверенный не-человек — не тревога
            [320, 160, 64, 128, 0.72, 0.10],
        ])
        detector = self.detector(output)
        found, score, box = detector.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
        self.assertTrue(found)
        self.assertAlmostEqual(score, 0.72, places=5)
        self.assertTrue(all(type(value) is float for value in (score, *box)))
        for got, want in zip(box, ((320 - 32) / 640, (160 - 64) / 640, 64 / 640, 128 / 640)):
            self.assertAlmostEqual(got, want, places=5)
        # Препроцессинг как у YOLOv5n-пути: растяжение в квадрат входа.
        self.assertEqual(detector.net.blob.shape, (1, 3, 640, 640))

    def test_non_person_is_not_an_alert(self):
        output = channels_first([[100, 100, 50, 50, 0.01, 0.99]])
        found, score = self.detector(output).detects_person(np.zeros((32, 32, 3), dtype=np.uint8))
        self.assertFalse(found)
        self.assertLess(score, 0.35)

    def test_points_first_layout_of_other_families_is_an_error(self):
        # (N, 85) — раскладка YOLOv5/YOLOX: разбирать её как YOLOv8 значит читать мусор.
        detector = self.detector(np.zeros((8400, 85), dtype=np.float32))
        with self.assertRaisesRegex(RuntimeError, "person_model_output_invalid"):
            detector.detect(np.zeros((32, 32, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
