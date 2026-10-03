"""Локальная классификация людей для CCTV Bridge.

Модель и кадры не покидают сервер. Этот модуль принимает уже декодированный
кадр, возвращает только достоверность класса COCO ``person`` и не знает ни
RTSP-адресов, ни Telegram.
"""
from __future__ import annotations

import os
import pathlib

import cv2
import numpy as np


MODEL_PATH = pathlib.Path(os.environ.get("CCTV_PERSON_MODEL", "/usr/share/cctv/models/yolov5n.onnx"))
INPUT_SIZE = 640  # pinned YOLOv5n export; его граф не принимает 320x320.
# На main-кадрах городской камеры живой проход дал серию 0.71, 0.41, 0.40.
# Порог 0.45 отбрасывал два соседних кадра и поэтому не мог выполнить
# обязательное подтверждение PERSON_HITS=2. 0.35 оставляет запас над фоном
# (~0.10), а единичный ошибочный кадр по-прежнему не создаёт тревогу.
PERSON_CONFIDENCE = float(os.environ.get("CCTV_PERSON_CONFIDENCE", "0.35"))


class PersonDetector:
    """CPU-only YOLOv5n detector, один экземпляр на поток камеры."""

    def __init__(self, model_path: pathlib.Path = MODEL_PATH, confidence: float = PERSON_CONFIDENCE) -> None:
        if not model_path.is_file():
            raise RuntimeError("person_model_missing")
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        self.confidence = confidence

    def score(self, frame: np.ndarray) -> float:
        """Вернуть максимальную достоверность именно человека (COCO class 0)."""
        return self.best(frame)[0]

    def best(self, frame: np.ndarray) -> tuple[float, tuple[float, float, float, float]]:
        """Лучшая рамка человека: (достоверность, (x, y, w, h) в долях кадра).

        Рамка нужна фильтру неподвижных объектов (still_object_filter): он
        сравнивает именно это место кадра с тем же местом несколько секунд
        назад. Координаты нормированы к кадру, а не к 640×640: blob растянут
        без letterbox, поэтому деление на INPUT_SIZE и есть доля кадра.
        """
        # Letterbox (сохранение пропорций) опробован 02.09.2026 и снял живой
        # combined-score substream'а городской камеры (320×240) с ~0.11 до ~0.013:
        # растяжение без сохранения пропорций случайно компенсирует нехватку
        # разрешения, вытягивая фигуру. Реальные цифры важнее "правильного" препроцессинга.
        blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, (INPUT_SIZE, INPUT_SIZE), swapRB=True)
        self.net.setInput(blob)
        output = self.net.forward()
        rows = output[0] if output.ndim == 3 else output
        if rows.ndim != 2 or rows.shape[1] < 6:
            raise RuntimeError("person_model_output_invalid")
        if not len(rows):
            return 0.0, (0.0, 0.0, 0.0, 0.0)
        # YOLOv5: x, y, w, h, objectness, COCO class probabilities; person = 0.
        scores = rows[:, 4] * rows[:, 5]
        index = int(np.argmax(scores))
        cx, cy, w, h = (float(v) / INPUT_SIZE for v in rows[index, :4])
        return float(scores[index]), (cx - w / 2, cy - h / 2, w, h)

    def detect(self, frame: np.ndarray) -> tuple[bool, float, tuple[float, float, float, float]]:
        score, box = self.best(frame)
        return score >= self.confidence, score, box

    def detects_person(self, frame: np.ndarray) -> tuple[bool, float]:
        found, score, _ = self.detect(frame)
        return found, score
