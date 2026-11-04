#!/usr/bin/env python3
"""Бэкенды детектора людей: выбор по настройке, потоки и одинаковый выход cv2/onnxruntime."""
from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

from cctv.engine import person_backends
from cctv.engine.person_backends import BackendSettings
from cctv.engine.person_detector import PersonDetector
from cctv.engine.person_detector_yolov8 import Yolov8PersonDetector
from cctv.engine.person_detector_yolox import YoloxPersonDetector

try:
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    import onnxruntime  # noqa: F401
except ImportError:  # пакет onnx — только для сборки тестовых графов (requirements-dev)
    onnx = None


class ResolveTest(unittest.TestCase):
    def test_default_is_auto_and_cpu_is_onnxruntime_checked_against_cv2_on_frames(self):
        self.assertEqual(person_backends.resolve({}), BackendSettings("auto", 0))
        self.assertEqual(person_backends.resolve({"CCTV_PERSON_BACKEND": " "}).name, "auto")
        self.assertEqual(BackendSettings().name, "auto")
        with mock.patch.object(person_backends.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(person_backends.resolve({"CCTV_PERSON_BACKEND": "cpu"}), BackendSettings("onnxruntime", 0))
            self.assertEqual(person_backends.cpu_backend(), "onnxruntime")

    def test_cpu_without_onnxruntime_package_is_cv2_but_explicit_choice_stays(self):
        with mock.patch.object(person_backends.importlib.util, "find_spec", return_value=None):
            self.assertEqual(person_backends.resolve({"CCTV_PERSON_BACKEND": "cpu"}), BackendSettings("cv2", 0))
            self.assertEqual(person_backends.cpu_backend(), "cv2")
            self.assertEqual(person_backends.resolve({"CCTV_PERSON_BACKEND": "onnxruntime"}).name, "onnxruntime")

    def test_backend_and_threads_from_config(self):
        settings = person_backends.resolve({"CCTV_PERSON_BACKEND": "ONNXRuntime", "CCTV_PERSON_THREADS": "2"})
        self.assertEqual(settings, BackendSettings("onnxruntime", 2))

    def test_bad_values_are_errors_not_silent_defaults(self):
        for env, error in (({"CCTV_PERSON_BACKEND": "gpu"}, "person_backend_unknown"),
                           ({"CCTV_PERSON_THREADS": "many"}, "person_threads_invalid"),
                           ({"CCTV_PERSON_THREADS": "-1"}, "person_threads_invalid")):
            with self.subTest(env=env):
                with self.assertRaisesRegex(ValueError, error):
                    person_backends.resolve(env)

    def test_log_line_names_backend_and_threads(self):
        self.assertEqual(person_backends.describe(BackendSettings("cv2")), "backend=cv2")
        self.assertEqual(person_backends.describe(BackendSettings("onnxruntime", 3)), "backend=onnxruntime threads=3")
        with mock.patch.object(person_backends, "default_threads", return_value=4):
            self.assertEqual(person_backends.describe(BackendSettings("onnxruntime")),
                             "backend=onnxruntime threads=4(auto)")


class ThreadsTest(unittest.TestCase):
    def cgroup(self, text: str | None) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        if text is not None:
            (root / "cpu.max").write_text(text)
        return root

    def test_container_quota_limits_cores(self):
        with mock.patch("os.sched_getaffinity", return_value=set(range(8))):
            self.assertEqual(person_backends.available_cpus(self.cgroup("150000 100000\n")), 2)
            self.assertEqual(person_backends.available_cpus(self.cgroup("50000 100000\n")), 1)
            self.assertEqual(person_backends.available_cpus(self.cgroup("max 100000\n")), 8)
            self.assertEqual(person_backends.available_cpus(self.cgroup(None)), 8)

    def test_affinity_limits_cores(self):
        with mock.patch("os.sched_getaffinity", return_value={0, 1, 2}):
            self.assertEqual(person_backends.available_cpus(self.cgroup("max 100000\n")), 3)

    def test_default_is_all_cores_but_at_most_four(self):
        self.assertEqual([person_backends.default_threads(n) for n in (1, 2, 4, 8, 64)], [1, 2, 4, 4, 4])


class RknnHelpersTest(unittest.TestCase):
    def test_rknn_file_is_next_to_onnx_by_soc(self):
        model = pathlib.Path("/models/yolov5n.onnx")
        self.assertEqual(person_backends.rknn_file(model, "rk3576"), pathlib.Path("/models/yolov5n.rk3576.rknn"))
        self.assertEqual(person_backends.rknn_file(pathlib.Path("/m/x.rknn")), pathlib.Path("/m/x.rknn"))

    def test_rknn_file_of_precision_variant_falls_back_to_base_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            model = root / "yolov5n.fp32.onnx"
            self.assertEqual(person_backends.rknn_file(model, "rk3576"), root / "yolov5n.fp32.rk3576.rknn")
            (root / "yolov5n.rk3576.rknn").write_bytes(b"x")
            self.assertEqual(person_backends.rknn_file(model, "rk3576"), root / "yolov5n.rk3576.rknn")
            self.assertEqual(person_backends.rknn_file(root / "yolov5n.int8.onnx", "rk3576"), root / "yolov5n.rk3576.rknn")
            self.assertEqual(person_backends.rknn_file(root / "yolov5n.onnx", "rk3576"), root / "yolov5n.rk3576.rknn")
            (root / "yolov5n.fp32.rk3576.rknn").write_bytes(b"x")
            self.assertEqual(person_backends.rknn_file(model, "rk3576"), root / "yolov5n.fp32.rk3576.rknn")

    def test_soc_from_device_tree(self):
        with tempfile.NamedTemporaryFile() as tmp:
            pathlib.Path(tmp.name).write_bytes(b"vendor,some-board\0rockchip,rk3576\0")
            self.assertEqual(person_backends.soc(pathlib.Path(tmp.name)), "rk3576")
        self.assertIsNone(person_backends.soc(pathlib.Path("/nonexistent/compatible")))

    def test_yolov5_head_decodes_to_onnx_layout(self):
        # Один уровень, один якорь, ячейка (строка 1, столбец 2): нули до sigmoid → 0.5.
        head = np.zeros((1, 6, 2, 3), dtype=np.float32)
        rows = person_backends.decode_yolov5([head], [8], [[10.0, 20.0]])
        self.assertEqual(rows.shape, (1, 6, 6))
        cx, cy, w, h, obj, person = rows[0, 1 * 3 + 2]
        self.assertAlmostEqual(cx, (0.5 * 2 - 0.5 + 2) * 8)
        self.assertAlmostEqual(cy, (0.5 * 2 - 0.5 + 1) * 8)
        self.assertAlmostEqual(w, 10.0)
        self.assertAlmostEqual(h, 20.0)
        self.assertAlmostEqual(obj * person, 0.25)

    def test_yolov5_head_decode_keeps_person_row_only(self):
        # NPU отдаёт голову целиком; движку нужны рамка, objectness и person — те же числа.
        rng = np.random.default_rng(1)
        heads = [rng.normal(size=(1, 3 * 85, n, n)).astype(np.float32) for n in (4, 2)]
        anchors = [[10.0, 13.0, 16.0, 30.0, 33.0, 23.0], [30.0, 61.0, 62.0, 45.0, 59.0, 119.0]]
        full = person_backends.decode_yolov5(heads, [8, 16], anchors)
        short = person_backends.decode_yolov5(heads, [8, 16], anchors, keep=person_backends.PERSON_ROW)
        self.assertEqual(full.shape, (1, 3 * (16 + 4), 85))
        self.assertEqual(short.shape, (1, 3 * (16 + 4), 6))
        np.testing.assert_allclose(short, full[..., :6], rtol=1e-6)

    def test_rknn_net_runs_through_c_api(self):
        # Поддельный librknnrt: вход — кадр uint8 NHWC, выход — головы float32 NCHW → разбор как у ONNX.
        import ctypes

        rng = np.random.default_rng(2)
        heads = [rng.normal(size=(1, 3 * 85, n, n)).astype(np.float32) for n in (4, 2)]
        anchors = [[10.0, 13.0, 16.0, 30.0, 33.0, 23.0], [30.0, 61.0, 62.0, 45.0, 59.0, 119.0]]
        seen, calls = {}, []

        class Lib:
            @staticmethod
            def rknn_init(ctx, model, size, flag, extend):
                seen["model"] = ctypes.string_at(model, size)
                return 0

            @staticmethod
            def rknn_query(ctx, cmd, out, size):
                target = out._obj
                if cmd == 0:
                    target.n_input, target.n_output = 1, len(heads)
                else:
                    target.n_dims, target.fmt = 4, 0
                    for i, d in enumerate(heads[target.index].shape):
                        target.dims[i] = d
                return 0

            @staticmethod
            def rknn_inputs_set(ctx, count, source):
                item = source._obj
                seen["input"] = np.frombuffer(ctypes.string_at(item.buf, item.size), np.uint8)
                return 0

            @staticmethod
            def rknn_run(ctx, extend):
                return 0

            @staticmethod
            def rknn_outputs_get(ctx, count, outputs, extend):
                for item in outputs:
                    assert item.want_float == 1
                    item.buf, item.size = heads[item.index].ctypes.data, heads[item.index].nbytes
                return 0

            @staticmethod
            def rknn_outputs_release(ctx, count, outputs):
                calls.append("release")
                return 0

            @staticmethod
            def rknn_destroy(ctx):
                calls.append("destroy")
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            model = pathlib.Path(tmp) / "m.rk3576.rknn"
            model.write_bytes(b"RKNN")
            meta = {"decode": "yolov5", "strides": [8, 16], "anchors": anchors, "input_scale": 255.0}
            model.with_name(model.name + ".json").write_text(person_backends.json.dumps(meta))
            with mock.patch.object(ctypes, "CDLL", return_value=Lib()):
                net = person_backends.RknnNet(model, BackendSettings("rknn"))
                blob = np.full((1, 3, 2, 2), 0.5, dtype=np.float32)
                blob[0, 0] = 1.0
                net.setInput(blob)
                rows = net.forward()
                del net
        self.assertEqual(seen["model"], b"RKNN")
        np.testing.assert_array_equal(seen["input"].reshape(2, 2, 3)[0, 0], [255, 128, 128])
        expected = person_backends.decode_yolov5(heads, [8, 16], anchors, keep=person_backends.PERSON_ROW)
        np.testing.assert_allclose(rows, expected, rtol=1e-6)
        self.assertEqual(calls, ["release", "destroy"])

    def test_missing_rknn_library_is_a_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = pathlib.Path(tmp) / "m.rk3576.rknn"
            model.write_bytes(b"RKNN")
            model.with_name(model.name + ".json").write_text('{"decode": "yolov5"}')
            with mock.patch.dict(person_backends.os.environ, {"CCTV_RKNN_LIB": str(pathlib.Path(tmp) / "none.so")}):
                with self.assertRaisesRegex(RuntimeError, "person_backend_runtime_missing"):
                    person_backends.RknnNet(model, BackendSettings("rknn"))

    def test_missing_rknn_file_is_a_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "person_rknn_model_missing"):
                person_backends.RknnNet(pathlib.Path(tmp) / "m.rk3576.rknn", BackendSettings("rknn"))


def _graph(path: pathlib.Path, size: int, strides: tuple[int, ...], channels: int, transpose: bool,
           raw: bool = False, fp16: bool = False, seed: int = 0) -> None:
    """Граф с формой выхода семейства: свёртки по уровням → (1, N, C) или (1, C, N), sigmoid.

    ``raw`` — как у YOLOX: вход 0..255, координаты — сдвиги в клетках, а не пиксели.
    """
    rng = np.random.default_rng(seed)
    dtype = np.float16 if fp16 else np.float32
    elem = TensorProto.FLOAT16 if fp16 else TensorProto.FLOAT
    nodes, inits, levels = [], [], []
    for i, stride in enumerate(strides):
        weight = (rng.normal(0, 0.6, (channels, 3, stride, stride)) / stride / (255 if raw else 1)).astype(dtype)
        inits.append(numpy_helper.from_array(weight, f"w{i}"))
        nodes.append(helper.make_node("Conv", ["x", f"w{i}"], [f"c{i}"], kernel_shape=[stride, stride],
                                      strides=[stride, stride]))
        nodes.append(helper.make_node("Reshape", [f"c{i}", "shape"], [f"r{i}"]))
        levels.append(f"r{i}")
    inits.append(numpy_helper.from_array(np.array([1, channels, -1], dtype=np.int64), "shape"))
    nodes.append(helper.make_node("Concat", levels, ["cat"], axis=2))
    last = "cat"
    if transpose:
        nodes.append(helper.make_node("Transpose", ["cat"], ["t"], perm=[0, 2, 1]))
        last = "t"
    nodes.append(helper.make_node("Sigmoid", [last], ["s"]))
    # Координаты в пикселях входа, как у экспорта: первые четыре канала × size.
    scale = np.ones(channels, dtype=dtype)
    scale[:4] = 1 if raw else size
    inits.append(numpy_helper.from_array(scale if transpose else scale.reshape(channels, 1), "scale"))
    nodes.append(helper.make_node("Mul", ["s", "scale"], ["y"]))
    count = sum((size // stride) ** 2 for stride in strides)
    shape = [1, count, channels] if transpose else [1, channels, count]
    graph = helper.make_graph(nodes, "family", [helper.make_tensor_value_info("x", elem, [1, 3, size, size])],
                              [helper.make_tensor_value_info("y", elem, shape)], inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))


@unittest.skipIf(onnx is None, "нужны onnx и onnxruntime (requirements-dev)")
class SameOutputTest(unittest.TestCase):
    """Адаптеры семейств на cv2 и onnxruntime: та же рамка, уверенность и решение при пороге."""

    FAMILIES = (
        # семейство, класс, вход, страйды уровней, каналы, (1, N, C)?, как у YOLOX?
        ("yolov5", PersonDetector, 640, (8, 16, 32), 85, True, False),
        ("yolox", YoloxPersonDetector, 416, (8, 16, 32), 85, True, True),
        ("yolov8", Yolov8PersonDetector, 640, (8, 16, 32), 84, False, False),
    )

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        rng = np.random.default_rng(7)
        self.frames = [rng.integers(0, 256, (360, 640, 3), dtype=np.uint8) for _ in range(4)]

    def detectors(self, cls, path, threads=1):
        return (cls(path, 0.5, backend=BackendSettings("cv2")),
                cls(path, 0.5, backend=BackendSettings("onnxruntime", threads)))

    def test_same_fields_score_box_and_threshold_decision(self):
        for family, cls, size, strides, channels, transpose, raw in self.FAMILIES:
            with self.subTest(family=family):
                path = self.dir / f"{family}.onnx"
                _graph(path, size, strides, channels, transpose, raw)
                reference, candidate = self.detectors(cls, path)
                self.assertIsInstance(candidate.net, person_backends.OnnxRuntimeNet)
                # Порог посередине уверенностей эталона: решение «человек есть» по обе стороны.
                threshold = float(np.median([reference.score(frame) for frame in self.frames]))
                reference.confidence = candidate.confidence = threshold
                decisions = set()
                for frame in self.frames:
                    found_r, score_r, box_r = reference.detect(frame)
                    found_c, score_c, box_c = candidate.detect(frame)
                    self.assertIsInstance(found_c, bool)
                    self.assertIsInstance(score_c, float)
                    self.assertEqual(len(box_c), 4)
                    self.assertAlmostEqual(score_c, score_r, places=4)
                    np.testing.assert_allclose(box_c, box_r, atol=1e-4)
                    self.assertEqual(found_c, found_r)
                    self.assertEqual(found_c, score_c >= threshold)
                    decisions.add(found_c)
                self.assertEqual(decisions, {True, False})

    def test_fp16_graph_runs_on_onnxruntime_with_float32_output(self):
        path16, path32 = self.dir / "m16.onnx", self.dir / "m32.onnx"
        _graph(path16, 640, (8, 16, 32), 85, True, fp16=True)
        _graph(path32, 640, (8, 16, 32), 85, True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            net = person_backends.open_net(path16, BackendSettings("onnxruntime", 1))
            person_backends.open_net(path32, BackendSettings("onnxruntime", 1))
        # Граф по умолчанию для CPU — fp32: на fp16 подсказка конвертации, на fp32 тишина.
        self.assertEqual(out.getvalue().splitlines(), [
            "person_model_precision file=m16.onnx precision=fp16 "
            "hint=scripts/models/convert.py fp32 m16.onnx m16.fp32.onnx"])
        net.setInput(np.zeros((1, 3, 640, 640), dtype=np.float32))
        self.assertEqual(net.forward().dtype, np.float32)
        fp16 = PersonDetector(path16, 0.5, backend=BackendSettings("onnxruntime", 1))
        fp32 = PersonDetector(path32, 0.5, backend=BackendSettings("onnxruntime", 1))
        for frame in self.frames:
            self.assertAlmostEqual(fp16.score(frame), fp32.score(frame), delta=0.01)

    def test_one_session_per_model_shared_by_cameras(self):
        path = self.dir / "m.onnx"
        _graph(path, 640, (8, 16, 32), 85, True)
        first, second = (PersonDetector(path, 0.5, backend=BackendSettings("onnxruntime", 2)) for _ in range(2))
        self.assertIs(first.net._session, second.net._session)
        self.assertIsNot(first.net, second.net)  # вход — у каждой камеры свой
        other_threads = PersonDetector(path, 0.5, backend=BackendSettings("onnxruntime", 1))
        self.assertIsNot(other_threads.net._session, first.net._session)

    def test_broken_file_is_a_load_error_like_cv2(self):
        path = self.dir / "broken.onnx"
        path.write_bytes(b"not onnx")
        with self.assertRaisesRegex(RuntimeError, "person_model_load_failed"):
            PersonDetector(path, 0.5, backend=BackendSettings("onnxruntime", 1))

    def test_missing_runtime_is_a_load_error(self):
        path = self.dir / "m.onnx"
        _graph(path, 640, (8, 16, 32), 85, True)
        with mock.patch.dict("sys.modules", {"onnxruntime": None}):
            with self.assertRaisesRegex(RuntimeError, "person_backend_runtime_missing"):
                PersonDetector(path, 0.5, backend=BackendSettings("onnxruntime", 7))


class ProcessBackendTest(unittest.TestCase):
    def test_detector_without_explicit_backend_uses_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "m.onnx"
            path.write_bytes(b"x")
            with mock.patch.object(person_backends, "_configured", BackendSettings("onnxruntime", 1)), \
                    mock.patch.dict(person_backends._OPENERS, {"onnxruntime": mock.Mock(return_value="net")}):
                self.assertEqual(PersonDetector(path, 0.5).net, "net")


if __name__ == "__main__":
    unittest.main(verbosity=2)
