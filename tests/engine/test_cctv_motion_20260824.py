#!/usr/bin/env python3
"""Детектор движения: шкала замера и чувствительность порога.

Порог уже один раз загрубили вслепую — 8 % против шума покоя 0.10 % — и
движение перестало доходить до темы. Проверяется не «код исполняется», а
что сцена с человеком порог берёт, а шум камеры его не берёт.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

import numpy as np

from cctv.engine import cctv_pipeline  # noqa: E402
from cctv.engine.cctv_pipeline import MOTION_THRESHOLD, motion_score  # noqa: E402

WIDTH, HEIGHT = 320, 240  # substream детектора
PIXELS = WIDTH * HEIGHT


def scene(fill: int = 90) -> np.ndarray:
    return np.full((HEIGHT, WIDTH), fill, dtype=np.uint8)


def with_figure(frame: np.ndarray, share: float) -> np.ndarray:
    """Тёмный силуэт, занимающий заданную долю кадра."""
    out = frame.copy()
    rows = max(1, int(PIXELS * share / WIDTH))
    out[:rows, :] = 10
    return out


class MotionScale(unittest.TestCase):
    def test_score_is_percent_not_fraction(self) -> None:
        """Полностью разные кадры дают 100, а не 1.0: с долей порог не сравнить."""
        black, white = scene(0), scene(255)
        self.assertAlmostEqual(motion_score(black, white), 100.0, places=3)

    def test_still_scene_scores_zero(self) -> None:
        frame = scene()
        self.assertEqual(motion_score(frame, frame), 0.0)


class Sensitivity(unittest.TestCase):
    def test_sensor_noise_stays_below_threshold(self) -> None:
        """Шум покоя на площадке — 0.10 % кадра; порог его не считает движением."""
        previous = scene()
        noisy = previous.copy()
        flicker = int(PIXELS * 0.002)  # вдвое выше наблюдавшегося максимума
        noisy.reshape(-1)[:flicker] = 200
        self.assertLess(motion_score(previous, noisy), MOTION_THRESHOLD)

    def test_person_sized_figure_crosses_threshold(self) -> None:
        """Фигура в 2 % кадра — человек на общем плане — порог берёт."""
        previous = scene()
        self.assertGreaterEqual(motion_score(previous, with_figure(previous, 0.02)), MOTION_THRESHOLD)

    def test_threshold_keeps_tenfold_margin_over_measured_noise(self) -> None:
        """Запас над замеренным шумом есть, но не такой, чтобы глушить сцену."""
        self.assertLessEqual(MOTION_THRESHOLD, 2.0)
        self.assertGreaterEqual(MOTION_THRESHOLD, 0.5)


class Telemetry(unittest.TestCase):
    def test_stats_interval_is_configured(self) -> None:
        """Без периодических замеров порог опять придётся выставлять вслепую."""
        self.assertGreater(cctv_pipeline.STATS_INTERVAL, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
