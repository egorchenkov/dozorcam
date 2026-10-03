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
import os

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
# 1.0 отключает обход совсем.
STILL_BYPASS_CONFIDENCE = float(os.environ.get("CCTV_PERSON_STILL_BYPASS", "0.70"))
# Ширина уменьшенной копии кадра: 20 с истории при 2 кадрах/с — 40 копий,
# 320×180 — ~2 МБ на камеру; усреднение при уменьшении заодно гасит шум.
STILL_WIDTH = int(os.environ.get("CCTV_PERSON_STILL_WIDTH", "320"))
PIXEL_DELTA = 25  # тот же порог «пиксель изменился», что у motion_score


@dataclasses.dataclass
class StillVerdict:
    moving: bool
    reason: str  # moving | still | no_reference | confidence_bypass
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
                 bypass: float = STILL_BYPASS_CONFIDENCE, width: int = STILL_WIDTH) -> None:
        self.window_sec, self.ref_age_sec = window_sec, ref_age_sec
        self.inside_min, self.ratio, self.bypass, self.width = inside_min, ratio, bypass, width
        self.history: collections.deque = collections.deque()  # (at, small_gray)

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
        return verdict if moving else self._bypass_or(verdict, confidence)

    def _bypass_or(self, verdict: StillVerdict, confidence: float) -> StillVerdict:
        if confidence >= self.bypass:
            return dataclasses.replace(verdict, moving=True, reason="confidence_bypass")
        return verdict
