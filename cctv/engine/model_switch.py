"""Смена модели детектора людей на ходу — из Telegram-бота, без перезапуска движка.

Бот ходит только в мост, а детекторы живут в соседнем процессе конвейера, поэтому
их связывают три файла в каталоге состояния движка (конфиг-каталог смонтирован
только на чтение, а его правка перезапускала бы всю цепочку вместе с очередью):

* ``person_model.request.json`` — заявка моста: ``{request_id, family, model}``;
* ``person_model.status.json`` — ответ конвейера: активная модель, исход последней
  заявки (``ok`` | ``rolled_back``) и код ошибки;
* ``person_model.json`` — выбор, сделанный в боте; переживает перезапуск и сильнее
  настройки ``[engine]``. Нет файла — модель берётся из конфига, как раньше
  (на проде без выбора в боте YOLOv5n не меняется).

Перезагрузка: менеджер собирает НОВЫЕ детекторы для всех камер и прогоняет на
каждом пробный кадр (``probe``), пока старые продолжают работать. Только когда
собрались все, детекторы подменяются под замком камеры — между двумя кадрами.
Курсор буфера при этом не трогается и процесс не перезапускается, так что кадры,
пришедшие до, во время и после смены, разбираются по порядку. Любая ошибка
сборки — откат: новые экземпляры выбрасываются, старые не отпускались вовсе.

Точка для автокалибровки при смене модели (T-20261003-05) — ``ModelManager.on_switch``.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import threading
import time
import uuid
from collections.abc import Callable, Mapping

from . import person_models
from .person_models import FAMILIES, DetectorSettings

CHOICE_NAME = "person_model.json"
REQUEST_NAME = "person_model.request.json"
STATUS_NAME = "person_model.status.json"
POLL_SEC = float(os.environ.get("CCTV_MODEL_SWITCH_POLL_SEC", "1"))
# Коды, которые бот переводит; всё прочее — общий «не загрузилась».
KNOWN_ERRORS = ("person_model_missing", "person_model_family_mismatch", "person_model_output_invalid",
                "person_model_family_unknown")
LOAD_FAILED = "person_model_load_failed"


def _read(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: pathlib.Path, value: dict) -> None:
    """Атомарно: соседний процесс не должен прочитать половину файла."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(path)


def describe(settings: DetectorSettings) -> dict:
    return {"family": settings.family, "model": settings.model.name,
            "confidence": round(settings.confidence, 2), "reason": settings.reason}


def label(settings: DetectorSettings | None) -> str:
    return f"{settings.family}/{settings.model.name}" if settings is not None else "none"


def error_code(exc: BaseException) -> str:
    code = str(exc)
    return code if code in KNOWN_ERRORS else LOAD_FAILED


def model_hint(name: str) -> str | None:
    """Семейство по имени файла — только для порядка в меню; проверяет загрузка."""
    lower = name.lower()
    if lower.startswith("yolox"):
        return "yolox"
    if lower.startswith("yolov5"):
        return "yolov5"
    if lower.startswith(("yolov8", "yolo11", "yolov11", "yolov9", "yolov10")):
        return "yolov8"
    return None


def choice_settings(choice: dict, env: Mapping[str, str] = os.environ) -> DetectorSettings:
    """Выбор из бота → настройка детектора. Порог — стартовый порог семейства:
    оценки разных семейств несравнимы (docs/models.md), поэтому
    ``CCTV_PERSON_CONFIDENCE`` из конфига к чужому семейству не переносится."""
    family = str(choice.get("family") or "")
    if family not in FAMILIES:
        raise ValueError("person_model_family_unknown")
    name = str(choice.get("model") or FAMILIES[family].default_file)
    model = person_models.locate(name, person_models.model_dirs(env))
    return DetectorSettings(family, model, FAMILIES[family].confidence, "bot")


# --- сторона моста ---------------------------------------------------------

def catalog(state_dir: pathlib.Path, env: Mapping[str, str] = os.environ) -> dict:
    """Что показать в меню: семейства, найденные файлы, активная модель, исход смены."""
    status = _read(state_dir / STATUS_NAME)
    request = _read(state_dir / REQUEST_NAME)
    active = status.get("active")
    if not isinstance(active, dict):
        # Конвейер ещё не отчитался (старт) — та же логика выбора, что у него.
        try:
            choice = _read(state_dir / CHOICE_NAME)
            active = describe(choice_settings(choice, env) if choice else person_models.resolve(env))
        except ValueError:
            active = None
    switch = {key: status.get(key) for key in ("request_id", "state", "error", "target", "previous", "at")
              if status.get(key) is not None}
    if request.get("request_id") and request.get("request_id") != status.get("request_id"):
        switch = {"request_id": request["request_id"], "state": "pending",
                  "target": {"family": request.get("family"), "model": request.get("model")}}
    return {
        "families": [{"family": f.name, "title": f.title, "default_file": f.default_file,
                      "confidence": f.confidence, "license": f.license, "bundled": f.bundled}
                     for f in FAMILIES.values()],
        "models": [{"model": path.name, "hint": model_hint(path.name)}
                   for path in person_models.available_models(env)],
        "active": active,
        "switch": switch,
    }


def request_switch(state_dir: pathlib.Path, family: str, model: str,
                   env: Mapping[str, str] = os.environ) -> dict:
    """Заявка на смену. Только имя файла из каталогов моделей — путь из чата не принимается."""
    if family not in FAMILIES:
        raise ValueError("person_model_family_unknown")
    names = {path.name for path in person_models.available_models(env)}
    if not model or "/" in model or "\\" in model or model not in names:
        raise LookupError("person_model_missing")
    request = {"request_id": uuid.uuid4().hex[:12], "family": family, "model": model,
               "at": time.time()}
    state_dir.mkdir(parents=True, exist_ok=True)
    _write(state_dir / REQUEST_NAME, request)
    return {"request_id": request["request_id"], "state": "pending"}


# --- сторона конвейера -----------------------------------------------------

class SwitchableDetector:
    """Детектор камеры, который можно подменить между кадрами.

    Интерфейс тот же, что у адаптеров семейств (``detect``, ``confidence``), поэтому
    конвейер и фильтр неподвижных объектов подмены не замечают. ``generation``
    растёт с каждой подменой — по нему поток камеры может узнать о смене модели.
    """

    def __init__(self, detector, settings: DetectorSettings) -> None:
        self._detector, self.settings, self.generation = detector, settings, 0
        # Порог камеры поверх порога модели (автокалибровка или ручной, модуль
        # threshold_calibration); None — порог модели, как до калибровки.
        self.threshold: float | None = None
        self._lock = threading.Lock()

    @property
    def confidence(self) -> float:
        return self.threshold if self.threshold is not None else self._detector.confidence

    def detect(self, frame):
        with self._lock:
            found, score, box = self._detector.detect(frame)
            threshold = self.threshold
        if threshold is not None:
            found = score >= threshold
        return found, score, box

    def swap(self, detector, settings: DetectorSettings):
        with self._lock:
            previous, self._detector, self.settings = self._detector, detector, settings
            # Порог прежней модели к новой не относится: до калибровки — стартовый.
            self.threshold = None
            self.generation += 1
        return previous


class ModelManager:
    """Один на процесс конвейера: держит активную модель и детекторы всех камер."""

    def __init__(self, state_dir: pathlib.Path, env: Mapping[str, str] = os.environ,
                 build: Callable[..., object] = person_models.build_person_detector,
                 log: Callable[[str], None] = lambda line: print(line, flush=True)) -> None:
        self.state_dir, self.env, self.build, self.log = state_dir, env, build, log
        self.holders: dict[str, SwitchableDetector] = {}
        self.hooks: list[Callable[[DetectorSettings | None, DetectorSettings, list[str]], None]] = []
        self.changed = threading.Condition()
        self.generation = 0
        self._switch_lock = threading.Lock()
        self.settings = self._startup_settings()

    # Активная модель при старте: выбор из бота, а если он не грузится — конфиг.
    # None — модель не задаётся вовсе (неизвестное семейство в конфиге): камеры
    # с YOLO ждут, пока исправный выбор не придёт из бота.
    def _startup_settings(self) -> DetectorSettings | None:
        status = _read(self.state_dir / STATUS_NAME)
        choice = _read(self.state_dir / CHOICE_NAME)
        try:
            configured = person_models.resolve(self.env)
        except ValueError as exc:
            configured = None
            self.log(f"person_model_config_invalid error={exc}")
        if choice:
            try:
                chosen = choice_settings(choice, self.env)
                self.build(chosen, probe=True)
                self._report(status, active=chosen)
                return chosen
            except Exception as exc:
                # Откат на запуске: выбранный в боте файл пропал или испорчен.
                self.log(f"person_model_rollback stage=start family={choice.get('family')} "
                         f"file={choice.get('model')} error={error_code(exc)}")
                (self.state_dir / CHOICE_NAME).unlink(missing_ok=True)
                status = {**status, "state": "rolled_back", "error": error_code(exc),
                          "target": {"family": choice.get("family"), "model": choice.get("model")}}
        self._report(status, active=configured)
        return configured

    def _report(self, status: dict, **fields) -> None:
        value = dict(status)
        for key, item in fields.items():
            value[key] = describe(item) if isinstance(item, DetectorSettings) else item
        value["at"] = time.time()
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _write(self.state_dir / STATUS_NAME, value)
        except OSError as exc:
            self.log(f"person_model_status_write_failed error={type(exc).__name__}")

    def on_switch(self, hook: Callable[[DetectorSettings | None, DetectorSettings, list[str]], None]) -> None:
        """Вызов после успешной смены: (прежняя, новая, камеры). Сюда T-20261003-05
        вешает автокалибровку порогов под новую модель. Ошибка хука смену не откатывает."""
        self.hooks.append(hook)

    def register(self, camera_id: str) -> SwitchableDetector:
        """Детектор камеры на активной модели (без пробы — как до смены моделей)."""
        settings = self.settings
        if settings is None:
            raise ValueError("person_model_family_unknown")
        holder = SwitchableDetector(self.build(settings), settings)
        with self._switch_lock:
            if holder.settings != self.settings:  # смена прошла, пока камера собиралась
                holder.swap(self.build(self.settings), self.settings)
            self.holders[camera_id] = holder
        return holder

    def wait_change(self, generation: int, timeout: float) -> int:
        with self.changed:
            if self.generation == generation:
                self.changed.wait(timeout)
            return self.generation

    def poll(self) -> str | None:
        """Обработать новую заявку, если есть. Возвращает исход (для тестов) или None."""
        request = _read(self.state_dir / REQUEST_NAME)
        status = _read(self.state_dir / STATUS_NAME)
        request_id = request.get("request_id")
        if not request_id or request_id == status.get("request_id"):
            return None
        return self.switch({"family": request.get("family"), "model": request.get("model")}, request_id)

    def switch(self, choice: dict, request_id: str | None = None) -> str:
        with self._switch_lock:
            previous = self.settings
            status = {"request_id": request_id, "target": {"family": choice.get("family"),
                                                           "model": choice.get("model")}}
            try:
                target = choice_settings(choice, self.env)
                # Новые экземпляры — на каждую камеру (сеть OpenCV не делится между
                # потоками) и с пробным кадром: файл чужого семейства падает здесь.
                fresh = {camera_id: self.build(target, probe=True) for camera_id in self.holders}
                if not fresh:
                    self.build(target, probe=True)  # камер с YOLO нет — всё равно проверить файл
            except Exception as exc:
                code = error_code(exc)
                self.log(f"person_model_rollback family={choice.get('family')} file={choice.get('model')} "
                         f"error={code} active={label(previous)}")
                self._report(status, state="rolled_back", error=code, active=previous, previous=previous)
                return "rolled_back"
            for camera_id, detector in fresh.items():
                self.holders[camera_id].swap(detector, target)
            self.settings = target
            try:
                _write(self.state_dir / CHOICE_NAME, {"family": target.family, "model": target.model.name,
                                                      "at": time.time()})
            except OSError as exc:  # модель уже работает; не переживёт лишь перезапуск
                self.log(f"person_model_choice_write_failed error={type(exc).__name__}")
            self._report(status, state="ok", active=target, previous=previous)
            self.log(f"person_model_switched family={target.family} file={target.model.name} "
                     f"confidence={target.confidence:.2f} previous={label(previous)} "
                     f"cameras={len(fresh)}")
        with self.changed:
            self.generation += 1
            self.changed.notify_all()
        for hook in list(self.hooks):
            try:
                hook(previous, target, sorted(fresh))
            except Exception as exc:
                self.log(f"person_model_hook_failed hook={getattr(hook, '__name__', hook)} "
                         f"error={type(exc).__name__}")
        return "ok"

    def run(self) -> None:
        while True:
            try:
                self.poll()
            except Exception as exc:  # поток смены не должен умирать от кривого файла
                self.log(f"person_model_switch_error error={type(exc).__name__}")
            time.sleep(POLL_SEC)

    def start(self) -> None:
        threading.Thread(target=self.run, name="person-model-switch", daemon=True).start()
