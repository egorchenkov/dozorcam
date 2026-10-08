#!/usr/bin/env python3
"""Фильтр неподвижных объектов: предмет в кадре не должен становиться «человеком».

Решение владельца 26.09.2026 после пяти ложняков Дача-3 на мешках в углу
(YOLO 0.35–0.41 при неподвижной сцене): исправление общее для всех камер с
детекцией на сервере, а не порог одной камеры.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

import numpy as np

from cctv.engine.person_detector import PersonDetector  # noqa: E402
from cctv.engine.still_object_filter import StillObjectFilter  # noqa: E402

BOX = (0.05, 0.80, 0.10, 0.15)  # угол с мешками на dacha3 (доли кадра)


def scene(value: int = 60, shape=(180, 320)) -> np.ndarray:
    return np.full(shape, value, dtype=np.uint8)


def with_figure(frame: np.ndarray, box=BOX, value: int = 200) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape
    out[int(box[1] * h):int((box[1] + box[3]) * h), int(box[0] * w):int((box[0] + box[2]) * w)] = value
    return out


def warm(filt: StillObjectFilter, frames, step: float = 0.5) -> float:
    at = 0.0
    for frame in frames:
        filt.remember(at, frame)
        at += step
    return at - step


class StillObjectFilterTest(unittest.TestCase):
    def filt(self, **kw) -> StillObjectFilter:
        kw.setdefault("bypass", 0.70)
        kw.setdefault("unreliable_outside", 15)
        return StillObjectFilter(window_sec=20, ref_age_sec=3, inside_min=10, ratio=3, **kw)

    def test_static_object_is_not_a_person(self):
        """Мешки стоят 10 с, YOLO даёт 0.41 — рамка не изменилась, тревоги нет."""
        filt = self.filt()
        at = warm(filt, [with_figure(scene())] * 20)
        verdict = filt.judge(at, BOX, 0.41)
        self.assertFalse(verdict.moving)
        self.assertEqual("still", verdict.reason)
        self.assertEqual(0.0, verdict.inside)
        self.assertGreaterEqual(verdict.ref_age, 3)

    def test_person_who_appeared_is_moving(self):
        """Секунды назад в этом месте кадра никого не было — это человек."""
        filt = self.filt()
        at = warm(filt, [scene()] * 12 + [with_figure(scene())] * 2)
        verdict = filt.judge(at, BOX, 0.37)
        self.assertTrue(verdict.moving)
        self.assertEqual("moving", verdict.reason)
        self.assertGreater(verdict.inside, 90)
        self.assertLess(verdict.outside, 1)

    def test_global_light_change_is_unknown_not_still(self):
        """Облако/ИК: изменился весь кадр — эталон недостоверен. С 0.1.2 это «не
        знаю», кадр идёт дальше (06.10.2026 так резались люди в сумерках)."""
        filt = self.filt()
        at = warm(filt, [with_figure(scene(60))] * 12 + [with_figure(scene(120), value=250)] * 2)
        verdict = filt.judge(at, BOX, 0.45)
        self.assertTrue(verdict.moving)
        self.assertEqual("unreliable_reference", verdict.reason)
        self.assertGreater(verdict.inside, 90)
        self.assertGreater(verdict.outside, 90)

    def test_known_static_object_stays_still_on_light_change(self):
        """Предмет уже признан неподвижным на этом месте — смена света его не
        «оживляет», и обход по уверенности он не получает."""
        filt = self.filt(bypass=0.55)
        at = warm(filt, [with_figure(scene(60))] * 20)
        self.assertEqual("still", filt.judge(at, BOX, 0.41).reason)
        filt2 = self.filt(bypass=0.55)
        filt2.static_boxes.extend(filt.static_boxes)
        at = warm(filt2, [with_figure(scene(60))] * 12 + [with_figure(scene(120), value=250)] * 2)
        verdict = filt2.judge(at, BOX, 0.60)
        self.assertFalse(verdict.moving)
        self.assertEqual("known_static", verdict.reason)
        at = warm(filt2, [with_figure(scene(60))] * 20)
        self.assertEqual("still", filt2.judge(at, BOX, 0.60).reason)  # без обхода 0.55

    def test_static_memory_survives_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "still.json"
            filt = self.filt(memory_path=path)
            at = warm(filt, [with_figure(scene())] * 20)
            filt.judge(at, BOX, 0.41)
            again = self.filt(memory_path=path, bypass=0.55)
            self.assertEqual(1, len(again.static_boxes))
            at = warm(again, [with_figure(scene())] * 20)
            self.assertEqual("still", again.judge(at, BOX, 0.60).reason)

    def test_no_reference_yet_means_no_alert(self):
        """Первые секунды после старта сравнивать не с чем — предмет не тревожит,
        человек проявится следующими кадрами."""
        filt = self.filt()
        at = warm(filt, [with_figure(scene())] * 3)  # 1 с истории < ref_age
        verdict = filt.judge(at, BOX, 0.41)
        self.assertFalse(verdict.moving)
        self.assertEqual("no_reference", verdict.reason)

    def test_confident_still_figure_bypasses(self):
        """Стоящий без движения человек с уверенностью выше порога обхода не теряется."""
        filt = self.filt()
        at = warm(filt, [with_figure(scene())] * 20)
        verdict = filt.judge(at, BOX, 0.75)
        self.assertTrue(verdict.moving)
        self.assertEqual("confidence_bypass", verdict.reason)

    def test_bypass_can_be_disabled(self):
        filt = StillObjectFilter(window_sec=20, ref_age_sec=3, bypass=1.0)
        at = warm(filt, [with_figure(scene())] * 20)
        self.assertFalse(filt.judge(at, BOX, 0.99).moving)

    def test_other_geometry_is_not_a_reference(self):
        """ISAPI-снимок и RTSP-кадр разной геометрии: чужая форма не опорный кадр."""
        filt = self.filt()
        at = warm(filt, [scene(shape=(90, 160))] * 12 + [with_figure(scene())] * 2)
        verdict = filt.judge(at, BOX, 0.41)
        self.assertEqual("no_reference", verdict.reason)

    def test_history_is_bounded_by_window(self):
        filt = self.filt()
        warm(filt, [scene()] * 100)  # 50 с при шаге 0.5
        self.assertLessEqual(filt.history[-1][0] - filt.history[0][0], 20)

    def test_large_frame_is_shrunk(self):
        filt = self.filt(width=320)
        filt.remember(0.0, np.zeros((1080, 1920, 3), dtype=np.uint8))
        self.assertEqual((180, 320), filt.history[-1][1].shape)


class DummyNet:
    def __init__(self, rows):
        self.rows = np.array([rows], dtype=np.float32)

    def setInput(self, _blob):
        pass

    def forward(self):
        return self.rows


class DetectorBoxTest(unittest.TestCase):
    def test_detect_returns_normalized_box_of_best_person(self):
        detector = PersonDetector.__new__(PersonDetector)
        detector.net, detector.confidence = DummyNet([
            [320, 320, 64, 128, 0.99, 0.01, 0.99],   # не человек
            [64, 576, 64, 96, 0.80, 0.75, 0.10],     # человек: центр (64,576), 64×96 из 640
        ]), 0.35
        found, score, box = detector.detect(np.zeros((32, 32, 3), dtype=np.uint8))
        self.assertTrue(found)
        self.assertAlmostEqual(score, 0.60, places=5)
        self.assertAlmostEqual(box[0], 0.05, places=5)
        self.assertAlmostEqual(box[1], 0.825, places=5)
        self.assertAlmostEqual(box[2], 0.10, places=5)
        self.assertAlmostEqual(box[3], 0.15, places=5)
        self.assertEqual((True, score), detector.detects_person(np.zeros((32, 32, 3), dtype=np.uint8)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
