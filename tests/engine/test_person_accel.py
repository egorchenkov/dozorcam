#!/usr/bin/env python3
"""Автовыбор ускорителя: поиск, самопроверка против CPU-эталона, тихий откат на CPU."""
from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

from cctv.engine import model_switch, person_accel, person_backends, person_models
from cctv.engine.person_backends import BackendSettings
from cctv.engine.person_models import DetectorSettings

from test_person_backends import _graph, onnx


def fake_sysfs(root: pathlib.Path, chip: str = "rk3576", nodes: bool = True) -> tuple[pathlib.Path, pathlib.Path]:
    """sysfs и /dev узла Rockchip: DRM-узлы card1/renderD129 драйвера RKNPU (как на RK3576)
    и GPU рядом; ``nodes`` — проброшены ли узлы NPU в /dev."""
    drm, dev = root / "sys/class/drm", root / "dev"
    for name, driver, compatible in (("card1", "RKNPU", f"rockchip,{chip}-rknpu"),
                                     ("renderD129", "RKNPU", f"rockchip,{chip}-rknpu"),
                                     ("renderD130", "panfrost", "rockchip,rk3576-mali")):
        device = drm / name / "device"
        device.mkdir(parents=True)
        (device / "uevent").write_text(f"DRIVER={driver}\nOF_NAME=npu\nOF_COMPATIBLE_0={compatible}\n"
                                       "OF_COMPATIBLE_N=1\n")
    (dev / "dri").mkdir(parents=True)
    if nodes:
        for name in ("card1", "renderD129", "renderD130"):
            (dev / "dri" / name).touch()
    return drm, dev


class DetectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

    def test_soc_and_nodes_from_sysfs_inside_container(self):
        # В контейнере /proc/device-tree скрыт: SoC — из OF_COMPATIBLE устройства rknpu.
        drm, dev = fake_sysfs(self.root)
        self.assertEqual(person_backends.soc(self.root / "no-device-tree", drm), "rk3576")
        self.assertEqual(person_backends.rknn_devices(dev, drm), [dev / "dri/card1", dev / "dri/renderD129"])

    def test_no_rockchip_npu_means_no_candidate(self):
        drm = self.root / "sys/class/drm"
        drm.mkdir(parents=True)
        with mock.patch.object(person_backends, "DRM_CLASS", drm), \
                mock.patch.object(person_backends, "DEVICE_TREE", self.root / "none"):
            self.assertIsNone(person_backends.soc(person_backends.DEVICE_TREE, drm))
            self.assertEqual(person_accel.candidates("auto"), [])

    def test_npu_without_device_nodes_is_a_problem_not_a_crash(self):
        drm, dev = fake_sysfs(self.root, nodes=False)
        with mock.patch.object(person_backends, "soc", return_value="rk3576"), \
                mock.patch.object(person_backends, "rknn_devices", return_value=[]):
            self.assertEqual(person_accel.candidates("auto"),
                             [person_accel.Candidate("rknn", "rk3576", "rknn_device_missing")])


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        person_accel.reset()
        self.addCleanup(person_accel.reset)
        self.log = []
        self.model = self.dir / "yolov5n.onnx"

    def graph(self):
        if onnx is None:
            self.skipTest("нужен пакет onnx (requirements-dev)")
        _graph(self.model, 640, (8, 16, 32), 85, True)
        return DetectorSettings("yolov5", self.model, 0.35, "test")

    def configured(self, name: str):
        return mock.patch.object(person_backends, "_configured", BackendSettings(name, 1))

    def candidate(self, *items):
        return mock.patch.object(person_accel, "candidates", return_value=list(items))

    def build(self, settings):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            detector = person_models.build_person_detector(settings)  # граф-заглушка: проба формы не для него
        self.log += [line for line in out.getvalue().splitlines() if line.startswith("person_backend_selected")]
        return detector


class ChooseTest(Fixture):
    def test_no_accelerator_auto_is_onnxruntime_cpu_without_errors(self):
        # x86 (в т.ч. ВМ без устройств) и ARM без NPU: кандидатов нет.
        settings = self.graph()
        with self.configured("auto"), self.candidate():
            detector = self.build(settings)
            choice = person_models.choose_backend(settings)
        self.assertIsInstance(detector.net, person_backends.OnnxRuntimeNet)
        self.assertEqual((choice.backend.name, choice.reason, choice.requested), ("onnxruntime", "no_accelerator", "auto"))
        self.assertEqual(len(self.log), 1)
        self.assertIn("requested=auto backend=onnxruntime threads=1 reason=no_accelerator", self.log[0])

    def test_explicit_cpu_backends_do_not_search(self):
        settings = self.graph()
        for name in ("onnxruntime", "cv2"):
            with self.subTest(backend=name), self.configured(name), \
                    mock.patch.object(person_accel, "candidates") as search:
                choice = person_models.choose_backend(settings)
                search.assert_not_called()
                self.assertEqual((choice.backend.name, choice.reason), (name, "cpu_requested"))

    def test_npu_without_device_nodes_falls_back_with_reason(self):
        settings = self.graph()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576", "rknn_device_missing")):
            detector = self.build(settings)
        self.assertIsInstance(detector.net, person_backends.OnnxRuntimeNet)
        self.assertIn("reason=rknn_device_missing accelerator=rk3576", self.log[0])

    def test_missing_rknn_file_falls_back_to_cpu(self):
        settings = self.graph()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), \
                mock.patch.dict(person_backends.os.environ, {"CCTV_RKNN_SOC": "rk3576"}):
            detector = self.build(settings)
            choice = person_models.choose_backend(settings)
        self.assertIsInstance(detector.net, person_backends.OnnxRuntimeNet)
        self.assertEqual((choice.backend.name, choice.reason), ("onnxruntime", "person_rknn_model_missing"))

    def test_missing_runtime_library_falls_back_to_cpu_also_when_rknn_is_forced(self):
        settings = self.graph()
        rknn = self.dir / "yolov5n.rk3576.rknn"
        rknn.write_bytes(b"RKNN")
        rknn.with_name(rknn.name + ".json").write_text('{"decode": "yolov5"}')
        env = {"CCTV_RKNN_SOC": "rk3576", "CCTV_RKNN_LIB": str(self.dir / "no-librknnrt.so")}
        with self.configured("rknn"), self.candidate(person_accel.Candidate("rknn", "rk3576")), \
                mock.patch.dict(person_backends.os.environ, env):
            detector = self.build(settings)
            choice = person_models.choose_backend(settings)
        self.assertIsInstance(detector.net, person_backends.OnnxRuntimeNet)
        self.assertEqual((choice.requested, choice.backend.name, choice.reason),
                         ("rknn", "onnxruntime", "person_backend_runtime_missing"))

    def test_forced_rknn_on_machine_without_npu(self):
        settings = self.graph()
        with self.configured("rknn"), self.candidate():
            choice = person_models.choose_backend(settings)
        self.assertEqual((choice.backend.name, choice.reason), ("onnxruntime", "rknn_not_found"))

    def accelerated(self, transform=None, fail=False):
        """«NPU», который считает тот же граф на onnxruntime и портит выход по ``transform``."""
        class Net:
            def __init__(self, path, settings):
                self.inner = person_backends.OnnxRuntimeNet(path, BackendSettings("onnxruntime", 1))

            def setInput(self, blob):  # noqa: N802
                self.inner.setInput(blob)

            def forward(self):
                if fail:
                    raise RuntimeError("person_rknn_inference_failed")
                output = self.inner.forward()
                return transform(output) if transform else output
        return mock.patch.dict(person_backends._OPENERS, {"rknn": Net}), Net

    def test_selfcheck_passes_and_accelerator_is_used_once_checked(self):
        settings = self.graph()
        patch, net = self.accelerated()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            detector = self.build(settings)
            again = self.build(settings)  # второй детектор (камера) — без повторной самопроверки
            choice = person_models.choose_backend(settings)
        self.assertIsInstance(detector.net, net)
        self.assertIsInstance(again.net, net)
        self.assertEqual((choice.backend.name, choice.reason, choice.accelerator), ("rknn", "selfcheck_ok", "rk3576"))
        status = choice.status()
        self.assertEqual(status["delta"], 0.0)
        self.assertIn("ms", status)
        self.assertEqual(len(self.log), 1)

    def test_selfcheck_mismatch_falls_back_to_cpu(self):
        settings = self.graph()
        for name, transform, code in (
                ("уверенность", lambda out: out * np.array([1, 1, 1, 1, 0.1] + [1] * 80, dtype=np.float32),
                 "person_selfcheck_score_mismatch"),
                ("рамка", lambda out: out + np.array([200, 150, 0, 0] + [0] * 81, dtype=np.float32),
                 "person_selfcheck_box_mismatch"),
                ("NaN", lambda out: out * np.float32("nan"), "person_selfcheck_output_invalid")):
            person_accel.reset()
            patch, _ = self.accelerated(transform)
            with self.subTest(name), self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), \
                    patch, mock.patch.object(person_accel, "SCORE_TOLERANCE", 0.15):
                detector = self.build(settings)
                choice = person_models.choose_backend(settings)
                self.assertIsInstance(detector.net, person_backends.OnnxRuntimeNet)
                if name == "рамка" and choice.reason == "person_selfcheck_score_mismatch":
                    continue  # сдвиг рамки сменил лучшую строку — тоже провал самопроверки
                self.assertEqual((choice.backend.name, choice.reason), ("onnxruntime", code))

    def test_inference_error_on_accelerator_falls_back(self):
        settings = self.graph()
        patch, _ = self.accelerated(fail=True)
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            detector = self.build(settings)
            choice = person_models.choose_backend(settings)
        self.assertIsInstance(detector.net, person_backends.OnnxRuntimeNet)
        self.assertEqual(choice.reason, "rknn_inference_failed")

    def test_broken_model_is_still_a_model_error(self):
        # Ошибка CPU-эталона — ошибка модели (откат модели в боте), а не ускорителя.
        self.model.write_bytes(b"not onnx")
        settings = DetectorSettings("yolov5", self.model, 0.35, "test")
        patch, _ = self.accelerated()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            with self.assertRaisesRegex(RuntimeError, "person_model_load_failed|person_model_output_invalid"):
                self.build(settings)

    def test_status_names_effective_backend_and_reason(self):
        settings = self.graph()
        patch, _ = self.accelerated()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            before = model_switch.describe(settings)  # мост до выбора: CPU-эталон, причины нет
            self.build(settings)
            after = model_switch.describe(settings)
            label = model_switch.label(settings)
        self.assertEqual(before["backend"], "onnxruntime")
        self.assertNotIn("accel", before)
        self.assertEqual(after["backend"], "rknn")
        self.assertEqual(after["accel"]["reason"], "selfcheck_ok")
        self.assertEqual(after["accel"]["accelerator"], "rk3576")
        self.assertEqual(label, "yolov5/yolov5n.onnx@rknn")  # порог камеры — свой у NPU


class ManagerStatusTest(Fixture):
    """Статус модели (бот /model, ключ порога) после выбора ускорителя на старте."""

    accelerated = ChooseTest.accelerated

    def manager(self):
        self.state = self.dir / "state"
        env = {"CCTV_PERSON_MODEL_FAMILY": "yolov5", "CCTV_MODEL_DIRS": str(self.dir)}
        with contextlib.redirect_stdout(io.StringIO()):
            return model_switch.ModelManager(self.state, env, log=self.log.append)

    def register(self, models, camera_id="cam1"):
        with contextlib.redirect_stdout(io.StringIO()):
            return models.register(camera_id)

    def status(self) -> dict:
        return model_switch._read(self.state / model_switch.STATUS_NAME)

    def test_config_model_on_npu_status_names_rknn_after_detector_is_built(self):
        # Старт без выбора из бота, auto, NPU есть и самопроверка прошла.
        from cctv.engine import threshold_calibration
        self.graph()
        patch, net = self.accelerated()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            models = self.manager()
            self.assertEqual(self.status()["active"]["backend"], "onnxruntime")  # до сборки — CPU-эталон
            holder = self.register(models)
            self.register(models, "cam2")
        self.assertIsInstance(holder._detector.net, net)
        active = self.status()["active"]
        self.assertEqual(active["backend"], "rknn")
        self.assertEqual((active["accel"]["reason"], active["accel"]["accelerator"]), ("selfcheck_ok", "rk3576"))
        self.assertNotIn("state", self.status())  # исхода заявки не было — и не появилось
        catalog = model_switch.catalog(self.state, {"CCTV_MODEL_DIRS": str(self.dir)})
        self.assertEqual(catalog["active"]["backend"], "rknn")
        overview = threshold_calibration.overview(self.state, [("cam1", "Cam 1")], catalog["active"])
        self.assertEqual(overview["model"], "yolov5/yolov5n.onnx@rknn")

    def test_failed_selfcheck_status_stays_cpu_with_reason(self):
        # Тихий откат: детекция на CPU, в статусе — ускоритель и причина отката.
        self.graph()
        patch, _ = self.accelerated(fail=True)
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            models = self.manager()
            holder = self.register(models)
        self.assertIsInstance(holder._detector.net, person_backends.OnnxRuntimeNet)
        active = self.status()["active"]
        self.assertEqual(active["backend"], "onnxruntime")
        self.assertEqual((active["accel"]["reason"], active["accel"]["accelerator"]),
                         ("rknn_inference_failed", "rk3576"))
        self.assertEqual(model_switch.label(holder.settings), "yolov5/yolov5n.onnx")

    def test_switch_outcome_survives_status_refresh(self):
        # Исход заявки из бота (откат) и его время не затираются уточнением бэкенда.
        self.graph()
        patch, _ = self.accelerated()
        with self.configured("auto"), self.candidate(person_accel.Candidate("rknn", "rk3576")), patch:
            models = self.manager()
            with contextlib.redirect_stdout(io.StringIO()):
                outcome = models.switch({"family": "yolox", "model": "missing.onnx"}, "req1")
            before = self.status()
            self.register(models)
        self.assertEqual(outcome, "rolled_back")
        after = self.status()
        self.assertEqual({k: after[k] for k in ("request_id", "state", "error", "at")},
                         {k: before[k] for k in ("request_id", "state", "error", "at")})
        self.assertEqual(after["active"]["backend"], "rknn")


class FramesTest(unittest.TestCase):
    def test_selfcheck_frames_are_shipped_with_the_package(self):
        frames = person_accel.frames()
        self.assertEqual([(name, has) for name, _, has in frames], [("person", True), ("empty", False)])
        self.assertTrue(all(frame.shape == (720, 1280, 3) for _, frame, _ in frames))


if __name__ == "__main__":
    unittest.main(verbosity=2)
