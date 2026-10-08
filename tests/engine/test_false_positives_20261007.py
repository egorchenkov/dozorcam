#!/usr/bin/env python3
"""Ложные события камеры у двери 07.10.2026 — реплей detect() до/после правки 0.1.2.

Первая сборка 0.1.2 (c683851) за полдня отправила восемь ложных событий door_out:

* три в окне confirm (сигнал камеры был): человек открыл калитку, в кадре открылось
  ведро с сеном, и YOLO 0.20–0.33 по неподвижному ведру ушёл «человеком» — в окне
  confirm фильтр неподвижных не применялся;
* пять без сигнала камеры (подписка здорова, цели в зоне не было): ИК-куст на
  рассвете 0.38–0.41 и мешок в тенях листвы днём 0.39–0.48 — рамка «двигалась».

«До» — c683851, «после» — в окне confirm режется мёртвая рамка (внутри < 3 %),
а у камеры у двери снаружи при молчащей камере порог 0.60. Настоящие проходы (идущий человек 0.21–0.44 при сигнале камеры,
человек у двери при смене всего кадра) ловятся по-прежнему — см. и
test_miss_fixes_20261006.
"""
from __future__ import annotations

import unittest
from unittest import mock

from cctv.engine import cctv_pipeline
from cctv.engine.onvif_motion_gate import OnvifMotionGate
from detect_replay import NEW, NEW_C683851, Shot, figure, quiet, replay, scene, stand, walk

T0 = 1_791_354_000.0
BUCKET = (0.114, 0.718, 0.077, 0.121)


def bucket_after_gate_opened():
    """Калитка открылась в T0+10: ведро в кадре; YOLO даёт ему 0.20–0.33 с T0+30."""
    return (quiet(T0, 10) + stand(T0 + 10, [0.05] * 40, box=BUCKET)
            + stand(T0 + 30, [0.25, 0.31, 0.22, 0.33, 0.27, 0.20, 0.23, 0.26] * 3, box=BUCKET)
            + quiet(T0 + 42, 30, extra=(BUCKET, 220)))


def moving_texture(scores, box=(0.59, 0.44, 0.13, 0.24)):
    """Куст или тени листвы: рамка на месте, но внутри всё меняется (still=moving)."""
    leaves = [Shot(T0 + 20 + i * 0.5, score, box, figure(scene(60, 2000 + i), box, 240 if i % 2 else 190))
              for i, score in enumerate(scores)]
    return quiet(T0, 20, extra=(box, 120)) + leaves + quiet(T0 + 20 + len(scores) * 0.5, 20, extra=(box, 120))


class BucketInConfirmWindowTest(unittest.TestCase):
    """09:26:04, 09:37:27, 09:38:33 — рамка на ведре 0.114,0.718 в окне сигнала камеры."""

    def test_before_bucket_became_person(self):
        result = replay(bucket_after_gate_opened(), signals=[T0 + 12], human=True, version=NEW_C683851)
        self.assertEqual(1, len(result.events))
        self.assertTrue(any("confirm=1" in line for line in result.log))

    def test_after_bucket_rejected_as_still(self):
        result = replay(bucket_after_gate_opened(), signals=[T0 + 12], human=True, version=NEW)
        self.assertEqual([], result.events)
        rejects = [r for r in result.journal if r["kind"] == "reject"]
        self.assertTrue(rejects)
        self.assertTrue(all(r["reason"] == "still" for r in rejects), rejects)

    def test_after_walking_person_in_window_still_caught(self):
        """Человек проходит мимо того же ведра: его рамка движется — событие есть."""
        shots = bucket_after_gate_opened()[:60] + walk(T0 + 30, [0.12, 0.24, 0.31, 0.28, 0.15],
                                                        box=(0.15, 0.40, 0.05, 0.20), dx=0.02)
        result = replay(shots, signals=[T0 + 28], human=True, version=NEW)
        self.assertEqual(1, len(result.events))


    def test_after_person_standing_at_door_with_gestures_caught(self):
        """Человек стоит у двери, двигаются только руки (внутри рамки ~5 %): в окне
        сигнала это не «мёртвая» рамка — событие есть (обычный фильтр отверг бы)."""
        box = (0.4, 0.3, 0.1, 0.4)
        standing = stand(T0, [0.05] * 40, box=box, value=150)
        gestures = []
        for i, score in enumerate([0.24, 0.30, 0.27, 0.22]):
            image = figure(scene(60, 3000 + i), box, 150)
            image[60:72, 135:145] = 220  # рука: ~5 % площади рамки
            gestures.append(Shot(T0 + 20 + i * 0.5, score, box, image))
        shots = standing + gestures + quiet(T0 + 22, 20, extra=(box, 150))
        result = replay(shots, signals=[T0 + 5], human=True, version=NEW)
        self.assertEqual(1, len(result.events))
        self.assertTrue(any("confirm=1" in line and "still=still" in line for line in result.log), result.log)


class QuietCameraTest(unittest.TestCase):
    """06:19–06:23 (ИК-куст 0.38–0.41) и 11:42/11:47 (мешок в тенях 0.39–0.48): камера молчала."""

    def test_before_texture_became_person(self):
        result = replay(moving_texture([0.30, 0.41, 0.38, 0.40, 0.48, 0.36]), human=True, version=NEW_C683851)
        self.assertEqual(1, len(result.events))

    def test_after_quiet_camera_raises_threshold(self):
        result = replay(moving_texture([0.30, 0.41, 0.38, 0.40, 0.48, 0.36]), human=True, version=NEW)
        self.assertEqual([], result.events)
        rejects = [r for r in result.journal if r["kind"] == "reject"]
        self.assertEqual(["camera_quiet"], [r["reason"] for r in rejects])
        self.assertEqual(0.48, rejects[0]["max_conf"])

    def test_confident_person_caught_without_camera(self):
        """Предохранитель от слепой камеры: уверенный человек проходит и без её сигнала."""
        result = replay(quiet(T0, 20) + walk(T0 + 20, [0.45, 0.66, 0.72, 0.70]) + quiet(T0 + 22, 20),
                        human=True, version=NEW)
        self.assertEqual(1, len(result.events))

    def test_camera_target_within_hold_keeps_normal_threshold(self):
        """Цель камеры в зоне 3 мин назад (удержание 5 мин): порог обычный, 0.40 — событие."""
        shots = quiet(T0, 20) + walk(T0 + 20, [0.20, 0.40, 0.41, 0.38]) + quiet(T0 + 22, 20)
        result = replay(shots, signals=[T0 - 180], human=True, version=NEW)
        self.assertEqual(1, len(result.events))
        self.assertFalse(any("confirm=1" in line for line in result.log))

    def test_unlisted_camera_keeps_normal_threshold(self):
        """Камера в доме (06.10: рабочий у края кадра 0.36–0.52, камера молчала) — правило не для неё."""
        shots = quiet(T0, 20) + walk(T0 + 20, [0.20, 0.40, 0.41, 0.38]) + quiet(T0 + 22, 20)
        self.assertEqual(1, len(replay(shots, human=True, version=NEW, camera_id="door_in").events))

    def test_camera_without_human_subscription_unchanged(self):
        shots = quiet(T0, 20) + walk(T0 + 20, [0.20, 0.40, 0.41, 0.38]) + quiet(T0 + 22, 20)
        self.assertEqual(1, len(replay(shots, human=False, version=NEW).events))

    def test_quiet_rule_needs_healthy_subscription_and_confirm(self):
        gate = OnvifMotionGate("x", "http://192.0.2.10/onvif/Events", "u", "p")
        quiet_now = cctv_pipeline.camera_is_quiet
        with mock.patch.object(cctv_pipeline, "HUMAN_GATE_MODE", "confirm"), \
                mock.patch.object(cctv_pipeline, "HUMAN_QUIET_CAMERAS", frozenset({"x"})):
            self.assertFalse(quiet_now("x", gate, False))      # подписка не поднялась
            gate._healthy = True
            self.assertTrue(quiet_now("x", gate, False))
            self.assertFalse(quiet_now("y", gate, False))      # камера не в списке
            self.assertFalse(quiet_now("x", gate, True))
            self.assertFalse(quiet_now("x", gate, None))       # время кадра неизвестно
            self.assertFalse(quiet_now("x", None, False))
            with mock.patch.object(cctv_pipeline, "HUMAN_GATE_MODE", "shadow"):
                self.assertFalse(quiet_now("x", gate, False))
        self.assertEqual(frozenset(), cctv_pipeline.HUMAN_QUIET_CAMERAS)  # по умолчанию выключено
        with mock.patch.object(cctv_pipeline, "HUMAN_QUIET_CONFIDENCE", 0.0):
            self.assertEqual(0.35, cctv_pipeline.person_threshold(0.35, False, True))
        with mock.patch.object(cctv_pipeline, "HUMAN_QUIET_CONFIDENCE", 0.60):
            self.assertEqual(0.60, cctv_pipeline.person_threshold(0.35, False, True))
            self.assertEqual(0.20, cctv_pipeline.person_threshold(0.35, True, True))
            self.assertEqual(0.35, cctv_pipeline.person_threshold(0.35, False, False))


if __name__ == "__main__":
    unittest.main()
