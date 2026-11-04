"""Бэкенд детектора людей: чем исполняется граф модели.

Семейство модели (``person_models``) разбирает выход сети, бэкенд — исполняет граф.
Интерфейс бэкенда тот же, что у ``cv2.dnn.Net``: ``setInput(blob)`` и
``forward() -> ndarray`` (float32, форма выхода исходного ONNX), поэтому адаптеры
семейств, их пороги и разбор выхода от бэкенда не зависят.

Бэкенды (``CCTV_PERSON_BACKEND``, в ``config.toml`` — ``[engine] person_backend``):

* ``auto`` — по умолчанию: ускоритель, если он есть на узле и прошёл самопроверку
  против CPU-эталона (``person_accel``), иначе ``cpu``. Ускорителя нет — так и пишется
  в журнал и в статус, это не ошибка;
* ``cpu`` — CPU-эталон без поиска ускорителя: ``onnxruntime``, а без пакета — ``cv2``;
* ``onnxruntime`` — onnxruntime, CPU; тот же граф заметно быстрее
  ``cv2`` (на x86 около −40 % на кадр). Одна сессия на файл модели на весь процесс,
  камеры зовут её параллельно (``run`` потокобезопасен). На наборе кадров с людьми
  уверенность и рамки fp32-графов совпали с ``cv2`` до 1e-4; fp16-граф на ARM
  onnxruntime считает в fp16 — расхождение до ~0.02, меньше шага калибровки порога.
  Граф по умолчанию для CPU — fp32 (на fp16 в журнале подсказка конвертации);
* ``cv2`` — OpenCV DNN, CPU, по сети на камеру — как в 0.7 и раньше;
* ``rknn`` — NPU Rockchip через C API ``librknnrt`` (в образе arm64 — ``/usr/lib``):
  файл ``<модель>.<soc>.rknn`` рядом с ONNX и его паспорт
  ``.rknn.json`` (страйды, якоря, множитель входа); голова YOLOv5 раскладывается
  здесь в форму выхода исходного ONNX. Выбранный явно, проходит ту же самопроверку,
  что в ``auto``, и при провале так же уступает ``cpu`` — ускоритель движок не гасит.

Потоки onnxruntime (``CCTV_PERSON_THREADS``, ``[engine] person_threads``): число
потоков одного вывода. Без настройки — ``min(4, ядер)``, где «ядер» — доступные
процессу с учётом лимита контейнера (cgroup ``cpu.max``) и привязки к ядрам;
объяснение — ``default_threads``.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import math
import os
import pathlib
import threading
import weakref
from collections.abc import Mapping

import numpy as np


BACKENDS = ("auto", "cpu", "onnxruntime", "cv2", "rknn")
DEFAULT_BACKEND = "auto"
# Бэкенды ускорителей: их выбирает самопроверка (person_accel), а не open_net.
ACCELERATED = ("rknn",)
MAX_DEFAULT_THREADS = 4
RKNN_SOCS = ("rk3576", "rk3588", "rk3568", "rk3566", "rk3562")
# Строка выхода YOLOv5, которую читает адаптер: x, y, w, h, objectness, person (класс 0).
PERSON_ROW = 6


@dataclasses.dataclass(frozen=True)
class BackendSettings:
    name: str = DEFAULT_BACKEND
    threads: int = 0  # 0 — по политике default_threads()


def _env(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def available_cpus(cgroup_root: pathlib.Path = pathlib.Path("/sys/fs/cgroup")) -> int:
    """Ядра, которые процесс реально получит: привязка к ядрам и квота cgroup v2."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    try:
        quota, period = (cgroup_root / "cpu.max").read_text().split()[:2]
        if quota != "max":
            cpus = min(cpus, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, cpus)


def default_threads(cpus: int | None = None) -> int:
    """Потоков на один вывод без настройки: все доступные ядра, но не больше четырёх.

    Камеры зовут общую сессию параллельно, поэтому при движении на нескольких камерах
    ядра делятся между ними, а одиночное событие получает всё. Дальше четырёх yolov5n
    640 почти не ускоряется, а ядра нужны ffmpeg записи. Потоки
    не крутятся вхолостую в ожидании работы (allow_spinning=0).
    """
    return max(1, min(MAX_DEFAULT_THREADS, available_cpus() if cpus is None else cpus))


def cpu_backend() -> str:
    """CPU-эталон узла: onnxruntime, а где его колёс нет — cv2, как до смены умолчания."""
    return "onnxruntime" if importlib.util.find_spec("onnxruntime") else "cv2"


def resolve(env: Mapping[str, str] = os.environ) -> BackendSettings:
    name = _env(env, "CCTV_PERSON_BACKEND").lower() or DEFAULT_BACKEND
    if name not in BACKENDS:
        raise ValueError("person_backend_unknown")
    if name == "cpu":
        name = cpu_backend()
    raw = _env(env, "CCTV_PERSON_THREADS")
    try:
        threads = int(raw) if raw else 0
    except ValueError:
        raise ValueError("person_threads_invalid") from None
    if threads < 0:
        raise ValueError("person_threads_invalid")
    return BackendSettings(name, threads)


def describe(settings: BackendSettings) -> str:
    """Строка для журнала запуска: бэкенд и (для onnxruntime) потоки."""
    if settings.name == "onnxruntime":
        threads = settings.threads or default_threads()
        return f"backend=onnxruntime threads={threads}{'' if settings.threads else '(auto)'}"
    return f"backend={settings.name}"


# --- cv2.dnn ---------------------------------------------------------------

def _open_cv2(path: pathlib.Path, settings: BackendSettings):
    import cv2

    return cv2.dnn.readNetFromONNX(str(path))


# --- onnxruntime -----------------------------------------------------------

class _OrtSession:
    """Сессия onnxruntime на файл модели — общая для всех камер процесса."""

    def __init__(self, path: pathlib.Path, threads: int) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError("person_backend_runtime_missing") from exc

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        # Ожидающие потоки спят, а не крутятся: рядом ffmpeg записи и другие камеры.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        try:
            self.session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        except Exception as exc:  # битый/не ONNX файл — та же ошибка, что у cv2.dnn
            raise RuntimeError("person_model_load_failed") from exc
        source = self.session.get_inputs()[0]
        self.input = source.name
        self.output = self.session.get_outputs()[0].name
        # Граф fp16 (экспорт yolov5n) ждёт вход fp16; выход приводится к float32.
        self.dtype = np.float16 if source.type == "tensor(float16)" else np.float32
        if self.dtype == np.float16:
            # Граф по умолчанию для CPU — fp32 (docs/models.md): его оценки совпадают с cv2,
            # на x86 он быстрее fp16, на A72 не медленнее. fp16 на ARM считается в fp16 и
            # дрейфует до ~0.02 — работает, но подсказка в журнале один раз на файл.
            print(f"person_model_precision file={path.name} precision=fp16 "
                  f"hint=scripts/models/convert.py fp32 {path.name} {path.stem}.fp32.onnx", flush=True)

    def run(self, blob: np.ndarray) -> np.ndarray:
        output = self.session.run([self.output], {self.input: blob.astype(self.dtype, copy=False)})[0]
        return output.astype(np.float32, copy=False)


_sessions: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_sessions_lock = threading.Lock()


def _session(path: pathlib.Path, threads: int) -> _OrtSession:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, threads)
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = _OrtSession(path, threads)
            _sessions[key] = session
        return session


class OnnxRuntimeNet:
    """Обёртка с интерфейсом cv2.dnn.Net: вход хранится у камеры, сессия общая."""

    def __init__(self, path: pathlib.Path, settings: BackendSettings) -> None:
        self.threads = settings.threads or default_threads()
        self._session = _session(path, self.threads)
        self._blob: np.ndarray | None = None

    def setInput(self, blob: np.ndarray) -> None:  # noqa: N802 — имя как у cv2.dnn.Net
        self._blob = blob

    def forward(self) -> np.ndarray:
        if self._blob is None:
            raise RuntimeError("person_model_input_missing")
        return self._session.run(self._blob)


# --- RKNN (NPU Rockchip) ---------------------------------------------------

DRM_CLASS = pathlib.Path("/sys/class/drm")
DEVICE_TREE = pathlib.Path("/proc/device-tree/compatible")


def _npu_drm(drm: pathlib.Path = DRM_CLASS) -> dict[str, dict[str, str]]:
    """DRM-узлы драйвера rknpu по sysfs: имя узла → поля uevent устройства.

    sysfs виден и в контейнере без проброса устройств (а ``/proc/device-tree`` Docker
    скрывает), поэтому NPU узнаётся и там, где его узлы ``/dev/dri`` не проброшены."""
    found = {}
    for uevent in sorted(drm.glob("*/device/uevent")):
        try:
            fields = dict(line.split("=", 1) for line in uevent.read_text().splitlines() if "=" in line)
        except OSError:
            continue
        if fields.get("DRIVER", "").lower() == "rknpu":
            found[uevent.parent.parent.name] = fields
    return found


def soc(compatible: pathlib.Path = DEVICE_TREE, drm: pathlib.Path = DRM_CLASS) -> str | None:
    """SoC Rockchip с NPU: ``OF_COMPATIBLE`` устройства rknpu (``rockchip,rk3576-rknpu``)
    или device tree (``rockchip,rk3576``); None — не Rockchip либо SoC без NPU."""
    names = [value for fields in _npu_drm(drm).values()
             for key, value in fields.items() if key.startswith("OF_COMPATIBLE_") and key != "OF_COMPATIBLE_N"]
    try:
        names += compatible.read_bytes().decode("ascii", "replace").split("\0")
    except OSError:
        pass
    for name in names:
        vendor, _, chip = name.partition(",")
        chip = chip.removesuffix("-rknpu")
        if vendor == "rockchip" and chip in RKNN_SOCS:
            return chip
    return None


def rknn_file(model: pathlib.Path, chip: str | None = None) -> pathlib.Path:
    """``yolov5n.onnx`` → ``yolov5n.rk3576.rknn`` рядом; явный ``.rknn`` — как есть.

    У ``yolov5n.fp32.onnx`` (конвертация по подсказке fp16 → fp32) своего ``.rknn``
    обычно нет — тогда берётся ``.rknn`` исходной модели ``yolov5n``: так его и называет
    ``scripts/models/convert.py rknn`` (имя до первой точки)."""
    if model.suffix == ".rknn":
        return model
    chip = chip or os.environ.get("CCTV_RKNN_SOC", "").strip() or soc()
    if not chip:
        raise RuntimeError("person_rknn_soc_unknown")
    own = model.with_name(f"{model.stem}.{chip}.rknn")
    base, _, precision = model.stem.rpartition(".")
    if base and precision in ("fp32", "fp16", "int8") and not own.is_file():
        shared = model.with_name(f"{base}.{chip}.rknn")
        if shared.is_file():
            return shared
    return own


def rknn_library() -> str:
    """``CCTV_RKNN_LIB`` или ``librknnrt.so`` с путей загрузчика (образ arm64: /usr/lib)."""
    return os.environ.get("CCTV_RKNN_LIB", "").strip() or "librknnrt.so"


def rknn_devices(dev: pathlib.Path = pathlib.Path("/dev"), drm: pathlib.Path = DRM_CLASS) -> list[pathlib.Path]:
    """Узлы NPU, доступные процессу: пара DRM-узлов драйвера rknpu (``card*`` +
    ``renderD*``; рантайму нужны оба) или ``/dev/rknpu`` старых ядер."""
    found = [dev / "dri" / name for name in _npu_drm(drm) if (dev / "dri" / name).exists()]
    if (dev / "rknpu").exists():
        found.append(dev / "rknpu")
    return found


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-values))


def decode_yolov5(heads: list[np.ndarray], strides: list[int], anchors: list[list[float]],
                  keep: int = 0) -> np.ndarray:
    """Голова YOLOv5 (свёртки до Detect) → ``(1, N, 5 + классы)`` как выход ONNX.

    То же, что делает слой Detect в графе экспорта: sigmoid, ``xy = (2s − 0.5 + сетка)
    · страйд``, ``wh = (2s)² · якорь``; порядок строк — уровень, якорь, строка, столбец.
    Сеть квантуется до этого места: в общем выходе координаты 0..640 и вероятности 0..1
    делили бы одну шкалу INT8, и вероятность легла бы в один-два шага шкалы.
    ``keep`` — сколько первых каналов строки раскладывать (6 = рамка, objectness,
    person — всё, что читает адаптер): sigmoid по 80 классам на ядре A72 стоил втрое
    дороже самого прохода сети на NPU.
    """
    rows = []
    for head, stride, level in zip(heads, strides, anchors):
        _, channels, height, width = head.shape
        count = len(level) // 2
        width_row = channels // count
        values = head.astype(np.float32, copy=False).reshape(count, width_row, height, width)
        values = _sigmoid(values[:, :keep] if keep else values)
        values = values.transpose(0, 2, 3, 1)  # якорь, строка, столбец, 5 + классы
        ys, xs = np.meshgrid(np.arange(height, dtype=np.float32), np.arange(width, dtype=np.float32), indexing="ij")
        grid = np.stack((xs, ys), axis=-1)
        pairs = np.asarray(level, dtype=np.float32).reshape(count, 1, 1, 2)
        xy = (values[..., :2] * 2 - 0.5 + grid) * stride
        wh = (values[..., 2:4] * 2) ** 2 * pairs
        rows.append(np.concatenate((xy, wh, values[..., 4:]), axis=-1).reshape(-1, values.shape[-1]))
    return np.concatenate(rows)[None]


class _RknnApi:
    """Минимум C API ``librknnrt`` (rknn_api.h, RKNPU2) через ctypes.

    Обёртка ``rknn-toolkit-lite2`` не годится: SoC она ищет по зашитому адресу узла
    NPU (от RK3568) и по первой строке device tree, поэтому на плате с собственным
    ``compatible`` (``<вендор>,<плата>``) и на RK3576 не стартует. Рантайму C API
    нужны оба узла NPU в DRM (``card*`` и ``renderD*`` из ``/dev/dri/by-path/*npu*``;
    с одним render-узлом — «failed to open rknpu module») или ``/dev/rknpu``.
    """

    MAX_DIMS, MAX_NAME = 16, 256
    QUERY_IN_OUT_NUM, QUERY_OUTPUT_ATTR = 0, 2
    TENSOR_UINT8, FORMAT_NHWC = 3, 1

    def __init__(self, library: str) -> None:
        import ctypes

        c = ctypes

        class InOutNum(c.Structure):
            _fields_ = [("n_input", c.c_uint32), ("n_output", c.c_uint32)]

        class TensorAttr(c.Structure):
            _fields_ = [("index", c.c_uint32), ("n_dims", c.c_uint32), ("dims", c.c_uint32 * self.MAX_DIMS),
                        ("name", c.c_char * self.MAX_NAME), ("n_elems", c.c_uint32), ("size", c.c_uint32),
                        ("fmt", c.c_int), ("type", c.c_int), ("qnt_type", c.c_int), ("fl", c.c_int8),
                        ("zp", c.c_int32), ("scale", c.c_float), ("w_stride", c.c_uint32),
                        ("size_with_stride", c.c_uint32), ("pass_through", c.c_uint8), ("h_stride", c.c_uint32)]

        class Input(c.Structure):
            _fields_ = [("index", c.c_uint32), ("buf", c.c_void_p), ("size", c.c_uint32),
                        ("pass_through", c.c_uint8), ("type", c.c_int), ("fmt", c.c_int)]

        class Output(c.Structure):
            _fields_ = [("want_float", c.c_uint8), ("is_prealloc", c.c_uint8), ("index", c.c_uint32),
                        ("buf", c.c_void_p), ("size", c.c_uint32)]

        self.c, self.InOutNum, self.TensorAttr, self.Input, self.Output = c, InOutNum, TensorAttr, Input, Output
        try:
            self.lib = c.CDLL(library)
        except OSError as exc:
            raise RuntimeError("person_backend_runtime_missing") from exc
        context, u32, ptr = c.c_uint64, c.c_uint32, c.c_void_p
        for name, args in (("rknn_init", [c.POINTER(context), ptr, u32, u32, ptr]),
                           ("rknn_query", [context, u32, ptr, u32]),
                           ("rknn_inputs_set", [context, u32, ptr]),
                           ("rknn_run", [context, ptr]),
                           ("rknn_outputs_get", [context, u32, ptr, ptr]),
                           ("rknn_outputs_release", [context, u32, ptr]),
                           ("rknn_destroy", [context])):
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, c.c_int


class RknnNet:
    """NPU Rockchip: ``.rknn`` + паспорт ``.rknn.json`` с конвертации.

    Вход сети на NPU — кадр uint8 NHWC (нормировка вшита при конвертации), поэтому
    blob адаптера переводится обратно множителем ``input_scale`` из паспорта. Выходы
    (свёртки головы) рантайм отдаёт во float32 в раскладке NCHW графа. Контекст не
    потокобезопасен — по контексту на камеру, ядра NPU — выбор рантайма (auto).
    Библиотека — ``CCTV_RKNN_LIB`` или ``librknnrt.so`` из путей загрузчика.
    """

    def __init__(self, path: pathlib.Path, settings: BackendSettings) -> None:
        self.file = rknn_file(path)
        meta_path = self.file.with_name(self.file.name + ".json")
        if not self.file.is_file() or not meta_path.is_file():
            raise RuntimeError("person_rknn_model_missing")
        self.meta = json.loads(meta_path.read_text())
        if self.meta.get("decode") not in ("yolov5", "none"):
            raise RuntimeError("person_rknn_decode_unknown")
        self.api = api = _RknnApi(rknn_library())
        c = api.c
        model = self.file.read_bytes()
        self.ctx = c.c_uint64(0)
        if api.lib.rknn_init(c.byref(self.ctx), c.c_char_p(model), len(model), 0, None) != 0:
            self.ctx = None
            raise RuntimeError("person_rknn_runtime_failed")
        count = api.InOutNum()
        if api.lib.rknn_query(self.ctx, api.QUERY_IN_OUT_NUM, c.byref(count), c.sizeof(count)) != 0:
            raise RuntimeError("person_rknn_runtime_failed")
        self.shapes = []
        for index in range(count.n_output):
            attr = api.TensorAttr(index=index)
            if api.lib.rknn_query(self.ctx, api.QUERY_OUTPUT_ATTR, c.byref(attr), c.sizeof(attr)) != 0:
                raise RuntimeError("person_rknn_runtime_failed")
            self.shapes.append((tuple(attr.dims[:attr.n_dims]), attr.fmt == api.FORMAT_NHWC))
        self._blob: np.ndarray | None = None

    def setInput(self, blob: np.ndarray) -> None:  # noqa: N802
        self._blob = blob

    def _infer(self, image: np.ndarray) -> list[np.ndarray]:
        api, c = self.api, self.api.c
        source = api.Input(index=0, buf=image.ctypes.data, size=image.nbytes, pass_through=0,
                           type=api.TENSOR_UINT8, fmt=api.FORMAT_NHWC)
        if api.lib.rknn_inputs_set(self.ctx, 1, c.byref(source)) != 0 or api.lib.rknn_run(self.ctx, None) != 0:
            raise RuntimeError("person_rknn_inference_failed")
        outputs = (api.Output * len(self.shapes))()
        for index, item in enumerate(outputs):
            item.index, item.want_float = index, 1
        if api.lib.rknn_outputs_get(self.ctx, len(self.shapes), outputs, None) != 0:
            raise RuntimeError("person_rknn_inference_failed")
        try:
            result = []
            for item, (dims, nhwc) in zip(outputs, self.shapes):
                values = np.ctypeslib.as_array(c.cast(item.buf, c.POINTER(c.c_float)), shape=(item.size // 4,))
                if nhwc:  # dims в NHWC → к NCHW графа
                    result.append(values[:math.prod(dims)].reshape(dims).transpose(0, 3, 1, 2).copy())
                else:
                    result.append(values[:math.prod(dims)].reshape(dims).copy())
            return result
        finally:
            api.lib.rknn_outputs_release(self.ctx, len(self.shapes), outputs)

    def forward(self) -> np.ndarray:
        if self._blob is None:
            raise RuntimeError("person_model_input_missing")
        image = np.clip(self._blob[0].transpose(1, 2, 0) * float(self.meta["input_scale"]), 0, 255)
        outputs = self._infer(np.ascontiguousarray(image.round().astype(np.uint8))[None])
        if self.meta["decode"] == "yolov5":
            return decode_yolov5(outputs, self.meta["strides"], self.meta["anchors"], keep=PERSON_ROW)
        return np.asarray(outputs[0], dtype=np.float32)

    def __del__(self) -> None:
        if getattr(self, "ctx", None) is not None:
            try:
                self.api.lib.rknn_destroy(self.ctx)
            except Exception:
                pass


def is_backend_error(exc: BaseException) -> bool:
    """Сбой бэкенда (нет рантайма, нет .rknn), а не файла модели: выбор модели цел."""
    return str(exc).startswith(("person_backend_", "person_rknn_"))


_OPENERS = {"cv2": _open_cv2, "onnxruntime": OnnxRuntimeNet, "rknn": RknnNet}
_configured: BackendSettings | None = None


def configured() -> BackendSettings:
    """Бэкенд процесса по окружению (читается один раз)."""
    global _configured
    if _configured is None:
        _configured = resolve()
    return _configured


def open_net(path: pathlib.Path, settings: BackendSettings | None = None):
    """Граф модели на бэкенде; ``settings`` None — бэкенд процесса, ``auto`` — CPU-эталон
    (ускоритель подставляет ``person_accel`` после самопроверки, а не open_net)."""
    settings = settings or configured()
    if settings.name in ("auto", "cpu"):
        settings = dataclasses.replace(settings, name=cpu_backend())
    return _OPENERS[settings.name](path, settings)


# Другие ускорители (OpenVINO, CUDA, Hailo) — отдельный модуль, тот же контракт.
from . import person_backends_more as _more  # noqa: E402

_more.register()
