"""Другие ускорители: обнаружение без устройств, регистрация, отказ без рантайма."""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cctv.engine import person_backends, person_backends_more as more


class RegistrationTest(unittest.TestCase):
    def test_backends_registered(self):
        for name in ("openvino", "cuda", "hailo"):
            self.assertIn(name, person_backends.BACKENDS)
            self.assertEqual(person_backends.resolve({"CCTV_PERSON_BACKEND": name}).name, name)

    def test_detectors_registered_in_accel(self):
        from cctv.engine import person_accel
        for name in ("cuda", "openvino", "hailo"):
            self.assertIn(name, person_accel.DETECTORS)
            self.assertIn(name, person_backends.ACCELERATED)

    def test_auto_order_and_empty_node(self):
        from cctv.engine import person_accel
        self.assertEqual(list(person_accel.DETECTORS), ["rknn", "cuda", "openvino", "hailo"])
        # Ни пакетов, ни устройств: auto не находит ускорителей — CPU, старт не падает.
        with mock.patch.object(person_backends, "soc", return_value=None), \
                mock.patch.object(more, "nvidia_present", return_value=False), \
                mock.patch.object(more, "hailo_devices", return_value=[]), \
                mock.patch("importlib.util.find_spec", return_value=None):
            self.assertEqual(person_accel.candidates("auto"), [])


class DiscoveryTest(unittest.TestCase):
    def test_nothing_on_empty_node(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            self.assertEqual(more.intel_devices(root, root / "drm", root / "accel"), [])
            self.assertFalse(more.nvidia_present(root))
            self.assertEqual(more.hailo_devices(root), [])

    def test_found_devices(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            (root / "dri").mkdir()
            (root / "dri" / "renderD128").touch()
            (root / "drivers" / "i915").mkdir(parents=True)
            (root / "drm" / "renderD128").mkdir(parents=True)
            (root / "drm" / "renderD128" / "device").mkdir()
            os.symlink(root / "drivers" / "i915", root / "drm" / "renderD128" / "device" / "driver")
            (root / "nvidia0").touch()
            (root / "hailo0").touch()
            self.assertEqual(more.intel_devices(root, root / "drm", root / "accel"), ["GPU"])
            self.assertTrue(more.nvidia_present(root))
            self.assertEqual(more.hailo_devices(root), [root / "hailo0"])

    def test_auto_skips_absent(self):
        with mock.patch.object(more, "nvidia_present", return_value=False), \
                mock.patch.object(more, "hailo_devices", return_value=[]), \
                mock.patch.object(more, "intel_devices", return_value=[]), \
                mock.patch.dict(os.environ, {"CCTV_PERSON_BACKEND": "auto"}):
            self.assertIsNone(more._cuda_candidate())
            self.assertIsNone(more._hailo_candidate())
            self.assertIsNone(more._openvino_candidate())

    def test_device_without_runtime_is_problem(self):
        with mock.patch.object(more, "nvidia_present", return_value=True), \
                mock.patch.object(more, "gpu_providers", return_value=[]):
            self.assertEqual(more._cuda_candidate(), ("nvidia", "cuda_runtime_missing"))
        with mock.patch.object(more, "hailo_devices", return_value=[pathlib.Path("/dev/hailo0")]), \
                mock.patch("importlib.util.find_spec", return_value=None):
            self.assertEqual(more._hailo_candidate(), ("hailo8", "hailo_runtime_missing"))


class OpenFailureTest(unittest.TestCase):
    def test_hailo_missing_hef_is_backend_error(self):
        with tempfile.TemporaryDirectory() as root:
            model = pathlib.Path(root) / "yolov5n.onnx"
            model.write_bytes(b"x")
            with self.assertRaises(RuntimeError) as caught:
                more.HailoNet(model, person_backends.BackendSettings("hailo"))
            self.assertEqual(str(caught.exception), "person_hailo_model_missing")
            self.assertEqual(more.hef_file(model), model.with_suffix(".hef"))

    def test_cuda_without_gpu_runtime(self):
        with mock.patch.object(more, "gpu_providers", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                more.CudaNet(pathlib.Path("/nonexistent.onnx"), person_backends.BackendSettings("cuda"))
            self.assertTrue(person_backends.is_backend_error(caught.exception))


if __name__ == "__main__":
    unittest.main()
