#!/usr/bin/env python3
"""Автокалибровка порога детектора по паре «модель × камера» (T-20261003-05).

Менеджер моделей и менеджер порогов — настоящие, детекторы — метки с заданной
оценкой (что грузится ONNX, проверяет test_model_switch_20261003). Последний класс
гоняет настоящую YOLOX-Tiny по записанному фону эталонного кадра.
"""
from __future__ import annotations

import json
import pathlib
import random
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

from cctv.engine import model_switch, threshold_calibration as tc
from cctv.engine.model_switch import ModelManager, request_switch
from cctv.engine.person_models import FAMILIES, DetectorSettings

from test_person_reference_frames import DATA, model_file, scene

FRAMES = 200


class FakeDetector:
    """Оценка кадра = число в кадре (кадр здесь — просто float)."""

    def __init__(self, settings) -> None:
        self.confidence = settings.confidence

    def detect(self, frame):
        score = float(frame)
        return score >= self.confidence, score, (0.1, 0.1, 0.2, 0.5)


def fake_build(settings, probe=False):
    return FakeDetector(settings)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.state, models = root / "state", root / "models"
        models.mkdir()
        for name in ("yolov5n.onnx", "yolox_tiny.onnx"):
            (models / name).write_bytes(b"x")
        self.env = {"CCTV_MODEL_DIRS": str(models), "CCTV_PERSON_MODEL_FAMILY": "yolov5"}
        patch = mock.patch.object(tc, "CALIBRATION_FRAMES", FRAMES)
        patch.start()
        self.addCleanup(patch.stop)
        self.patch_env = mock.patch.dict("os.environ", self.env)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.lines: list[str] = []
        self.models, self.thresholds, self.holder, self.cal = self.engine()
        self.clock = 1_000_000.0

    def engine(self):
        models = ModelManager(self.state, self.env, build=fake_build, log=self.lines.append)
        thresholds = tc.ThresholdManager(self.state, models, log=self.lines.append)
        holder = models.register("dacha")
        return models, thresholds, holder, thresholds.camera("dacha", holder)

    def frames(self, scores, camera_human=None, still=False, step=1.0):
        """Прогнать кадры как конвейер: detect → observe (событие — 2 подряд)."""
        hits, series = 0, []
        for score in scores:
            self.clock += step
            found, value, _ = self.holder.detect(score)
            hits = hits + 1 if found else 0
            series = (series + [value])[-2:] if found else []
            self.cal.observe(self.clock, value, found, still, camera_human)
            if hits >= 2:
                self.cal.event(min(series), bool(camera_human))
                hits, series = 0, []

    def background(self, n, low=0.02, high=0.20, seed=1, spike=None, every=20):
        """Фон сцены; ``spike`` — одиночные всплески (ветка, блик) раз в ``every`` кадров."""
        rng = random.Random(seed)
        return [spike if spike is not None and i % every == every - 1 else rng.uniform(low, high)
                for i in range(n)]

    def entry(self) -> dict:
        return json.loads((self.state / tc.STATE_NAME).read_text())["cameras"]["dacha"]


class ComputeTest(unittest.TestCase):
    def test_threshold_is_noise_plus_margin_and_never_below_start(self):
        noisy = [0.40] * 95 + [0.48] * 5
        result = tc.compute_threshold(noisy, [], start=0.35)
        self.assertEqual(result["value"], 0.58)
        self.assertEqual(result["reason"], "noise")
        quiet = tc.compute_threshold([0.05] * 100, [], start=0.35)
        self.assertEqual((quiet["value"], quiet["reason"]), (0.35, "start_above_noise"))

    def test_confirmed_passes_cap_threshold_but_not_below_noise(self):
        noisy = [0.40] * 100
        capped = tc.compute_threshold(noisy, [0.47, 0.52, 0.60], start=0.30)
        self.assertEqual((capped["value"], capped["reason"]), (0.47, "passes"))
        floor = tc.compute_threshold(noisy, [0.46, 0.46, 0.46], start=0.30)
        self.assertEqual(floor["value"], 0.46)
        # Проходы на уровне шума порогом не спасти — они не учитываются.
        useless = tc.compute_threshold(noisy, [0.41, 0.42, 0.43], start=0.30)
        self.assertEqual((useless["value"], useless["passes_used"]), (0.50, 0))
        # Камера видит людей, которых модель оценивает ниже стартового порога:
        # проходы опускают порог под стартовый, но не к шуму.
        low = tc.compute_threshold([0.05] * 100, [0.22, 0.25, 0.28, 0.6], start=0.30)
        self.assertEqual((low["value"], low["reason"]), (0.22, "passes"))

    def test_effective_priority_and_model_binding(self):
        entry = {"model": "yolox/a.onnx", "result": {"value": 0.42}}
        manual = {"model": "yolox/a.onnx", "value": 0.5}
        self.assertEqual(tc.effective(0.3, entry, manual, "yolox/a.onnx"), (0.5, "manual"))
        self.assertEqual(tc.effective(0.3, entry, None, "yolox/a.onnx"), (0.42, "auto"))
        self.assertEqual(tc.effective(0.3, entry, manual, "yolov5/b.onnx"), (0.3, "start"))


class EngineTest(Base):
    def test_without_command_threshold_stays_model_threshold(self):
        """Без команды из бота и без ручного порога — ровно порог модели (прод YOLOv5n)."""
        self.frames(self.background(3 * FRAMES, 0.30, 0.34))
        self.assertIsNone(self.holder.threshold)
        self.assertEqual(self.holder.confidence, 0.35)
        self.assertEqual(self.entry()["state"], "idle")

    def test_calibrate_command_sets_threshold_above_noise(self):
        tc.request_calibration(self.state)
        self.assertEqual(self.thresholds.poll(), "calibrating")
        self.assertEqual(self.entry()["state"], "collecting")
        # Одиночные всплески 0.42 выше стартового 0.35: сами события не дают, но
        # два подряд дали бы ложное — порог обязан встать над ними.
        noise = self.background(FRAMES + 30, 0.05, 0.30, spike=0.42)
        self.frames(noise)
        entry = self.entry()
        self.assertEqual(entry["state"], "calibrated")
        self.assertAlmostEqual(entry["result"]["noise_level"], 0.42, places=3)
        self.assertEqual(self.holder.confidence, 0.52)
        self.assertGreater(self.holder.confidence, max(noise), "порог выше всего фона — запас есть")
        self.assertEqual(entry["result"]["reason"], "noise")
        # Фон больше не даёт событий, проход человека — даёт.
        self.assertFalse(self.holder.detect(max(noise))[0])
        self.assertTrue(self.holder.detect(0.8)[0])
        self.assertTrue(any(line.startswith("person_threshold_calibrated camera=dacha") for line in self.lines))

    def test_model_switch_starts_calibration_from_start_threshold(self):
        tc.request_calibration(self.state)
        self.thresholds.poll()
        self.frames(self.background(FRAMES + 30, 0.05, 0.30, spike=0.45))
        self.assertGreater(self.holder.confidence, 0.5)
        request_switch(self.state, "yolox", "yolox_tiny.onnx", self.env)
        self.assertEqual(self.models.poll(), "ok")
        # Порог прежней модели к новой не относится: до накопления — стартовый.
        self.assertIsNone(self.holder.threshold)
        self.assertEqual(self.holder.confidence, FAMILIES["yolox"].confidence)
        entry = self.entry()
        self.assertEqual((entry["model"], entry["state"], entry["reason"]),
                         ("yolox/yolox_tiny.onnx", "collecting", "model_switch"))
        self.frames(self.background(FRAMES + 30, 0.02, 0.10))
        self.assertEqual(self.entry()["state"], "calibrated")
        self.assertEqual(self.holder.confidence, 0.30, "тихая сцена — стартовый порог остаётся")

    def test_frames_around_a_person_are_not_noise(self):
        tc.request_calibration(self.state)
        self.thresholds.poll()
        # Человек подходит: 0.30–0.34 ниже порога 0.35, затем событие 0.6/0.7.
        self.frames(self.background(30))
        self.frames([0.30, 0.32, 0.34, 0.6, 0.7, 0.33, 0.31])
        self.frames(self.background(FRAMES + 30))
        result = self.entry()["result"]
        self.assertLess(result["noise_level"], 0.21, "подступы к срабатыванию в шум не попали")

    def test_still_objects_and_camera_human_are_not_noise(self):
        tc.request_calibration(self.state)
        self.thresholds.poll()
        self.frames([0.5] * 50, still=True)  # мешки в углу: их отсекает фильтр, не порог
        self.frames([0.45] * 20, camera_human=True)
        self.frames(self.background(FRAMES + 30), camera_human=False)
        self.assertLess(self.entry()["result"]["noise_level"], 0.21)

    def test_confirmed_passes_are_recorded_and_cap_threshold(self):
        # Проходы YOLO (PERSON_HITS=2) с подтверждением камеры и без.
        self.frames([0.05, 0.50, 0.52, 0.05], camera_human=True)
        self.frames([0.05] * 15, camera_human=False)
        self.frames([0.05, 0.55, 0.60, 0.05], camera_human=False)
        self.frames([0.05] * 15, camera_human=False)
        # Проход, который видела только камера: один кадр 0.48 события не даёт,
        # пик YOLO за эпизод камеры — оценка прохода.
        self.frames([0.05, 0.48, 0.05, 0.05], camera_human=True)
        self.frames([0.05] * 15, camera_human=False)
        passes = self.entry()["passes"]
        self.assertEqual([(p["score"], p["source"]) for p in passes],
                         [(0.5, "yolo+onvif"), (0.55, "yolo"), (0.48, "onvif")])
        # Шумная сцена: по шуму порог ушёл бы на 0.40+0.10, проходы держат его ниже.
        tc.request_calibration(self.state)
        self.thresholds.poll()
        self.frames(self.background(FRAMES + 30, 0.02, 0.10, spike=0.40), camera_human=False)
        result = self.entry()["result"]
        self.assertEqual((result["reason"], result["passes_used"]), ("passes", 3))
        self.assertEqual(self.holder.confidence, 0.48)
        self.assertLess(self.holder.confidence, 0.50, "без проходов было бы 0.50")

    def test_onvif_episode_with_yolo_event_is_one_pass(self):
        self.frames([0.05, 0.50, 0.52, 0.05], camera_human=True)
        self.frames([0.05] * 3, camera_human=False)
        self.assertEqual([p["source"] for p in self.entry()["passes"]], ["yolo+onvif"])

    def test_manual_override_wins_and_survives_restart(self):
        active = model_switch.catalog(self.state, self.env)["active"]
        tc.set_override(self.state, "dacha", 0.62, active)
        self.assertEqual(self.thresholds.poll(), "overrides")
        self.assertEqual(self.holder.confidence, 0.62)
        tc.request_calibration(self.state)
        self.thresholds.poll()
        self.frames(self.background(FRAMES + 30))
        self.assertEqual(self.entry()["state"], "calibrated")
        self.assertEqual(self.holder.confidence, 0.62, "ручной порог сильнее автокалибровки")
        # Перезапуск движка: тот же каталог состояния.
        _models, _thresholds, holder, _cal = self.engine()
        self.assertEqual(holder.confidence, 0.62)
        tc.set_override(self.state, "dacha", None, active)
        _thresholds.poll()
        self.assertEqual(holder.confidence, 0.35, "снят ручной — авто (тихая сцена = стартовый)")

    def test_calibrated_threshold_survives_restart(self):
        tc.request_calibration(self.state)
        self.thresholds.poll()
        self.frames(self.background(FRAMES + 30, spike=0.45))
        value = self.holder.confidence
        self.assertGreater(value, 0.5)
        _models, _thresholds, holder, _cal = self.engine()
        self.assertEqual(holder.confidence, value)

    def test_override_rejects_out_of_range(self):
        active = model_switch.catalog(self.state, self.env)["active"]
        for value in (0.0, 1.2, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tc.set_override(self.state, "dacha", value, active)

    def test_overview_for_bot(self):
        active = model_switch.catalog(self.state, self.env)["active"]
        view = tc.overview(self.state, [("dacha", "Дача"), ("city", "Город")], active)
        self.assertEqual(view["model"], "yolov5/yolov5n.onnx")
        self.assertEqual([(c["camera_id"], c["threshold"], c["source"]) for c in view["cameras"]],
                         [("dacha", 0.35, "start"), ("city", 0.35, "start")])
        tc.request_calibration(self.state, ["dacha"])
        self.assertTrue(tc.overview(self.state, [("dacha", "Дача")], active)["pending"])
        self.thresholds.poll()
        view = tc.overview(self.state, [("dacha", "Дача")], active)
        self.assertEqual((view["pending"], view["cameras"][0]["state"]), (False, "collecting"))


class PipelineWiringTest(unittest.TestCase):
    def test_pipeline_feeds_calibration_and_events(self):
        import inspect

        from cctv.engine import cctv_pipeline

        source = inspect.getsource(cctv_pipeline.detect)
        self.assertIn("person_thresholds_for(storage).camera(camera.camera_id, person_detector)", source)
        self.assertIn("calibration.observe(frame_at, score, found and score >= person_detector.confidence, still_object", source)
        self.assertIn("weakest = min(hit_scores[-PERSON_HITS:]", source)
        self.assertIn("calibration.event(weakest", source)


class RecordedBackgroundTest(unittest.TestCase):
    """Настоящая YOLOX-Tiny: фон — записанный кадр без людей с дрожанием
    яркости, шумом сенсора и сдвигом; порог выше шума с запасом, человек — выше порога."""

    def test_real_model_threshold_above_background_noise(self):
        path = model_file("yolox_tiny.onnx")
        if path is None:
            self.skipTest("нет весов yolox_tiny.onnx (кэш бенча)")
        settings = DetectorSettings("yolox", path, FAMILIES["yolox"].confidence, "test")
        holder = model_switch.SwitchableDetector(FAMILIES["yolox"].factory(path, settings.confidence), settings)
        base = cv2.resize(cv2.imread(str(DATA / "no_person_coffee.jpg")), (1280, 720))
        rng = np.random.default_rng(7)
        frames = 120
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tc, "CALIBRATION_FRAMES", frames):
            thresholds = tc.ThresholdManager(pathlib.Path(tmp), log=lambda _l: None)
            cal = thresholds.camera("cam", holder)
            thresholds.begin(["cam"], "request")
            noise = []
            for index in range(frames + 15):
                dx, dy = rng.integers(-12, 13, size=2)
                shifted = np.roll(base, (int(dy), int(dx)), axis=(0, 1)).astype(np.int16)
                gain = rng.uniform(0.6, 1.3)
                frame = np.clip(shifted * gain + rng.normal(0, 8, base.shape), 0, 255).astype(np.uint8)
                found, score, _ = holder.detect(frame)
                noise.append(score)
                cal.observe(1000.0 + index, score, found, False, None)
            state = json.loads((pathlib.Path(tmp) / tc.STATE_NAME).read_text())["cameras"]["cam"]
        self.assertEqual(state["state"], "calibrated")
        level = state["result"]["noise_level"]
        self.assertGreaterEqual(holder.confidence, round(level + tc.MARGIN, 2) - 0.01)
        self.assertGreaterEqual(holder.confidence, FAMILIES["yolox"].confidence)
        found, score, _ = holder.detect(scene())
        self.assertTrue(found, f"человек {score:.2f} при пороге {holder.confidence:.2f}")


if __name__ == "__main__":
    unittest.main()
