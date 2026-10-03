"""YOLOv8-стиль (anchor-free, без objectness) — адаптер с тем же интерфейсом.

Семейство: экспорт Ultralytics YOLOv8/YOLO11 и совместимые по выходу модели.
Вход 640×640 RGB 0..1, выход ``(1, 4 + классы, N)`` — для каждой из N точек
центр/размер рамки в пикселях входа (уже декодированы в графе) и вероятности
классов COCO без отдельной objectness; person = класс 0. Раскладка «каналы,
потом точки» — признак семейства: у YOLOv5/YOLOX наоборот ``(1, N, 5 + классы)``,
и файл чужого семейства здесь отвергается, а не читается мусором.

Веса Ultralytics — AGPL-3.0, в образ не входят: файл кладёт пользователь
(docs/models.md).
"""
from __future__ import annotations

import os
import pathlib

import cv2
import numpy as np


MODEL_PATH = pathlib.Path(os.environ.get("CCTV_PERSON_YOLOV8_MODEL", "/etc/cctv/models/yolov8n.onnx"))
INPUT_SIZE = 640  # стандартный экспорт Ultralytics; граф фиксирован на квадрат
# Стартовый порог до автокалибровки (замер 03.10.2026, bench/yolov8_start_threshold.py):
# на 0.30 YOLOv8n находит 77 проходов из 81 при 1 ложном событии, на 0.35 — 71 при
# том же 1 ложном: на камере с людьми за перилами медиана уверенности 0.32.
PERSON_CONFIDENCE = float(os.environ.get("CCTV_PERSON_YOLOV8_CONFIDENCE", "0.30"))


class Yolov8PersonDetector:
    """CPU-only YOLOv8-стиль, один экземпляр на поток камеры."""

    def __init__(self, model_path: pathlib.Path = MODEL_PATH, confidence: float = PERSON_CONFIDENCE,
                 input_size: int = INPUT_SIZE) -> None:
        if not model_path.is_file():
            raise RuntimeError("person_model_missing")
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.confidence = confidence
        self.input_size = input_size

    def score(self, frame: np.ndarray) -> float:
        """Вернуть максимальную достоверность именно человека (COCO class 0)."""
        return self.best(frame)[0]

    def best(self, frame: np.ndarray) -> tuple[float, tuple[float, float, float, float]]:
        """Лучшая рамка человека: (достоверность, (x, y, w, h) в долях кадра).

        Растяжение без letterbox — как у YOLOv5n и YOLOX: на 16:9 кадре фигуре
        остаётся вдвое больше строк сетки, а бенч letterbox не взял (bench/REPORT.md).
        """
        size = self.input_size
        blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, (size, size), swapRB=True)
        self.net.setInput(blob)
        output = self.net.forward()
        rows = output[0] if output.ndim == 3 else output
        if rows.ndim != 2 or rows.shape[0] < 5 or rows.shape[0] >= rows.shape[1]:
            raise RuntimeError("person_model_output_invalid")
        rows = rows.T  # (4 + классы, N) → (N, 4 + классы)
        # YOLOv8: cx, cy, w, h, COCO class probabilities (без objectness); person = 0.
        scores = rows[:, 4]
        index = int(np.argmax(scores))
        cx, cy, w, h = (float(v) / size for v in rows[index, :4])
        return float(scores[index]), (cx - w / 2, cy - h / 2, w, h)

    def detect(self, frame: np.ndarray) -> tuple[bool, float, tuple[float, float, float, float]]:
        score, box = self.best(frame)
        return score >= self.confidence, score, box

    def detects_person(self, frame: np.ndarray) -> tuple[bool, float]:
        found, score, _ = self.detect(frame)
        return found, score
