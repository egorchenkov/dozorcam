"""Реплей настоящего цикла ``cctv_pipeline.detect()`` на сценарии кадров.

Камера, буфер, YOLO и мост подменены: кадр — синтетическая картинка (для гейта
кадров и фильтра неподвижных) плюс заданная оценка и рамка «YOLO», время —
время съёмки кадра. Всё остальное — боевой код цикла: гейты, серия кадров,
confirm, фильтр неподвижных, журнал отказов. Так эпизоды пропусков проверяются
до/после без камеры и без модели.
"""
from __future__ import annotations

import dataclasses
import functools
import os
import pathlib
import tempfile
from unittest import mock

import numpy as np

from cctv.engine import cctv_pipeline, person_diag
from cctv.engine.cctv_bridge import Camera
from cctv.engine.onvif_motion_gate import OnvifMotionGate
from cctv.engine.still_object_filter import StillObjectFilter

SHAPE = (180, 320, 3)


class Stop(BaseException):
    """Кадры кончились — выход из бесконечного цикла detect() мимо его except Exception."""


@dataclasses.dataclass
class Shot:
    at: float
    score: float
    box: tuple = (0.4, 0.3, 0.1, 0.4)
    image: np.ndarray | None = None


def scene(level: int = 60, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.full(SHAPE, level, dtype=np.int16) + rng.integers(-3, 4, SHAPE)
    return np.clip(base, 0, 255).astype(np.uint8)


def figure(image: np.ndarray, box, value: int = 220) -> np.ndarray:
    out = image.copy()
    h, w = out.shape[:2]
    out[int(box[1] * h):int((box[1] + box[3]) * h), int(box[0] * w):int((box[0] + box[2]) * w)] = value
    return out


def walk(start: float, scores, box=(0.4, 0.3, 0.1, 0.4), step: float = 0.5, level: int = 60,
         dx: float = 0.01, value: int = 220) -> list[Shot]:
    """Идущий человек: рамка сдвигается каждый кадр, фон спокоен."""
    shots = []
    for i, score in enumerate(scores):
        moved = (min(0.89, box[0] + dx * i), box[1], box[2], box[3])
        shots.append(Shot(start + i * step, score, moved, figure(scene(level, i), moved, value)))
    return shots


def stand(start: float, scores, box=(0.4, 0.3, 0.1, 0.4), step: float = 0.5, level: int = 60,
          value: int = 220) -> list[Shot]:
    """Стоящий человек (или предмет): та же рамка, кадры почти одинаковы."""
    return [Shot(start + i * step, score, box, figure(scene(level, 1000 + i), box, value))
            for i, score in enumerate(scores)]


def quiet(start: float, seconds: float, step: float = 0.5, level: int = 60, score: float = 0.05,
          extra=None) -> list[Shot]:
    out = []
    for i in range(int(seconds / step)):
        image = scene(level, 5000 + i)
        if extra is not None:
            image = figure(image, extra[0], extra[1])
        out.append(Shot(start + i * step, score, (0.0, 0.0, 0.0, 0.0), image))
    return out


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, _seconds: float) -> None:
        pass


class FakeStream:
    def __init__(self, shots: list[Shot], clock: Clock, lag: float) -> None:
        self.shots, self.clock, self.lag, self.index = shots, clock, lag, -1

    def read(self):
        self.index += 1
        if self.index >= len(self.shots):
            raise Stop()
        shot = self.shots[self.index]
        self.clock.now = shot.at + self.lag
        return shot.image, False

    @property
    def frame_captured_at(self):
        return self.shots[self.index].at if self.index >= 0 else None

    @property
    def current_started_at(self):
        at = self.frame_captured_at
        return None if at is None else at - at % 5

    def behind(self) -> bool:
        return False


class FakeDetector:
    def __init__(self, scores: dict, confidence: float) -> None:
        self.scores, self.confidence = scores, confidence
        self.settings = mock.Mock(family="yolov5", confidence=confidence, reason="test")
        self.settings.model.name = "fake.onnx"
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        score, box = self.scores[id(frame)]
        return score >= self.confidence, score, box


class FakeBridge:
    def __init__(self, storage: pathlib.Path) -> None:
        self.storage, self.events = storage, []

    def motion(self, camera, captured_at, source=None, snapshot_body=None):
        self.events.append(captured_at)


@dataclasses.dataclass
class Result:
    events: list
    journal: list
    yolo_frames: int
    log: list


OLD = dict(human_mode="shadow", bypass=0.70, unreliable_outside=1000.0, static_iou=2.0, hit_hold=0.0,
           confirm_still=0.0, quiet=0.0, quiet_cameras=())
# Первая сборка 0.1.2 (c683851, в проде 07.10 00:30) — без фильтра в окне confirm.
NEW_C683851 = dict(human_mode="confirm", bypass=0.55, unreliable_outside=15.0, static_iou=0.5, hit_hold=5.0,
                   confirm_still=0.0, quiet=0.0, quiet_cameras=())
# 0.1.2 после ложных 07.10: фильтр неподвижных и в окне confirm, порог 0.60 при молчащей камере.
# Порог при молчащей камере — поимённо: door_out — камера у двери снаружи.
NEW = dict(NEW_C683851, confirm_still=3.0, quiet=0.60, quiet_cameras=("door_out",))


def replay(shots: list[Shot], *, signals=(), human=False, version=NEW, confidence=0.35,
           lag: float = 7.0, camera_id: str = "door_out", gate_mode: str = "enforce",
           static_boxes=()) -> Result:
    """Прогнать сценарий через detect(). ``signals`` — моменты сигнала камеры
    (ONVIF FieldDetector), ``human`` — у камеры есть такая подписка."""
    clock = Clock()
    log: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        storage = pathlib.Path(tmp)
        bridge = FakeBridge(storage)
        detector = FakeDetector({id(s.image): (s.score, s.box) for s in shots}, confidence)
        models = mock.Mock(generation=0)
        models.register.return_value = detector
        gate = None
        if human:
            gate = OnvifMotionGate(camera_id, "http://192.0.2.10/onvif/Events", "u", "p",
                                   topics=("FieldDetector",), label="human_gate")
            gate._healthy = True
            gate.start = lambda: None  # без сети: подписка — это сигналы сценария
            for at in signals:
                gate.note_motion(at)
                gate.note_state(True, at)  # true камеры — и сигнал, и «цель в зоне»
        cam = Camera(camera_id=camera_id, title=camera_id, site="test", rtsp_url="rtsp://192.0.2.10/x",
                     person_detection=True, camera_human_events=human)
        still = functools.partial(StillObjectFilter, bypass=version["bypass"],
                                  unreliable_outside=version["unreliable_outside"],
                                  static_iou=version["static_iou"])

        def make_still(**kw):
            filt = still(**kw)
            filt.static_boxes.extend(static_boxes)
            return filt

        def build_gate(_camera):
            if gate is not None:
                journal = cctv_pipeline.diag_journal()
                gate.on_motion = None
                if journal is not None:
                    for at in signals:
                        journal.write({"kind": "camera_signal", "camera": camera_id, "at": at})
            return gate

        printed = lambda *a, **k: log.append(" ".join(str(x) for x in a))
        with mock.patch.dict(os.environ, {"CCTV_STATE_DIR": str(storage / "state")}), \
                mock.patch.object(cctv_pipeline, "time", clock), \
                mock.patch.object(person_diag, "time", clock), \
                mock.patch.object(cctv_pipeline, "RecordedMainStream", lambda *_: FakeStream(shots, clock, lag)), \
                mock.patch.object(cctv_pipeline, "person_models_for", lambda _s: models), \
                mock.patch.object(cctv_pipeline, "person_thresholds_for", mock.Mock(side_effect=RuntimeError)), \
                mock.patch.object(cctv_pipeline, "build_human_gate", build_gate), \
                mock.patch.object(cctv_pipeline, "lower_thread_priority", lambda: None), \
                mock.patch.object(cctv_pipeline, "StillObjectFilter", make_still), \
                mock.patch.object(cctv_pipeline, "HUMAN_GATE_MODE", version["human_mode"]), \
                mock.patch.object(cctv_pipeline, "PERSON_GATE_MODE", gate_mode), \
                mock.patch.object(cctv_pipeline, "PERSON_HIT_HOLD_SEC", version["hit_hold"]), \
                mock.patch.object(cctv_pipeline, "HUMAN_CONFIRM_STILL_INSIDE", version["confirm_still"]), \
                mock.patch.object(cctv_pipeline, "HUMAN_QUIET_CONFIDENCE", version["quiet"]), \
                mock.patch.object(cctv_pipeline, "HUMAN_QUIET_CAMERAS", frozenset(version["quiet_cameras"])), \
                mock.patch.object(cctv_pipeline, "DIAG_JOURNAL", None), \
                mock.patch("builtins.print", printed):
            try:
                cctv_pipeline.detect(cam, bridge)
            except Stop:
                pass
            # Хвост: закрыть открытый эпизод, как это сделал бы следующий кадр.
            journal = cctv_pipeline.DIAG_JOURNAL
            records = []
            if journal is not None:
                for path in sorted(journal.root.glob("journal-*.jsonl")):
                    records += journal.read(path.stem.split("-", 1)[1])
        return Result(bridge.events, records, detector.calls, log)
