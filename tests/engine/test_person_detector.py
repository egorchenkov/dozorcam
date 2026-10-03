#!/usr/bin/env python3
"""Контракт классификатора людей: только COCO person, не любой объект."""
from __future__ import annotations

import pathlib
import sys
import unittest

import numpy as np

from cctv.engine.person_detector import PersonDetector  # noqa: E402


class DummyNet:
    def __init__(self, rows):
        self.rows = np.array([rows], dtype=np.float32)

    def setInput(self, _blob):
        pass

    def forward(self):
        return self.rows


class PersonScoreTest(unittest.TestCase):
    def detector(self, rows):
        detector = PersonDetector.__new__(PersonDetector)
        detector.net, detector.confidence = DummyNet(rows), 0.45
        return detector

    def test_person_uses_objectness_times_person_class(self):
        # class 0 = person; высокая уверенность другого COCO-класса не тревога.
        detector = self.detector([
            [0, 0, 0, 0, 0.99, 0.01, 0.99],
            [0, 0, 0, 0, 0.80, 0.75, 0.10],
        ])
        found, score = detector.detects_person(np.zeros((32, 32, 3), dtype=np.uint8))
        self.assertTrue(found)
        self.assertAlmostEqual(score, 0.60, places=5)

    def test_non_person_is_not_an_alert(self):
        detector = self.detector([[0, 0, 0, 0, 0.99, 0.01, 0.99]])
        found, score = detector.detects_person(np.zeros((32, 32, 3), dtype=np.uint8))
        self.assertFalse(found)
        self.assertLess(score, detector.confidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
