#!/usr/bin/env python3
"""Инструменты графов детектора: convert.py (fp16 → fp32, int8) и bench/backends.py."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    import onnx
    from onnx import TensorProto
    import onnxruntime  # noqa: F401
except ImportError:  # пакет onnx — requirements-dev
    onnx = None


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(onnx is None, "нужны onnx и onnxruntime (requirements-dev)")
class ModelToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.engine.test_person_backends import _graph

        cls.graph = staticmethod(_graph)
        cls.convert = _load("dz_convert", ROOT / "scripts" / "models" / "convert.py")
        cls.bench = _load("dz_bench_backends", ROOT / "bench" / "backends.py")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.frames = self.dir / "frames"
        self.frames.mkdir()
        rng = np.random.default_rng(3)
        for index in range(4):
            cv2.imwrite(str(self.frames / f"f{index}.jpg"), rng.integers(0, 256, (360, 640, 3), dtype=np.uint8))

    def run_quiet(self, function, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = function(*args)
        return result, out.getvalue()

    def test_fp32_export_has_no_fp16_and_same_scores(self):
        source, target = self.dir / "m.onnx", self.dir / "m.fp32.onnx"
        self.graph(source, 640, (8, 16, 32), 85, True, fp16=True)
        _, out = self.run_quiet(self.convert.to_fp32, source, target)
        self.assertIn("fp32:", out)
        model = onnx.load(str(target))
        self.assertFalse([t.name for t in model.graph.initializer if t.data_type == TensorProto.FLOAT16])
        self.assertEqual(model.graph.input[0].type.tensor_type.elem_type, TensorProto.FLOAT)
        data = np.random.default_rng(0).random((1, 3, 640, 640), dtype=np.float32)
        reference = self.convert.run(self.convert.session(source), data)[0]
        got = self.convert.run(self.convert.session(target), data)[0]
        person, _ = self.convert.scores_close(reference, got)
        self.assertLess(person, 0.01)

    def test_int8_keeps_head_convs_in_fp32(self):
        source, target = self.dir / "m.onnx", self.dir / "m.int8.onnx"
        self.graph(source, 640, (8, 16, 32), 85, True)
        _, out = self.run_quiet(self.convert.to_int8, source, target, self.frames, 0)
        self.assertIn("3 свёрток", out)  # у тестового графа все свёртки — голова
        self.assertTrue(target.exists())
        # Голова не квантована — выход совпадает с исходником.
        data = self.convert.blob(next(self.frames.iterdir()))
        reference = self.convert.run(self.convert.session(source), data)[0]
        got = self.convert.run(self.convert.session(target), data)[0]
        np.testing.assert_allclose(got, reference, atol=1e-4)

    def test_head_outputs_found_before_reshape(self):
        source = self.dir / "m.onnx"
        self.graph(source, 640, (8, 16, 32), 85, True)
        self.assertEqual(self.convert.head_outputs(onnx.load(str(source))), ["c0", "c1", "c2"])

    def test_bench_frames_sample_evenly(self):
        files = self.bench.frames(self.frames, 3)
        self.assertEqual([p.name for p in files], ["f0.jpg", "f2.jpg", "f3.jpg"])

    def test_bench_latency_and_compare_on_both_backends(self):
        model = self.dir / "m.onnx"
        self.graph(model, 640, (8, 16, 32), 85, True)
        args = ["--model", str(model), "--runs", "2", "--warmup", "1", "--json"]
        result, out = self.run_quiet(self.bench.main, ["latency", str(self.frames), "--backend", "onnxruntime",
                                                       "--threads", "1", "--threads", "2", *args])
        self.assertEqual(result, 0)
        latency = json.loads(out)
        self.assertEqual([r["threads"] for r in latency["results"]], [1, 2])
        self.assertTrue(all(r["median_ms"] > 0 for r in latency["results"]))
        csv_path = self.dir / "rows.csv"
        _, out = self.run_quiet(self.bench.main, ["compare", str(self.frames), "--backend", "cv2", "--model",
                                                  str(model), "--candidate", f"{model}:onnxruntime",
                                                  "--csv", str(csv_path), "--json"])
        summary = json.loads(out)["summary"][f"{model}:onnxruntime"]
        self.assertLessEqual(summary["max_abs_diff"], 1e-4)
        self.assertEqual(summary["decision_mismatch"], 0)
        self.assertEqual(len(csv_path.read_text().splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
