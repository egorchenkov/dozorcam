#!/usr/bin/env python3
"""Настоящие веса каждого семейства на эталонных кадрах.

Кадры — свободные изображения из набора scikit-image (tests/data/reference/README.md):
человек (астронавт, NASA, public domain) и сцена без людей (кофе, CC0). «Кадр
камеры» 16:9 собирается из них: человек вклеен в известное место сцены — так
проверяется и уверенность, и перевод рамки в доли кадра через растяжение.

Весов в репо нет (лицензии, docs/models.md): тест ищет их в ``CCTV_TEST_MODELS_DIR``
(по умолчанию кэш бенча ``~/.cache/cctv-bench/models``) и в каталоге образа, а без
файла семейство пропускает.
"""
from __future__ import annotations

import os
import pathlib
import unittest

import cv2
import numpy as np

from cctv.engine.person_models import FAMILIES, DetectorSettings, build_person_detector

DATA = pathlib.Path(__file__).resolve().parents[1] / "data" / "reference"
MODEL_DIRS = [pathlib.Path(os.environ.get("CCTV_TEST_MODELS_DIR") or pathlib.Path.home() / ".cache/cctv-bench/models"),
              pathlib.Path("/usr/share/cctv/models")]
# Место человека в сцене 1280×720, доли кадра (x, y, w, h).
PASTE = (0.5, 1 / 3, 0.4, 0.4)


def model_file(name: str) -> pathlib.Path | None:
    return next((d / name for d in MODEL_DIRS if (d / name).is_file()), None)


def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w, h = min(ax2, bx2) - max(a[0], b[0]), min(ay2, by2) - max(a[1], b[1])
    inter = max(0.0, w) * max(0.0, h)
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def scene() -> np.ndarray:
    frame = cv2.resize(cv2.imread(str(DATA / "no_person_coffee.jpg")), (1280, 720))
    x, y, w, h = (round(v * s) for v, s in zip(PASTE, (1280, 720, 1280, 720)))
    frame[y:y + h, x:x + w] = cv2.resize(cv2.imread(str(DATA / "person_astronaut.jpg")), (w, h))
    return frame


class ReferenceFramesTest(unittest.TestCase):
    def detector(self, family: str):
        path = model_file(FAMILIES[family].default_file)
        if path is None:
            self.skipTest(f"нет весов {FAMILIES[family].default_file} (CCTV_TEST_MODELS_DIR)")
        cv2.setNumThreads(1)
        return build_person_detector(DetectorSettings(family, path, FAMILIES[family].confidence, "test"), probe=True)

    def check_family(self, family: str):
        detector = self.detector(family)
        found, score, box = detector.detect(scene())
        self.assertTrue(found, f"{family}: человек в сцене не найден, score={score:.3f}")
        self.assertGreater(iou(box, PASTE), 0.5, f"{family}: рамка {box} мимо вклейки {PASTE}")
        found, score, _ = detector.detect(cv2.imread(str(DATA / "no_person_coffee.jpg")))
        self.assertFalse(found)
        self.assertLess(score, 0.1, f"{family}: фон без людей дал {score:.3f}")

    def test_yolov5(self):
        self.check_family("yolov5")

    def test_yolox(self):
        self.check_family("yolox")

    def test_yolov8(self):
        self.check_family("yolov8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
