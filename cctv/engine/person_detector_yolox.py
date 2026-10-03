"""YOLOX-Tiny (Apache-2.0) как замена YOLOv5n (AGPL) — адаптер с тем же интерфейсом.

Одно из трёх семейств ``person_models``: ``detect(frame) -> (found, score, box)`` и
``best/score/detects_person`` ведут себя так же, как у ``PersonDetector``, поэтому
конвейер и фильтр неподвижных объектов (рамка в долях кадра) не различают модели.
Модель по умолчанию для новых установок — по бенчу ``bench/``.

Модель — официальный экспорт Megvii ``yolox_tiny.onnx`` (релиз 0.1.1rc0): вход
416×416 BGR без нормировки (0..255), выход ``(1, 3549, 85)`` — сырые смещения по
сеткам страйдов 8/16/32, декодируются здесь, а не в графе.
"""
from __future__ import annotations

import os
import pathlib

import cv2
import numpy as np


MODEL_PATH = pathlib.Path(os.environ.get("CCTV_PERSON_YOLOX_MODEL", "/usr/share/cctv/models/yolox_tiny.onnx"))
INPUT_SIZE = 416  # граф экспорта фиксирован на 416×416
STRIDES = (8, 16, 32)
# Порог по бенчу 03.10.2026 (bench/REPORT.md): 0.30 — самый высокий общий порог без
# потери проходов на 4 камерах; фон городской камеры в ИК доходит до 0.35, его снимает
# фильтр неподвижных объектов. Порог на камеру точнее (0.30–0.70, см. отчёт).
PERSON_CONFIDENCE = float(os.environ.get("CCTV_PERSON_YOLOX_CONFIDENCE", "0.30"))
# Растяжение без сохранения пропорций — как у YOLOv5n-пути: на 16:9 кадре letterbox
# оставляет фигуре вдвое меньше строк сетки. Бенч подтвердил: letterbox поднимает
# ночной фон городской камеры до 0.57 и теряет людей за перилами на дачной.
STRETCH = os.environ.get("CCTV_PERSON_YOLOX_STRETCH", "1") != "0"


def _grids(size: int = INPUT_SIZE) -> tuple[np.ndarray, np.ndarray]:
    """Координаты ячеек и страйд для каждой из 3549 строк выхода (порядок как у YOLOX)."""
    cells, strides = [], []
    for stride in STRIDES:
        side = size // stride
        ys, xs = np.meshgrid(np.arange(side), np.arange(side), indexing="ij")
        cells.append(np.stack((xs, ys), axis=2).reshape(-1, 2))
        strides.append(np.full((side * side, 1), stride))
    return np.concatenate(cells).astype(np.float32), np.concatenate(strides).astype(np.float32)


GRID, GRID_STRIDE = _grids()


class YoloxPersonDetector:
    """CPU-only YOLOX-Tiny, один экземпляр на поток камеры."""

    def __init__(self, model_path: pathlib.Path = MODEL_PATH, confidence: float = PERSON_CONFIDENCE,
                 stretch: bool = STRETCH) -> None:
        if not model_path.is_file():
            raise RuntimeError("person_model_missing")
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.confidence = confidence
        self.stretch = stretch

    def _blob(self, frame: np.ndarray) -> tuple[np.ndarray, float, float]:
        """Вход сети и масштабы x/y «пиксель входа → доля кадра»."""
        height, width = frame.shape[:2]
        if self.stretch:
            image = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_LINEAR)
            return cv2.dnn.blobFromImage(image), 1 / INPUT_SIZE, 1 / INPUT_SIZE
        # Letterbox как при обучении YOLOX: пропорции сохранены, поле 114 справа/снизу.
        ratio = min(INPUT_SIZE / height, INPUT_SIZE / width)
        resized = cv2.resize(frame, (int(width * ratio), int(height * ratio)), interpolation=cv2.INTER_LINEAR)
        image = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
        image[:resized.shape[0], :resized.shape[1]] = resized
        return cv2.dnn.blobFromImage(image), 1 / (ratio * width), 1 / (ratio * height)

    def score(self, frame: np.ndarray) -> float:
        """Вернуть максимальную достоверность именно человека (COCO class 0)."""
        return self.best(frame)[0]

    def best(self, frame: np.ndarray) -> tuple[float, tuple[float, float, float, float]]:
        """Лучшая рамка человека: (достоверность, (x, y, w, h) в долях кадра)."""
        blob, sx, sy = self._blob(frame)
        self.net.setInput(blob)
        output = self.net.forward()
        rows = output[0] if output.ndim == 3 else output
        if rows.ndim != 2 or rows.shape[1] < 6 or rows.shape[0] != len(GRID):
            raise RuntimeError("person_model_output_invalid")
        # YOLOX: dx, dy, log(w), log(h), objectness, COCO class probabilities; person = 0.
        scores = rows[:, 4] * rows[:, 5]
        index = int(np.argmax(scores))
        stride = GRID_STRIDE[index, 0]
        cx = (rows[index, 0] + GRID[index, 0]) * stride
        cy = (rows[index, 1] + GRID[index, 1]) * stride
        w = float(np.exp(min(rows[index, 2], 10.0))) * stride
        h = float(np.exp(min(rows[index, 3], 10.0))) * stride
        return float(scores[index]), (float((cx - w / 2) * sx), float((cy - h / 2) * sy), float(w * sx), float(h * sy))

    def detect(self, frame: np.ndarray) -> tuple[bool, float, tuple[float, float, float, float]]:
        score, box = self.best(frame)
        return score >= self.confidence, score, box

    def detects_person(self, frame: np.ndarray) -> tuple[bool, float]:
        found, score, _ = self.detect(frame)
        return found, score
