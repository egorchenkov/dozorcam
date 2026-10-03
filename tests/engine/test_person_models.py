#!/usr/bin/env python3
"""Выбор детектора людей по настройке «семейство + файл модели»."""
from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

from cctv.engine import person_detector, person_models
from cctv.engine.person_detector import PersonDetector
from cctv.engine.person_detector_yolov8 import Yolov8PersonDetector
from cctv.engine.person_detector_yolox import YoloxPersonDetector
from cctv.engine.person_models import DetectorSettings, build_person_detector, resolve


class DummyNet:
    def __init__(self, rows):
        self.rows = np.zeros((1, *rows), dtype=np.float32)

    def setInput(self, _blob):
        pass

    def forward(self):
        return self.rows


class ResolveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.user_dir, self.image_dir = self.root / "etc-models", self.root / "share-models"
        self.user_dir.mkdir()
        self.image_dir.mkdir()
        (self.image_dir / "yolox_tiny.onnx").write_bytes(b"x")
        self.no_legacy = self.root / "absent" / "yolov5n.onnx"

    def env(self, **values):
        return {"CCTV_MODEL_DIRS": f"{self.user_dir}:{self.image_dir}", **values}

    def test_new_install_without_setting_gets_bundled_yolox(self):
        got = resolve(self.env(), legacy_model=self.no_legacy)
        self.assertEqual(got, DetectorSettings("yolox", self.image_dir / "yolox_tiny.onnx", 0.30, "default"))

    def test_existing_install_with_model_variable_stays_yolov5n(self):
        # Старые образы задавали CCTV_PERSON_MODEL — это всегда был файл YOLOv5n.
        got = resolve(self.env(CCTV_PERSON_MODEL="/opt/old/yolov5n.onnx"), legacy_model=self.no_legacy)
        self.assertEqual(got, DetectorSettings("yolov5", pathlib.Path("/opt/old/yolov5n.onnx"), 0.35,
                                               "legacy_model_setting"))

    def test_existing_install_with_legacy_file_stays_yolov5n(self):
        legacy = self.root / "yolov5n.onnx"
        legacy.write_bytes(b"x")
        got = resolve(self.env(), legacy_model=legacy)
        self.assertEqual((got.family, got.model, got.confidence, got.reason),
                         ("yolov5", legacy, 0.35, "legacy_model_file"))

    def test_legacy_threshold_variable_still_applies(self):
        got = resolve(self.env(CCTV_PERSON_MODEL="/m/yolov5n.onnx", CCTV_PERSON_CONFIDENCE="0.42"),
                      legacy_model=self.no_legacy)
        self.assertEqual((got.family, got.confidence), ("yolov5", 0.42))

    def test_family_setting_wins_over_legacy_file(self):
        legacy = self.root / "yolov5n.onnx"
        legacy.write_bytes(b"x")
        got = resolve(self.env(CCTV_PERSON_MODEL_FAMILY="YOLOX"), legacy_model=legacy)
        self.assertEqual((got.family, got.model, got.reason), ("yolox", self.image_dir / "yolox_tiny.onnx", "setting"))

    def test_user_file_is_found_by_name_and_user_dir_shadows_image(self):
        (self.user_dir / "yolov8s.onnx").write_bytes(b"x")
        (self.user_dir / "yolox_tiny.onnx").write_bytes(b"x")
        got = resolve(self.env(CCTV_PERSON_MODEL_FAMILY="yolov8", CCTV_PERSON_MODEL="yolov8s.onnx"),
                      legacy_model=self.no_legacy)
        self.assertEqual((got.family, got.model, got.confidence), ("yolov8", self.user_dir / "yolov8s.onnx", 0.30))
        got = resolve(self.env(CCTV_PERSON_MODEL_FAMILY="yolox"), legacy_model=self.no_legacy)
        self.assertEqual(got.model, self.user_dir / "yolox_tiny.onnx")

    def test_missing_file_points_to_user_dir_for_the_error_message(self):
        got = resolve(self.env(CCTV_PERSON_MODEL_FAMILY="yolov5"), legacy_model=self.no_legacy)
        self.assertEqual(got.model, self.user_dir / "yolov5n.onnx")
        with self.assertRaisesRegex(RuntimeError, "person_model_missing"):
            build_person_detector(got)

    def test_each_family_has_its_own_start_threshold(self):
        thresholds = {name: resolve(self.env(CCTV_PERSON_MODEL_FAMILY=name), legacy_model=self.no_legacy).confidence
                      for name in person_models.FAMILIES}
        self.assertEqual(thresholds, {"yolov5": 0.35, "yolox": 0.30, "yolov8": 0.30})
        # Порог YOLOv5 — тот же, что у PersonDetector до мультимодели.
        self.assertEqual(thresholds["yolov5"], person_detector.PERSON_CONFIDENCE)

    def test_empty_values_from_compose_mean_unset(self):
        got = resolve(self.env(CCTV_PERSON_MODEL_FAMILY="", CCTV_PERSON_MODEL=" ", CCTV_PERSON_CONFIDENCE=""),
                      legacy_model=self.no_legacy)
        self.assertEqual((got.family, got.confidence, got.reason), ("yolox", 0.30, "default"))

    def test_unknown_family_is_an_error_not_a_silent_default(self):
        with self.assertRaisesRegex(ValueError, "person_model_family_unknown"):
            resolve(self.env(CCTV_PERSON_MODEL_FAMILY="detr"), legacy_model=self.no_legacy)

    def test_available_models_lists_onnx_files_once(self):
        (self.user_dir / "yolov8n.onnx").write_bytes(b"x")
        (self.user_dir / "yolox_tiny.onnx").write_bytes(b"x")
        (self.user_dir / "README.txt").write_text("x")
        self.assertEqual(person_models.available_models(self.env()),
                         [self.user_dir / "yolov8n.onnx", self.user_dir / "yolox_tiny.onnx"])


class BuildTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.model = pathlib.Path(tmp.name) / "model.onnx"
        self.model.write_bytes(b"x")

    def build(self, family, output_shape, probe=False):
        with mock.patch("cv2.dnn.readNetFromONNX", return_value=DummyNet(output_shape)):
            return build_person_detector(DetectorSettings(family, self.model, 0.5, "test"), probe=probe)

    def test_one_adapter_per_family_with_common_interface(self):
        for family, cls, shape in (("yolov5", PersonDetector, (25200, 85)),
                                   ("yolox", YoloxPersonDetector, (3549, 85)),
                                   ("yolov8", Yolov8PersonDetector, (84, 8400))):
            with self.subTest(family=family):
                detector = self.build(family, shape, probe=True)
                self.assertIs(type(detector), cls)
                self.assertEqual(detector.confidence, 0.5)
                found, score, box = detector.detect(np.zeros((72, 128, 3), dtype=np.uint8))
                self.assertFalse(found)
                self.assertEqual(len(box), 4)

    def test_probe_rejects_file_of_another_family(self):
        # YOLOX-файл на входе 640 даёт (8400, 85): YOLOv5-разбор прочёл бы его без ошибки.
        for family, shape in (("yolov5", (8400, 85)), ("yolov5", (84, 8400)),
                              ("yolox", (25200, 85)), ("yolov8", (8400, 85))):
            with self.subTest(family=family, shape=shape):
                with self.assertRaisesRegex(RuntimeError, "person_model_(family_mismatch|output_invalid)"):
                    self.build(family, shape, probe=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
