"""Выбор ускорителя детектора людей: поиск, самопроверка, тихий откат на CPU.

``person_backend = auto`` (по умолчанию) — движок сам ищет ускоритель на узле и
включает его, только если тот прошёл самопроверку: те же кадры (``selfcheck/``:
человек, вклеенный в сцену, и сцена без людей) через ускоритель и через CPU-эталон
(``onnxruntime``, без пакета — ``cv2``), уверенность и рамка должны совпасть с
допуском. Любая ошибка — нет устройства, нет рантайма, нет файла модели под
ускоритель, сбой или расхождение — откат на CPU-эталон: одна строка в журнале
(``person_backend_selected``) и причина в статусе модели (бот, ``/model``).
Ускорителя на узле нет (x86 без устройств, ВМ, ARM без NPU) — это не ошибка:
``reason=no_accelerator``, «ускорителя нет, CPU».

Явный ``rknn`` ищет только NPU Rockchip и проходит ту же самопроверку; при провале
уступает CPU так же — ускоритель никогда не гасит детекцию. ``cpu``, ``onnxruntime``
и ``cv2`` ничего не ищут и не проверяют.

Выбор делается на файл модели (у другой модели может не быть ``.rknn``) один раз на
процесс; смена модели из бота проверяет новую заново.
"""
from __future__ import annotations

import dataclasses
import math
import pathlib
import threading
import time
from collections.abc import Callable

import numpy as np

from . import person_backends, person_backends_more
from .person_backends import BackendSettings

SELFCHECK_DIR = pathlib.Path(__file__).resolve().parent / "selfcheck"
# Допуск самопроверки. INT8 на NPU занижает уверенность в среднем на 0.02, по кадрам —
# до 0.17 (docs/models.md, 115 кадров); на чистом эталонном кадре расхождение много
# меньше. 0.15 пропускает честное квантование и ловит сломанный граф, чужой паспорт
# или неверный вход (там уверенность падает к нулю или рамка уезжает).
SCORE_TOLERANCE = 0.15
MIN_BOX_IOU = 0.5
# Место человека в сцене самопроверки, доли кадра (x, y, w, h) — как в тесте эталонов.
PASTE = (0.5, 1 / 3, 0.4, 0.4)


@dataclasses.dataclass(frozen=True)
class Choice:
    """Чем исполняется модель и почему."""
    backend: BackendSettings
    requested: str
    reason: str  # selfcheck_ok | no_accelerator | cpu_requested | <код отката>
    accelerator: str | None = None  # «rk3576» — найденный ускоритель, даже если откатились
    detail: tuple[tuple[str, object], ...] = ()

    @property
    def accelerated(self) -> bool:
        return self.backend.name in person_backends.ACCELERATED

    def status(self) -> dict:
        """Для ``person_model.status.json`` → бот."""
        value = {"requested": self.requested, "backend": self.backend.name, "reason": self.reason}
        if self.accelerator:
            value["accelerator"] = self.accelerator
        value.update(self.detail)
        return value

    def log_line(self, model: pathlib.Path) -> str:
        extra = "".join(f" {key}={value}" for key, value in self.detail)
        return (f"person_backend_selected file={model.name} requested={self.requested} "
                f"{person_backends.describe(self.backend)} reason={self.reason}"
                f"{f' accelerator={self.accelerator}' if self.accelerator else ''}{extra}")


@dataclasses.dataclass(frozen=True)
class Candidate:
    backend: str
    accelerator: str  # что показать человеку: SoC, модель GPU…
    problem: str | None = None  # код, если ускоритель есть, но взять его нельзя


def _rknn_candidate() -> Candidate | None:
    chip = person_backends.soc()
    if chip is None:
        return None
    if not person_backends.rknn_devices():
        # NPU на плате есть, но его узлы не проброшены в контейнер (compose без них).
        return Candidate("rknn", chip, "rknn_device_missing")
    return Candidate("rknn", chip)


# Порядок — приоритет. Поиск дешёвый (sysfs, без загрузки рантайма).
DETECTORS: dict[str, Callable[[], Candidate | None]] = {"rknn": _rknn_candidate}


def candidates(requested: str) -> list[Candidate]:
    names = list(DETECTORS) if requested == "auto" else [requested]
    found = []
    for name in names:
        try:
            item = DETECTORS[name]()
        except Exception:  # поиск не должен ронять старт: sysfs бывает странным
            item = None
        if item is not None:
            found.append(item)
    return found


# --- самопроверка ----------------------------------------------------------

def frames() -> list[tuple[str, np.ndarray, bool]]:
    """(имя, кадр 1280×720, есть ли человек) — человек вклеен в известное место сцены."""
    import cv2

    scene = cv2.imread(str(SELFCHECK_DIR / "empty.jpg"))
    person = cv2.imread(str(SELFCHECK_DIR / "person.jpg"))
    if scene is None or person is None:
        raise RuntimeError("person_selfcheck_frames_missing")
    scene = cv2.resize(scene, (1280, 720))
    framed = scene.copy()
    x, y, w, h = (round(v * s) for v, s in zip(PASTE, (1280, 720, 1280, 720)))
    framed[y:y + h, x:x + w] = cv2.resize(person, (w, h))
    return [("person", framed, True), ("empty", scene, False)]


def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w, h = min(ax2, bx2) - max(a[0], b[0]), min(ay2, by2) - max(a[1], b[1])
    inter = max(0.0, w) * max(0.0, h)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def selfcheck(candidate, reference) -> dict:
    """Ускоритель против CPU-эталона на кадрах самопроверки; провал — RuntimeError.

    ``candidate``/``reference`` — детекторы одного семейства (``detect(frame)``).
    Человек, которого эталон видит, должен найтись в том же месте; уверенность —
    в пределах допуска на обоих кадрах."""
    detail: dict = {}
    worst = 0.0
    for name, frame, has_person in frames():
        started = time.monotonic()
        _, score, box = candidate.detect(frame)
        candidate_ms = (time.monotonic() - started) * 1000
        started = time.monotonic()
        _, ref_score, ref_box = reference.detect(frame)
        reference_ms = (time.monotonic() - started) * 1000
        if not (math.isfinite(score) and all(math.isfinite(v) for v in box)):
            raise RuntimeError("person_selfcheck_output_invalid")
        delta = abs(score - ref_score)
        worst = max(worst, delta)
        if delta > SCORE_TOLERANCE:
            raise RuntimeError(f"person_selfcheck_score_mismatch frame={name} "
                               f"score={score:.3f} cpu={ref_score:.3f}")
        if has_person and ref_score >= getattr(reference, "confidence", 0.0):
            overlap = iou(box, ref_box)
            detail["iou"] = round(overlap, 2)
            if overlap < MIN_BOX_IOU:
                raise RuntimeError(f"person_selfcheck_box_mismatch frame={name} iou={overlap:.2f}")
        if has_person:
            detail.update(score=round(score, 3), cpu_score=round(ref_score, 3))
        # Первый прогон греет рантайм; честнее время второго кадра.
        detail.update(ms=round(candidate_ms), cpu_ms=round(reference_ms))
    detail["delta"] = round(worst, 3)
    return detail


# --- выбор -----------------------------------------------------------------

_choices: dict[tuple, Choice] = {}
_lock = threading.Lock()


def _key(model: pathlib.Path, family: str, settings: BackendSettings) -> tuple:
    stat = model.stat()
    return (str(model.resolve()), stat.st_mtime_ns, stat.st_size, family, settings)


def cpu_choice(settings: BackendSettings, reason: str, accelerator: str | None = None,
               detail: tuple = ()) -> Choice:
    backend = dataclasses.replace(settings, name=person_backends.cpu_backend())
    return Choice(backend, settings.name, reason, accelerator, detail)


def choose(model: pathlib.Path, family: str, build: Callable[[BackendSettings], object],
           settings: BackendSettings | None = None,
           log: Callable[[str], None] = lambda line: print(line, flush=True)) -> Choice:
    """Бэкенд для файла модели. ``build(backend)`` — детектор семейства на бэкенде.

    Ошибка CPU-эталона (файл не ONNX, чужое семейство) не прячется: она же была бы
    и без ускорителя, и разбирает её вызывающий (откат модели)."""
    settings = settings or person_backends.configured()
    if settings.name not in ("auto", *person_backends.ACCELERATED):
        return Choice(settings, settings.name, "cpu_requested")
    if not model.is_file():
        return cpu_choice(settings, "model_missing")  # детектор скажет person_model_missing
    key = _key(model, family, settings)
    with _lock:
        cached = _choices.get(key)
        if cached is not None:
            return cached
        choice = _choose(model, build, settings)
        _choices[key] = choice
    log(choice.log_line(model))
    return choice


def _choose(model: pathlib.Path, build, settings: BackendSettings) -> Choice:
    found = candidates(settings.name)
    if not found:
        return cpu_choice(settings, "no_accelerator" if settings.name == "auto" else f"{settings.name}_not_found")
    reference = None
    failure: Choice | None = None
    for candidate in found:
        if candidate.problem:
            failure = failure or cpu_choice(settings, candidate.problem, candidate.accelerator)
            continue
        backend = dataclasses.replace(settings, name=candidate.backend)
        try:
            accelerated = build(backend)
        except Exception as exc:
            code = str(exc).split()[0] if str(exc) else type(exc).__name__
            failure = failure or cpu_choice(settings, code, candidate.accelerator)
            continue
        if reference is None:
            reference = build(dataclasses.replace(settings, name=person_backends.cpu_backend()))
        try:
            detail = selfcheck(accelerated, reference)
        except Exception as exc:
            text = str(exc) or type(exc).__name__
            code, _, rest = text.partition(" ")
            if not code.startswith("person_selfcheck_"):  # сбой рантайма на прогоне
                code, rest = f"{candidate.backend}_inference_failed", text
            failure = failure or cpu_choice(settings, code, candidate.accelerator,
                                            (("error", rest.replace(" ", ",")),) if rest else ())
            continue
        return Choice(backend, settings.name, "selfcheck_ok", candidate.accelerator, tuple(detail.items()))
    return failure


def peek(model: pathlib.Path, family: str, settings: BackendSettings | None = None) -> Choice | None:
    """Уже сделанный выбор (без поиска и самопроверки) или None — мост, тесты."""
    try:
        settings = settings or person_backends.configured()
        return _choices.get(_key(model, family, settings))
    except (OSError, ValueError):
        return None


def reset() -> None:
    """Забыть выборы (тесты)."""
    with _lock:
        _choices.clear()


person_backends_more.register()
