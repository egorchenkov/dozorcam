"""Выбор детектора людей: семейство модели + файл весов.

Ровно три семейства с разным разбором выхода сети — YOLOv5, YOLOX, YOLOv8-стиль;
плагинов нет. Все адаптеры отдают одно и то же ``detect(frame) -> (found, score,
box)``, поэтому конвейер и фильтр неподвижных объектов модель не различают.

Настройка (переменные окружения движка, ``.env`` в compose):

* ``CCTV_PERSON_MODEL_FAMILY`` — ``yolov5`` | ``yolox`` | ``yolov8``;
* ``CCTV_PERSON_MODEL`` — файл весов: абсолютный путь или имя, которое ищется в
  каталогах ``CCTV_MODEL_DIRS`` (по умолчанию ``/etc/cctv/models`` — сюда
  пользователь кладёт свои файлы через смонтированный конфиг, затем
  ``/usr/share/cctv/models`` — комплект образа); без него — файл семейства по умолчанию;
* ``CCTV_PERSON_CONFIDENCE`` — порог; без него — стартовый порог семейства.

Без семейства сохраняется прежнее поведение: задан ``CCTV_PERSON_MODEL`` или
лежит прежний ``/usr/share/cctv/models/yolov5n.onnx`` — это YOLOv5n как раньше;
иначе (новая установка) — YOLOX-Tiny, единственная модель комплекта
(Apache-2.0, вердикт бенча 03.10.2026, bench/REPORT.md).
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
from collections.abc import Callable, Mapping

import numpy as np

from . import person_detector, person_detector_yolov8, person_detector_yolox


@dataclasses.dataclass(frozen=True)
class Family:
    name: str
    title: str
    default_file: str
    # Стартовый порог до автокалибровки: обоснование — docs/models.md.
    confidence: float
    license: str
    bundled: bool  # входит ли файл по умолчанию в образ
    factory: Callable[[pathlib.Path, float], object]
    # Форма выхода (без батча), по которой файл узнаётся как «своего» семейства.
    # Адаптер разбирает и чужой выход без ошибки (YOLOX на 640 отдаёт (8400, 85) —
    # для YOLOv5-разбора это мусор, а не исключение), поэтому смена модели сверяет
    # число точек: у YOLOv5 3 якоря на ячейку сеток 8/16/32(/64) входа 640, у YOLOX
    # одна точка на ячейку входа 416, у YOLOv8 одна точка на ячейку входа 640,
    # раскладка «каналы, потом точки».
    layout: Callable[[tuple[int, ...]], bool]


_CELLS_640 = (8400, 8500)  # сетки 80²+40²+20² и с P6-головой ещё 10²
FAMILIES: dict[str, Family] = {
    "yolov5": Family("yolov5", "YOLOv5", "yolov5n.onnx", 0.35, "AGPL-3.0 (Ultralytics)", False,
                     lambda path, conf: person_detector.PersonDetector(path, conf),
                     lambda shape: shape[0] in tuple(3 * n for n in _CELLS_640) and shape[1] >= 6),
    "yolox": Family("yolox", "YOLOX", "yolox_tiny.onnx", 0.30, "Apache-2.0 (Megvii)", True,
                    lambda path, conf: person_detector_yolox.YoloxPersonDetector(path, conf),
                    lambda shape: shape[0] == len(person_detector_yolox.GRID) and shape[1] >= 6),
    "yolov8": Family("yolov8", "YOLOv8-style", "yolov8n.onnx", 0.30, "AGPL-3.0 (Ultralytics)", False,
                     lambda path, conf: person_detector_yolov8.Yolov8PersonDetector(path, conf),
                     lambda shape: shape[1] in _CELLS_640 and shape[0] >= 5),
}
DEFAULT_FAMILY = "yolox"  # вердикт бенча T-20261003-02: recall 0.951 против 0.877, ложных 1 против 4
LEGACY_FAMILY = "yolov5"
LEGACY_MODEL = pathlib.Path("/usr/share/cctv/models/yolov5n.onnx")  # файл образов до мультимодели
MODEL_DIRS = "/etc/cctv/models:/usr/share/cctv/models"


@dataclasses.dataclass(frozen=True)
class DetectorSettings:
    family: str
    model: pathlib.Path
    confidence: float
    reason: str  # откуда взят выбор — для лога запуска


def _env(env: Mapping[str, str], name: str) -> str:
    # compose передаёт незаданную переменную пустой строкой — это «не задано».
    return (env.get(name) or "").strip()


def model_dirs(env: Mapping[str, str] = os.environ) -> list[pathlib.Path]:
    return [pathlib.Path(p) for p in (_env(env, "CCTV_MODEL_DIRS") or MODEL_DIRS).split(":") if p]


def locate(name: str, dirs: list[pathlib.Path]) -> pathlib.Path:
    """Файл весов: абсолютный путь как есть, имя — первое найденное в каталогах."""
    path = pathlib.Path(name)
    if path.is_absolute():
        return path
    for directory in dirs:
        if (directory / path).is_file():
            return directory / path
    return (dirs[0] if dirs else pathlib.Path(".")) / path  # не найден: детектор скажет person_model_missing


def available_models(env: Mapping[str, str] = os.environ) -> list[pathlib.Path]:
    """Все *.onnx в каталогах моделей (первый каталог перекрывает одноимённые)."""
    seen: dict[str, pathlib.Path] = {}
    for directory in model_dirs(env):
        if directory.is_dir():
            for path in sorted(directory.glob("*.onnx")):
                seen.setdefault(path.name, path)
    return list(seen.values())


def resolve(env: Mapping[str, str] = os.environ, legacy_model: pathlib.Path = LEGACY_MODEL) -> DetectorSettings:
    family_name = _env(env, "CCTV_PERSON_MODEL_FAMILY").lower()
    model_name = _env(env, "CCTV_PERSON_MODEL")
    if family_name:
        if family_name not in FAMILIES:
            raise ValueError("person_model_family_unknown")
        reason = "setting"
    elif model_name:
        family_name, reason = LEGACY_FAMILY, "legacy_model_setting"  # переменная была только у YOLOv5n
    elif legacy_model.is_file():
        family_name, model_name, reason = LEGACY_FAMILY, str(legacy_model), "legacy_model_file"
    else:
        family_name, reason = DEFAULT_FAMILY, "default"
    family = FAMILIES[family_name]
    if reason == "legacy_model_setting":
        model = pathlib.Path(model_name)  # побайтно как PersonDetector до мультимодели
    else:
        model = locate(model_name or family.default_file, model_dirs(env))
    raw_confidence = _env(env, "CCTV_PERSON_CONFIDENCE")
    confidence = float(raw_confidence) if raw_confidence else family.confidence
    return DetectorSettings(family_name, model, confidence, reason)


def build_person_detector(settings: DetectorSettings | None = None, probe: bool = False):
    """Детектор по настройке; нет файла или ONNX не читается — исключение.

    ``probe`` прогоняет пустой кадр: файл чужого семейства (другая форма выхода)
    падает сразу, а не на первом кадре камеры — это нужно смене модели на ходу,
    чтобы откатиться до того, как старый детектор отпущен.
    """
    settings = settings or resolve()
    detector = FAMILIES[settings.family].factory(settings.model, settings.confidence)
    if probe:
        detector.detect(np.zeros((360, 640, 3), dtype=np.uint8))
        output = detector.net.forward()  # вход тот же, что у detect выше
        rows = output[0] if output.ndim == 3 else output
        if rows.ndim != 2 or not FAMILIES[settings.family].layout(tuple(rows.shape)):
            raise RuntimeError("person_model_family_mismatch")
    return detector
