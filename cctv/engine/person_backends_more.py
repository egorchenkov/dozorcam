"""Другие ускорители детектора людей: OpenVINO, CUDA/TensorRT, Hailo-8.

Тот же контракт, что у ``rknn`` (``person_backends``): интерфейс ``cv2.dnn.Net``
(``setInput``/``forward``, выход — форма исходного ONNX, float32), включение через
``person_backend``, поиск без загрузки рантайма и самопроверка против CPU-эталона
(``person_accel``). Рантаймы — необязательные пакеты: в базовом образе их нет, без
пакета или устройства бэкенд просто не находится, и движок остаётся на CPU.

* ``openvino`` — Intel CPU/iGPU/NPU. Граф — тот же ONNX, без конвертации. Устройство —
  ``CCTV_OPENVINO_DEVICE`` (по умолчанию ``AUTO``: NPU/GPU, если есть, иначе CPU).
  Узел iGPU — ``/dev/dri/renderD*`` драйвера ``i915``/``xe``, NPU — ``/dev/accel/accel*``
  драйвера ``intel_vpu``; в контейнер iGPU пробрасывается целиком ``/dev/dri`` без
  passthrough. Пакет ``openvino`` (pip, ~30 МБ колесо).
* ``cuda`` — NVIDIA (и Jetson) через onnxruntime с провайдером TensorRT/CUDA
  (пакет ``onnxruntime-gpu`` или сборка JetPack вместо ``onnxruntime``). Экспериментальный:
  на доступном железе не проверен.
* ``hailo`` — Hailo-8/8L (M.2) через HailoRT (``hailo_platform``): файл
  ``<модель>.hef`` рядом с ONNX и паспорт ``.hef.json`` как у ``.rknn``. Экспериментальный
  каркас: обнаружение (``/dev/hailo*``), загрузка, интерфейс; на железе не проверен.

Не реализуются (только описание в ``docs/models.md``): Coral EdgeTPU, AMD iGPU,
AMD XDNA (VitisAI EP), Apple CoreML.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import threading

import numpy as np

from . import person_backends
from .person_backends import BackendSettings

EXPERIMENTAL = ("cuda", "hailo")
INTEL_GPU_DRIVERS = ("i915", "xe")
DEV = pathlib.Path("/dev")
DRM_CLASS = pathlib.Path("/sys/class/drm")
ACCEL_CLASS = pathlib.Path("/sys/class/accel")


def _driver(node: pathlib.Path) -> str:
    try:
        return (node / "device" / "driver").resolve().name
    except OSError:
        return ""


def _blob_input(blob: np.ndarray | None) -> np.ndarray:
    if blob is None:
        raise RuntimeError("person_model_input_missing")
    return blob


# --- OpenVINO (Intel) --------------------------------------------------------

def intel_devices(dev: pathlib.Path = DEV, drm: pathlib.Path = DRM_CLASS,
                  accel: pathlib.Path = ACCEL_CLASS) -> list[str]:
    """Ускорители Intel, доступные процессу: ``GPU`` (render-узел i915/xe), ``NPU``."""
    found = []
    if any(_driver(node) in INTEL_GPU_DRIVERS and (dev / "dri" / node.name).exists()
           for node in sorted(drm.glob("renderD*"))):
        found.append("GPU")
    if any(_driver(node) == "intel_vpu" and (dev / "accel" / node.name).exists()
           for node in sorted(accel.glob("accel*"))):
        found.append("NPU")
    return found


def openvino_device() -> str:
    return os.environ.get("CCTV_OPENVINO_DEVICE", "").strip().upper() or "AUTO"


class OpenVinoNet:
    """OpenVINO: ONNX компилируется под устройство; запрос вывода — на камеру."""

    _compiled: dict[tuple, object] = {}
    _lock = threading.Lock()

    def __init__(self, path: pathlib.Path, settings: BackendSettings) -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError("person_backend_runtime_missing") from exc
        self.device = openvino_device()
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, self.device)
        with self._lock:
            compiled = self._compiled.get(key)
            if compiled is None:
                core = ov.Core()
                try:
                    compiled = core.compile_model(str(path), self.device,
                                                  {"PERFORMANCE_HINT": "LATENCY"})
                except Exception as exc:
                    raise RuntimeError("person_openvino_compile_failed") from exc
                self._compiled[key] = compiled
        self.compiled = compiled
        self.request = compiled.create_infer_request()
        source = compiled.input(0)
        self.dtype = np.float16 if source.get_element_type().get_type_name() == "f16" else np.float32
        self._blob: np.ndarray | None = None

    def setInput(self, blob: np.ndarray) -> None:  # noqa: N802 — имя как у cv2.dnn.Net
        self._blob = blob

    def forward(self) -> np.ndarray:
        blob = _blob_input(self._blob).astype(self.dtype, copy=False)
        try:
            output = self.request.infer({0: blob})[self.compiled.output(0)]
        except Exception as exc:
            raise RuntimeError("person_openvino_inference_failed") from exc
        return np.asarray(output, dtype=np.float32)


# --- CUDA / TensorRT (NVIDIA, Jetson) через onnxruntime ------------------------

GPU_PROVIDERS = ("TensorrtExecutionProvider", "CUDAExecutionProvider")


def nvidia_present(dev: pathlib.Path = DEV) -> bool:
    """Карта NVIDIA (``/dev/nvidia0``) или Jetson (``/dev/nvhost-*``)."""
    return (dev / "nvidia0").exists() or any(dev.glob("nvhost-*"))


def gpu_providers() -> list[str]:
    """Провайдеры GPU сборки onnxruntime; обычное колесо CPU их не содержит."""
    if importlib.util.find_spec("onnxruntime") is None:
        return []
    import onnxruntime as ort

    return [name for name in GPU_PROVIDERS if name in ort.get_available_providers()]


class CudaNet:
    """onnxruntime на GPU NVIDIA. Экспериментальный: на доступном железе не проверен.

    Провайдер — TensorRT (если есть в сборке), иначе CUDA; CPU в цепочке — для узлов
    графа, которых GPU не умеет. Кэш движка TensorRT — рядом с моделью."""

    def __init__(self, path: pathlib.Path, settings: BackendSettings) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("person_backend_runtime_missing") from exc
        providers = gpu_providers()
        if not providers:
            raise RuntimeError("person_backend_runtime_missing")
        chain: list = []
        for name in providers:
            if name == "TensorrtExecutionProvider":
                chain.append((name, {"trt_engine_cache_enable": True,
                                     "trt_engine_cache_path": str(path.parent),
                                     "trt_fp16_enable": True}))
            else:
                chain.append(name)
        options = ort.SessionOptions()
        options.log_severity_level = 3
        try:
            self.session = ort.InferenceSession(str(path), options, providers=[*chain, "CPUExecutionProvider"])
        except Exception as exc:
            raise RuntimeError("person_cuda_session_failed") from exc
        if not set(self.session.get_providers()) & set(GPU_PROVIDERS):
            raise RuntimeError("person_cuda_provider_unavailable")  # тихо ушёл бы на CPU
        source = self.session.get_inputs()[0]
        self.input, self.output = source.name, self.session.get_outputs()[0].name
        self.dtype = np.float16 if source.type == "tensor(float16)" else np.float32
        self._blob: np.ndarray | None = None

    def setInput(self, blob: np.ndarray) -> None:  # noqa: N802
        self._blob = blob

    def forward(self) -> np.ndarray:
        blob = _blob_input(self._blob).astype(self.dtype, copy=False)
        try:
            output = self.session.run([self.output], {self.input: blob})[0]
        except Exception as exc:
            raise RuntimeError("person_cuda_inference_failed") from exc
        return output.astype(np.float32, copy=False)


# --- Hailo-8 / 8L (M.2) -----------------------------------------------------

def hailo_devices(dev: pathlib.Path = DEV) -> list[pathlib.Path]:
    return sorted(dev.glob("hailo[0-9]*"))


def hef_file(model: pathlib.Path) -> pathlib.Path:
    """``yolov5n.onnx`` → ``yolov5n.hef`` рядом; явный ``.hef`` — как есть."""
    return model if model.suffix == ".hef" else model.with_suffix(".hef")


class HailoNet:
    """HailoRT: ``.hef`` (компилятор Hailo DFC, INT8) + паспорт ``.hef.json``.

    Экспериментальный каркас, на железе не проверен. Как у ``rknn``, сеть отрезается
    до слоя Detect: выход — свёртки головы, раскладка — ``decode_yolov5``; вход —
    кадр uint8 NHWC (``input_scale`` паспорта переводит blob обратно). Устройство
    одно на процесс у HailoRT (VDevice), камеры зовут его по очереди."""

    _lock = threading.Lock()

    def __init__(self, path: pathlib.Path, settings: BackendSettings) -> None:
        self.file = hef_file(path)
        meta_path = self.file.with_name(self.file.name + ".json")
        if not self.file.is_file() or not meta_path.is_file():
            raise RuntimeError("person_hailo_model_missing")
        self.meta = json.loads(meta_path.read_text())
        if self.meta.get("decode") not in ("yolov5", "none"):
            raise RuntimeError("person_hailo_decode_unknown")
        try:
            import hailo_platform as hp
        except ImportError as exc:
            raise RuntimeError("person_backend_runtime_missing") from exc
        try:
            hef = hp.HEF(str(self.file))
            self.device = hp.VDevice()
            params = hp.ConfigureParams.create_from_hef(hef, interface=hp.HailoStreamInterface.PCIe)
            self.group = self.device.configure(hef, params)[0]
            self.group_params = self.group.create_params()
            self.inputs = hp.InputVStreamParams.make(self.group, format_type=hp.FormatType.UINT8)
            self.outputs = hp.OutputVStreamParams.make(self.group, format_type=hp.FormatType.FLOAT32)
            self.input_name = hef.get_input_vstream_infos()[0].name
            self.output_names = [info.name for info in hef.get_output_vstream_infos()]
            self.hp = hp
        except Exception as exc:
            raise RuntimeError("person_hailo_runtime_failed") from exc
        self._blob: np.ndarray | None = None

    def setInput(self, blob: np.ndarray) -> None:  # noqa: N802
        self._blob = blob

    def forward(self) -> np.ndarray:
        blob = _blob_input(self._blob)
        image = np.clip(blob[0].transpose(1, 2, 0) * float(self.meta["input_scale"]), 0, 255)
        image = np.ascontiguousarray(image.round().astype(np.uint8))[None]
        hp = self.hp
        try:
            with self._lock, self.group.activate(self.group_params), \
                    hp.InferVStreams(self.group, self.inputs, self.outputs) as pipeline:
                result = pipeline.infer({self.input_name: image})
        except Exception as exc:
            raise RuntimeError("person_hailo_inference_failed") from exc
        heads = [np.asarray(result[name], dtype=np.float32) for name in self.output_names]
        heads = [head.transpose(0, 3, 1, 2) if head.ndim == 4 else head for head in heads]  # NHWC → NCHW
        if self.meta["decode"] == "yolov5":
            return person_backends.decode_yolov5(heads, self.meta["strides"], self.meta["anchors"],
                                                 keep=person_backends.PERSON_ROW)
        return heads[0]


# --- регистрация -------------------------------------------------------------

OPENERS = {"openvino": OpenVinoNet, "cuda": CudaNet, "hailo": HailoNet}


def _openvino_candidate():
    if importlib.util.find_spec("openvino") is None:
        return None  # без пакета ускоритель не ищется: базовый образ его не несёт
    found = intel_devices()
    device = openvino_device()
    requested = os.environ.get("CCTV_PERSON_BACKEND", "").strip().lower() or "auto"
    if device == "AUTO" and not found and requested != "openvino":
        return None  # в auto CPU Intel через OpenVINO не подменяет CPU-эталон
    return (device if device != "AUTO" else ("+".join(found) or "CPU"), None)


def _cuda_candidate():
    if not nvidia_present():
        return None
    return ("nvidia", None if gpu_providers() else "cuda_runtime_missing")


def _hailo_candidate():
    if not hailo_devices():
        return None
    return ("hailo8", None if importlib.util.find_spec("hailo_platform") else "hailo_runtime_missing")


# Порядок — приоритет после rknn: CUDA, OpenVINO (iGPU/NPU), Hailo.
DETECTORS = {"cuda": _cuda_candidate, "openvino": _openvino_candidate, "hailo": _hailo_candidate}


def register(backends=person_backends) -> None:
    """Добавить бэкенды в ``person_backends`` и поиск в ``person_accel`` (если он есть)."""
    for name, opener in OPENERS.items():
        if name not in backends.BACKENDS:
            backends.BACKENDS = (*backends.BACKENDS, name)
        backends._OPENERS.setdefault(name, opener)
    if hasattr(backends, "ACCELERATED"):
        backends.ACCELERATED = (*backends.ACCELERATED, *(n for n in OPENERS if n not in backends.ACCELERATED))
    person_accel = sys.modules.get(f"{__package__}.person_accel")
    if person_accel is None or not hasattr(person_accel, "DETECTORS"):
        return  # person_accel зовёт register() сам в конце импорта
    for name, detect in DETECTORS.items():
        def candidate(detect=detect, name=name):
            item = detect()
            return None if item is None else person_accel.Candidate(name, item[0], item[1])
        person_accel.DETECTORS.setdefault(name, candidate)
