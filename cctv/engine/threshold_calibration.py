"""Автокалибровка порога детектора людей по паре «модель × камера».

Оценки разных моделей несравнимы, а шум одной и той же модели на разных сценах
отличается в разы (бенч 03.10.2026: лучшие пороги по камерам 0.30–0.70 при общем
0.30). Поэтому порог считается по камере и под активную модель — тем же
принципом, что плавающий пол гейта (``gate_threshold_for``): квантиль шума сцены
плюс запас.

* **Шум** — максимальная уверенность «человека» на кадрах, где человека нет: кадр
  прошёл в YOLO, не входит в серию из ``PERSON_HITS`` срабатываний подряд (так
  движок определяет человека) и не лежит ближе ``GUARD_SEC`` по времени съёмки
  к такой серии — подступы человека к порогу тоже не шум, — а камера (ONVIF
  FieldDetector, если есть) человека не видела. Одиночный всплеск выше порога —
  шум: именно два таких подряд и дают ложное событие. Предметы, подавленные фильтром неподвижных объектов, в шум не идут:
  события они не дают при любом пороге, а поднять порог над мешками в углу
  значило бы ослепить камеру для настоящих проходов (0.37–0.58 у городской камеры).
* **Порог** = ``max(стартовый, q99(шум) + MARGIN)``: ниже стартового порога
  модели калибровка по одному шуму не опускает — полноту (recall) без размеченных
  людей не измерить, и тихая сцена не повод ловить всё подряд.
* **Подтверждённые проходы** — событие YOLO (``PERSON_HITS`` кадров подряд,
  оценка прохода — слабейший из них; с пометкой, видела ли его камера) и эпизод,
  когда человека видела только камера (оценка — пик YOLO за эпизод). Проход,
  чья оценка не выше шума, порогом не спасти — он не учитывается. Если таких
  проходов набралось ``PASS_MIN``, порог не поднимается выше их нижнего
  квантиля (но и не опускается ниже шума + ``MIN_MARGIN``) — так проходы
  ограничивают порог сверху и позволяют опустить его ниже стартового.

Когда считается: при смене модели (хук ``ModelManager.on_switch``) и по команде
«Откалибровать» из бота. Пока шум не набран (``CALIBRATION_FRAMES`` кадров),
работает прежний порог: после смены модели — стартовый порог семейства. Без
команды калибровка не запускается: установка без выбора в боте работает с
порогом из конфига ровно как раньше.

Файлы в каталоге состояния движка (как у model_switch — мост и конвейер это
разные процессы, конфиг-каталог смонтирован только на чтение):

* ``person_threshold.json`` — пишет конвейер: по камере модель, состояние
  калибровки, результат и подтверждённые проходы;
* ``person_threshold.override.json`` — пишет мост: ручной порог камеры для
  конкретной модели; сильнее автокалибровки и переживает перезапуск;
* ``person_threshold.request.json`` — заявка моста «Откалибровать».
"""
from __future__ import annotations

import os
import pathlib
import threading
import time
import uuid
from collections.abc import Callable, Iterable

from .model_switch import _read, _write, label

STATE_NAME = "person_threshold.json"
OVERRIDE_NAME = "person_threshold.override.json"
REQUEST_NAME = "person_threshold.request.json"

CALIBRATION_FRAMES = int(os.environ.get("CCTV_PERSON_CALIBRATION_FRAMES", "600"))
NOISE_QUANTILE = float(os.environ.get("CCTV_PERSON_CALIBRATION_NOISE_QUANTILE", "0.99"))
MARGIN = float(os.environ.get("CCTV_PERSON_CALIBRATION_MARGIN", "0.10"))
MIN_MARGIN = float(os.environ.get("CCTV_PERSON_CALIBRATION_MIN_MARGIN", "0.05"))
PASS_MIN = int(os.environ.get("CCTV_PERSON_CALIBRATION_PASS_MIN", "3"))
PASS_QUANTILE = float(os.environ.get("CCTV_PERSON_CALIBRATION_PASS_QUANTILE", "0.2"))
PASSES_KEPT = 50
PERSON_HITS = int(os.environ.get("CCTV_PERSON_HITS", "2"))  # то же, что у конвейера
GUARD_SEC = float(os.environ.get("CCTV_PERSON_CALIBRATION_GUARD_SEC", "10"))
THRESHOLD_MIN, THRESHOLD_MAX = 0.05, 0.95
POLL_SEC = float(os.environ.get("CCTV_THRESHOLD_POLL_SEC", "2"))


def quantile(values: Iterable[float], q: float) -> float:
    ranked = sorted(values)
    return ranked[min(len(ranked) - 1, int(len(ranked) * q))]


def clamp(value: float) -> float:
    return round(min(THRESHOLD_MAX, max(THRESHOLD_MIN, value)), 2)


def compute_threshold(noise: list[float], passes: list[float], start: float) -> dict:
    """Порог по шуму сцены и подтверждённым проходам (чистая функция — для тестов)."""
    level = quantile(noise, NOISE_QUANTILE)
    value, reason = start, "start_above_noise"
    if level + MARGIN > start:
        value, reason = level + MARGIN, "noise"
    useful = [score for score in passes if score > level + MIN_MARGIN]
    if len(useful) >= PASS_MIN:
        cap = quantile(useful, PASS_QUANTILE)
        if cap < value:
            value, reason = max(cap, level + MIN_MARGIN), "passes"
    return {"value": clamp(value), "reason": reason, "noise_level": round(level, 3),
            "noise_n": len(noise), "passes_n": len(passes), "passes_used": len(useful)}


def effective(start: float, entry: dict | None, override: dict | None, model: str) -> tuple[float, str]:
    """Итоговый порог камеры: ручной > автокалибровка > стартовый/из конфига.
    Чужая модель в записи — запись не действует (оценки несравнимы)."""
    if override and override.get("model") == model and isinstance(override.get("value"), (int, float)):
        return float(override["value"]), "manual"
    result = (entry or {}).get("result")
    if entry and entry.get("model") == model and isinstance(result, dict) and "value" in result:
        return float(result["value"]), "auto"
    return start, "start"


# --- сторона конвейера -----------------------------------------------------

class CameraCalibration:
    """Наблюдатель одной камеры: копит шум и проходы, по готовности ставит порог."""

    def __init__(self, manager: "ThresholdManager", camera_id: str, holder) -> None:
        self.manager, self.camera_id, self.holder = manager, camera_id, holder
        self.lock = threading.Lock()
        self.collecting = False
        self.noise: list[float] = []
        self.pending: list[tuple[float, float]] = []  # кадры, которые ещё может «съесть» соседнее срабатывание
        self.guard_until = 0.0
        self.run, self.run_start = 0, 0.0  # текущая серия срабатываний подряд
        self.episode_peak: float | None = None  # пик YOLO, пока человека видит камера
        self.episode_event = False

    def begin(self) -> None:
        with self.lock:
            self.collecting, self.noise, self.pending = True, [], []

    def observe(self, frame_at: float, score: float, found: bool, still: bool = False,
                camera_human: bool | None = None) -> None:
        """Каждый кадр, прошедший YOLO. ``found`` — уже после фильтра неподвижных."""
        try:
            self._observe(frame_at, float(score), found, still, camera_human)
        except Exception as exc:  # калибровка не имеет права ронять детекцию
            self.manager.log(f"person_threshold_error camera={self.camera_id} error={type(exc).__name__}")

    def _observe(self, frame_at, score, found, still, camera_human) -> None:
        onvif_pass = None
        finished = None
        with self.lock:
            if camera_human:
                # Неподвижный предмет пиком эпизода не считается — это не человек.
                peak = self.episode_peak or 0.0
                self.episode_peak = peak if still else max(peak, score)
            elif self.episode_peak is not None:
                if not self.episode_event:
                    onvif_pass = self.episode_peak
                self.episode_peak, self.episode_event = None, False
            if found:
                self.run += 1
                if self.run == 1:
                    self.run_start = frame_at
            else:
                self.run = 0
            if self.run >= PERSON_HITS:
                # Серия — человек: она сама и подступы к порогу (кадры до и после) — не шум.
                self.pending = [item for item in self.pending if item[0] < self.run_start - GUARD_SEC]
                self.guard_until = frame_at + GUARD_SEC
            elif not still and not camera_human and frame_at >= self.guard_until:
                self.pending.append((frame_at, score))
            ripe = [s for at, s in self.pending if at < frame_at - GUARD_SEC]
            self.pending = [item for item in self.pending if item[0] >= frame_at - GUARD_SEC]
            if self.collecting:
                self.noise.extend(ripe)
                if len(self.noise) >= CALIBRATION_FRAMES:
                    finished, self.noise, self.collecting = self.noise, [], False
        if onvif_pass is not None and onvif_pass > 0:
            self.manager.add_pass(self.camera_id, self.holder, onvif_pass, "onvif")
        if finished is not None:
            self.manager.finish(self.camera_id, self.holder, finished)
        elif self.collecting:
            self.manager.progress(self.camera_id, self.holder, len(self.noise))

    def event(self, score: float, onvif: bool) -> None:
        """Событие YOLO (PERSON_HITS подряд): ``score`` — слабейший из кадров события."""
        try:
            with self.lock:
                if self.episode_peak is not None:
                    self.episode_event = True
            self.manager.add_pass(self.camera_id, self.holder, float(score), "yolo+onvif" if onvif else "yolo")
        except Exception as exc:
            self.manager.log(f"person_threshold_error camera={self.camera_id} error={type(exc).__name__}")


class ThresholdManager:
    """Один на процесс конвейера, рядом с ModelManager."""

    def __init__(self, state_dir: pathlib.Path, models=None,
                 log: Callable[[str], None] = lambda line: print(line, flush=True)) -> None:
        self.state_dir, self.log = state_dir, log
        self.cameras: dict[str, CameraCalibration] = {}
        self.lock = threading.RLock()
        self._override_seen: dict | None = None
        self._progress_at: dict[str, float] = {}
        if models is not None:
            models.on_switch(self.on_model_switch)

    # Запись состояния — под общим замком: камеры пишут из своих потоков.
    def _state(self) -> dict:
        state = _read(self.state_dir / STATE_NAME)
        if not isinstance(state.get("cameras"), dict):
            state["cameras"] = {}
        return state

    def _save(self, state: dict) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            _write(self.state_dir / STATE_NAME, state)
        except OSError as exc:
            self.log(f"person_threshold_write_failed error={type(exc).__name__}")

    @staticmethod
    def _start(holder) -> float:
        return float(holder.settings.confidence)

    def _entry(self, state: dict, camera_id: str, holder) -> dict:
        """Запись камеры под активную модель; запись чужой модели сбрасывается."""
        model = label(holder.settings)
        entry = state["cameras"].get(camera_id)
        if not isinstance(entry, dict) or entry.get("model") != model:
            entry = {"model": model, "state": "idle", "passes": []}
            state["cameras"][camera_id] = entry
        entry["start"] = round(self._start(holder), 2)
        return entry

    def camera(self, camera_id: str, holder) -> CameraCalibration:
        with self.lock:
            calibration = self.cameras[camera_id] = CameraCalibration(self, camera_id, holder)
            state = self._state()
            entry = self._entry(state, camera_id, holder)
            if entry.get("state") == "collecting":  # перезапуск посреди калибровки — начать заново
                entry["collected"] = 0
                calibration.begin()
            self._save(state)
            self.apply(camera_id, state)
        return calibration

    def apply(self, camera_id: str, state: dict | None = None) -> None:
        calibration = self.cameras.get(camera_id)
        if calibration is None:
            return
        holder = calibration.holder
        state = state if state is not None else self._state()
        overrides = _read(self.state_dir / OVERRIDE_NAME)
        model = label(holder.settings)
        value, source = effective(self._start(holder), state["cameras"].get(camera_id),
                                  overrides.get(camera_id), model)
        threshold = None if source == "start" else value
        if threshold != holder.threshold:
            holder.threshold = threshold
            self.log(f"person_threshold camera={camera_id} model={model} value={value:.2f} source={source}")

    def begin(self, camera_ids: Iterable[str], reason: str) -> None:
        with self.lock:
            state = self._state()
            for camera_id in camera_ids:
                calibration = self.cameras.get(camera_id)
                if calibration is None:
                    continue
                entry = self._entry(state, camera_id, calibration.holder)
                entry.update(state="collecting", collected=0, needed=CALIBRATION_FRAMES, reason=reason,
                             started_at=time.time())
                calibration.begin()
                self.log(f"person_threshold_calibration camera={camera_id} model={entry['model']} "
                         f"reason={reason} frames={CALIBRATION_FRAMES}")
            self._save(state)

    def progress(self, camera_id: str, holder, collected: int) -> None:
        # Счётчик в файле — для меню бота; чаще раза в 10 с писать незачем.
        if time.time() - self._progress_at.get(camera_id, 0.0) < 10:
            return
        self._progress_at[camera_id] = time.time()
        with self.lock:
            state = self._state()
            entry = self._entry(state, camera_id, holder)
            if entry.get("state") == "collecting":
                entry["collected"] = collected
                self._save(state)

    def finish(self, camera_id: str, holder, noise: list[float]) -> None:
        with self.lock:
            state = self._state()
            entry = self._entry(state, camera_id, holder)
            passes = [float(p["score"]) for p in entry.get("passes") or [] if isinstance(p, dict)]
            result = compute_threshold(noise, passes, self._start(holder))
            entry.update(state="calibrated", collected=len(noise), result=result, calibrated_at=time.time())
            self._save(state)
            self.log(f"person_threshold_calibrated camera={camera_id} model={entry['model']} "
                     f"value={result['value']:.2f} reason={result['reason']} "
                     f"noise_q={result['noise_level']:.3f} noise_n={result['noise_n']} "
                     f"passes={result['passes_used']}/{result['passes_n']}")
            self.apply(camera_id, state)

    def add_pass(self, camera_id: str, holder, score: float, source: str) -> None:
        with self.lock:
            state = self._state()
            entry = self._entry(state, camera_id, holder)
            passes = [p for p in entry.get("passes") or [] if isinstance(p, dict)]
            passes.append({"score": round(score, 3), "source": source, "at": round(time.time())})
            entry["passes"] = passes[-PASSES_KEPT:]
            self._save(state)

    def on_model_switch(self, previous, target, camera_ids: list[str]) -> None:
        """Хук смены модели: SwitchableDetector.swap уже вернул стартовый порог;
        ручной порог прежней модели к новой не относится — калибруем заново."""
        with self.lock:
            for camera_id in camera_ids:
                self.apply(camera_id)
            self.begin(camera_ids, "model_switch")

    def poll(self) -> str | None:
        """Заявка «Откалибровать» и правка ручных порогов из моста."""
        outcome = None
        request = _read(self.state_dir / REQUEST_NAME)
        with self.lock:
            state = self._state()
            request_id = request.get("request_id")
            if request_id and request_id != state.get("request_id"):
                wanted = request.get("cameras")
                targets = [c for c in self.cameras if not wanted or c in wanted]
                state["request_id"] = request_id
                self._save(state)
                self.begin(targets, "request")
                outcome = "calibrating"
            overrides = _read(self.state_dir / OVERRIDE_NAME)
            if overrides != self._override_seen:
                self._override_seen = overrides
                for camera_id in self.cameras:
                    self.apply(camera_id)
                outcome = outcome or "overrides"
        return outcome

    def run(self) -> None:
        while True:
            try:
                self.poll()
            except Exception as exc:
                self.log(f"person_threshold_poll_error error={type(exc).__name__}")
            time.sleep(POLL_SEC)

    def start(self) -> None:
        threading.Thread(target=self.run, name="person-threshold", daemon=True).start()


# --- сторона моста ---------------------------------------------------------

def overview(state_dir: pathlib.Path, cameras: list[tuple[str, str]], active: dict | None) -> dict:
    """Пороги по камерам для бота. ``active`` — активная модель из model_switch.catalog."""
    state = _read(state_dir / STATE_NAME)
    entries = state.get("cameras") if isinstance(state.get("cameras"), dict) else {}
    overrides = _read(state_dir / OVERRIDE_NAME)
    request = _read(state_dir / REQUEST_NAME)
    active = active if isinstance(active, dict) and active.get("family") else None
    model = f"{active['family']}/{active.get('model')}" if active else None
    start = float(active.get("confidence") or 0) if active else 0.0
    rows = []
    for camera_id, title in cameras:
        entry = entries.get(camera_id) if isinstance(entries.get(camera_id), dict) else None
        if entry is not None and entry.get("model") != model:
            entry = None
        override = overrides.get(camera_id) if isinstance(overrides.get(camera_id), dict) else None
        value, source = effective(start, entry, override, model or "")
        result = (entry or {}).get("result") if isinstance((entry or {}).get("result"), dict) else None
        rows.append({
            "camera_id": camera_id, "title": title, "threshold": round(value, 2), "source": source,
            "start": round(start, 2), "auto": result,
            "manual": float(override["value"]) if source == "manual" else None,
            "state": (entry or {}).get("state") or "idle",
            "collected": int((entry or {}).get("collected") or 0),
            "needed": int((entry or {}).get("needed") or CALIBRATION_FRAMES),
            "passes": len((entry or {}).get("passes") or []),
        })
    pending = bool(request.get("request_id")) and request.get("request_id") != state.get("request_id")
    return {"model": model, "cameras": rows, "pending": pending,
            "min": THRESHOLD_MIN, "max": THRESHOLD_MAX}


def set_override(state_dir: pathlib.Path, camera_id: str, value: float | None, active: dict | None) -> dict:
    """Ручной порог камеры под активную модель; ``None`` — снять (вернуть авто)."""
    if not isinstance(active, dict) or not active.get("family"):
        raise ValueError("person_model_unavailable")
    if value is not None and not THRESHOLD_MIN <= value <= THRESHOLD_MAX:
        raise ValueError("threshold_out_of_range")
    overrides = _read(state_dir / OVERRIDE_NAME)
    if value is None:
        overrides.pop(camera_id, None)
    else:
        overrides[camera_id] = {"model": f"{active['family']}/{active.get('model')}",
                                "value": round(float(value), 2), "at": round(time.time())}
    state_dir.mkdir(parents=True, exist_ok=True)
    _write(state_dir / OVERRIDE_NAME, overrides)
    return {"camera_id": camera_id, "value": overrides.get(camera_id, {}).get("value")}


def request_calibration(state_dir: pathlib.Path, cameras: list[str] | None = None) -> dict:
    request = {"request_id": uuid.uuid4().hex[:12], "cameras": cameras or [], "at": time.time()}
    state_dir.mkdir(parents=True, exist_ok=True)
    _write(state_dir / REQUEST_NAME, request)
    return {"request_id": request["request_id"], "state": "pending", "frames": CALIBRATION_FRAMES}
