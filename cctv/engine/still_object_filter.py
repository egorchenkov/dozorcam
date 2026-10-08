"""Фильтр неподвижных объектов для детектора людей (все камеры с YOLO на сервере).

Решение владельца 26.09.2026: посторонние предметы в кадре будут всегда — кто-то
поставит, кто-то уберёт, — и они не должны становиться причиной ложного
«человека». Порогом уверенности это не лечится: мешки и ведро в углу
Дача-3 давали 0.35–0.52, ночной предмет — 0.53, тогда как настоящие
проходы городской камеры идут на 0.37–0.58. Разница между предметом и человеком не в
уверенности сети, а в движении: человек, которого сеть увидела, несколько
секунд назад в этом месте кадра отсутствовал, предмет — стоял.

Поэтому каждое срабатывание YOLO проверяется по своей рамке: сравниваем область
рамки на текущем кадре с тем же местом на кадре ``ref_age``–``window`` секунд
назад. Человек — доля изменившихся пикселей внутри рамки велика И заметно выше,
чем снаружи (иначе это смена освещения, облако, ИК-шум — они меняют весь кадр
равномерно). Замер 26.09.2026 на буферах четырёх камер: у верхней рамки
покоящейся сцены изменений внутри 0.00 % (p95), у вставленной фигуры человека —
64–83 % при 0.6–1.6 % снаружи.

Модуль не знает ни камер, ни Telegram: только серые кадры, время съёмки и рамка.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import os
import pathlib

import cv2
import numpy as np

# Глубина памяти кадров и минимальный возраст опорного кадра. Опорный — самый
# старый в окне, но не моложе ref_age: сравнение с соседним кадром (0.5 с при
# 2 кадрах/с) пропустило бы медленно идущего человека.
STILL_WINDOW_SEC = float(os.environ.get("CCTV_PERSON_STILL_WINDOW_SEC", "20"))
STILL_REF_AGE_SEC = float(os.environ.get("CCTV_PERSON_STILL_REF_AGE_SEC", "3"))
# Минимальная доля изменившихся пикселей внутри рамки (в процентах) и во
# сколько раз она обязана превышать долю снаружи. Замер: человек 64–83 %
# против 0.6–1.6 % снаружи; предмет — 0 %. Запас в обе стороны кратный.
STILL_INSIDE_MIN = float(os.environ.get("CCTV_PERSON_STILL_INSIDE_MIN", "10"))
STILL_RATIO = float(os.environ.get("CCTV_PERSON_STILL_RATIO", "3"))
# Уверенность, при которой неподвижная рамка всё же считается человеком —
# страховка от стоящего без движения дольше окна. Предметы доходили до 0.53;
# 1.0 отключает обход совсем. 0.70 (до 0.1.2) резал людей в сумерках: 06.10.2026
# на городской камере отвергнуты 0.55–0.69 при живом проходе. Предмет, который
# уже был признан неподвижным на этом месте кадра, обход не получает (ниже).
STILL_BYPASS_CONFIDENCE = float(os.environ.get("CCTV_PERSON_STILL_BYPASS", "0.55"))
# Вне рамки изменилось больше этой доли (в процентах) — менялся весь кадр:
# сумерки, переключение ИК, автоэкспозиция. Эталон 3–20-секундной давности
# тогда недостоверен, и «inside ≥ ratio × outside» отвергает и человека
# (06.10.2026: люди при outside 15–96 %, тогда как у предметов outside 0–0.3 %).
# Такой кадр — «не знаю», он идёт дальше, а не отвергается. 100 отключает.
STILL_UNRELIABLE_OUTSIDE = float(os.environ.get("CCTV_PERSON_STILL_UNRELIABLE_OUTSIDE", "15"))
# Память «здесь стоит предмет»: рамка, внутри которой за окно не изменилось
# почти ничего, запоминается на сутки. Совпавшая с ней рамка (IoU) не получает
# ни обхода по уверенности, ни пропуска по недостоверному эталону: на большом
# поле предмет 0.036×0.078 кадра держал 0.36–0.60 на рассвете (05–06.10.2026),
# и обход 0.55 без памяти превратил бы его в серию ложных «людей».
STILL_STATIC_MEMORY_SEC = float(os.environ.get("CCTV_PERSON_STILL_STATIC_MEMORY_SEC", "86400"))
STILL_STATIC_IOU = float(os.environ.get("CCTV_PERSON_STILL_STATIC_IOU", "0.5"))
# Ширина уменьшенной копии кадра: 20 с истории при 2 кадрах/с — 40 копий,
# 320×180 — ~2 МБ на камеру; усреднение при уменьшении заодно гасит шум.
STILL_WIDTH = int(os.environ.get("CCTV_PERSON_STILL_WIDTH", "320"))
PIXEL_DELTA = 25  # тот же порог «пиксель изменился», что у motion_score


@dataclasses.dataclass
class StillVerdict:
    moving: bool
    # moving | still | no_reference | confidence_bypass | unreliable_reference | known_static
    reason: str
    inside: float = 0.0    # % изменившихся пикселей внутри рамки
    outside: float = 0.0   # % изменившихся пикселей вне рамки
    ref_age: float = 0.0   # возраст опорного кадра, с

    def note(self) -> str:
        return (f"inside={self.inside:.1f}% outside={self.outside:.2f}% "
                f"ref_age={self.ref_age:.0f}s still={self.reason}")


class StillObjectFilter:
    """Память уменьшенных серых кадров одной камеры и вердикт по рамке."""

    def __init__(self, window_sec: float = STILL_WINDOW_SEC, ref_age_sec: float = STILL_REF_AGE_SEC,
                 inside_min: float = STILL_INSIDE_MIN, ratio: float = STILL_RATIO,
                 bypass: float = STILL_BYPASS_CONFIDENCE, width: int = STILL_WIDTH,
                 unreliable_outside: float = STILL_UNRELIABLE_OUTSIDE,
                 static_memory_sec: float = STILL_STATIC_MEMORY_SEC,
                 static_iou: float = STILL_STATIC_IOU, memory_path: pathlib.Path | None = None) -> None:
        self.window_sec, self.ref_age_sec = window_sec, ref_age_sec
        self.inside_min, self.ratio, self.bypass, self.width = inside_min, ratio, bypass, width
        self.unreliable_outside = unreliable_outside
        self.static_memory_sec, self.static_iou = static_memory_sec, static_iou
        self.history: collections.deque = collections.deque()  # (at, small_gray)
        self.static_boxes: collections.deque = collections.deque(maxlen=64)  # (at, box)
        self.memory_path, self._saved_at = memory_path, 0.0
        if memory_path is not None:
            self._load_static()

    def shrink(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if getattr(frame, "ndim", 2) == 3 else frame
        h, w = gray.shape[:2]
        if w <= self.width:
            return gray
        return cv2.resize(gray, (self.width, max(1, round(h * self.width / w))), interpolation=cv2.INTER_AREA)

    def remember(self, at: float, frame) -> None:
        """Запомнить кадр; вызывается на КАЖДЫЙ кадр, в том числе пропущенный гейтом,
        иначе после открытия гейта не с чем было бы сравнивать."""
        self.history.append((at, self.shrink(frame)))
        while self.history and at - self.history[0][0] > self.window_sec:
            self.history.popleft()

    def judge(self, at: float, box: tuple[float, float, float, float], confidence: float) -> StillVerdict:
        """Двигается ли то, что YOLO обвёл рамкой ``box`` (x, y, w, h в долях кадра)
        на последнем запомненном кадре."""
        if not self.history:
            return self._bypass_or(StillVerdict(False, "no_reference"), confidence)
        current = self.history[-1][1]
        reference = None
        for past_at, past in self.history:
            if at - past_at >= self.ref_age_sec and past.shape == current.shape:
                reference = (past_at, past)
                break
        if reference is None:
            return self._bypass_or(StillVerdict(False, "no_reference"), confidence)
        changed = cv2.absdiff(current, reference[1]) > PIXEL_DELTA
        height, width = changed.shape
        x0 = min(width - 1, max(0, int(box[0] * width)))
        y0 = min(height - 1, max(0, int(box[1] * height)))
        x1 = min(width, max(x0 + 1, int((box[0] + box[2]) * width)))
        y1 = min(height, max(y0 + 1, int((box[1] + box[3]) * height)))
        region = changed[y0:y1, x0:x1]
        inside_sum, inside_n = int(region.sum()), int(region.size)
        total_sum, total_n = int(changed.sum()), int(changed.size)
        inside = inside_sum / inside_n * 100
        outside = (total_sum - inside_sum) / max(1, total_n - inside_n) * 100
        moving = inside >= self.inside_min and inside >= self.ratio * outside
        verdict = StillVerdict(moving, "moving" if moving else "still", inside, outside, at - reference[0])
        if moving:
            return verdict
        known = self._known_static(at, box)
        if inside < self.inside_min and outside < self.unreliable_outside:
            # Внутри рамки за окно не изменилось почти ничего при спокойном кадре —
            # это предмет (или замерший человек); место запоминается.
            self._remember_static(at, box, known)
            return verdict if known else self._bypass_or(verdict, confidence)
        if known:
            return dataclasses.replace(verdict, reason="known_static")
        if outside >= self.unreliable_outside:
            return dataclasses.replace(verdict, moving=True, reason="unreliable_reference")
        return self._bypass_or(verdict, confidence)

    def _known_static(self, at: float, box) -> bool:
        """Совпадает ли рамка с местом, где раньше стоял неподвижный предмет."""
        while self.static_boxes and at - self.static_boxes[0][0] > self.static_memory_sec:
            self.static_boxes.popleft()
        return any(box_iou(box, past) >= self.static_iou for _, past in self.static_boxes)

    def _remember_static(self, at: float, box, known: bool) -> None:
        # Одно место — одна запись со свежим временем: предмет судится на каждом
        # кадре, и без замены он вытеснил бы из памяти все остальные места.
        kept = [(t, b) for t, b in self.static_boxes if box_iou(box, b) < self.static_iou]
        self.static_boxes.clear()
        self.static_boxes.extend(kept)
        self.static_boxes.append((at, tuple(float(v) for v in box)))
        # Память переживает перезапуск движка: иначе первый рассвет после
        # выката снова дал бы предмету обход по уверенности. Пишем редко —
        # новое место или раз в 10 минут.
        if self.memory_path is not None and (not known or at - self._saved_at >= 600):
            self._saved_at = at
            try:
                self.memory_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.memory_path.with_suffix(".tmp")
                temp.write_text(json.dumps([[t, list(b)] for t, b in self.static_boxes]))
                temp.replace(self.memory_path)
            except OSError:
                pass  # память — улучшение, а не условие работы

    def _load_static(self) -> None:
        try:
            for at, box in json.loads(self.memory_path.read_text()):
                self.static_boxes.append((float(at), tuple(float(v) for v in box)))
        except (OSError, ValueError, TypeError):
            pass

    def _bypass_or(self, verdict: StillVerdict, confidence: float) -> StillVerdict:
        if confidence >= self.bypass:
            return dataclasses.replace(verdict, moving=True, reason="confidence_bypass")
        return verdict


def box_iou(a, b) -> float:
    """IoU двух рамок (x, y, w, h) в долях кадра."""
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0
