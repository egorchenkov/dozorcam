#!/usr/bin/env python3
"""Пропуски людей 06.10.2026 — реплей эпизодов через настоящий detect() до/после 0.1.2.

Эпизоды из журнала движка в тот вечер (камеры обезличены):

* door_out — камера у двери снаружи с сигналом людей ONVIF FieldDetector: камера
  видела 9 проходов, детектор не отправил ни одного события. Три механизма:
  уверенность 0.21–0.25 ниже порога 0.35; один кадр 0.40 без второго
  (PERSON_HITS=2); кадр 0.68 срезан фильтром неподвижных — человек стоял у двери
  дольше окна, а весь кадр менялся (inside 68 %, outside 66 %);
* city — камера без сигнала людей: фильтр неподвижных отвергал людей при смене
  освещения всего кадра (outside 15–30 %, уверенность 0.42–0.69);
* field — большое поле: предмет 0.036×0.078 кадра держит 0.53–0.60 на рассвете,
  0.1.2 не должен превратить его в ложных людей.

«До» — правила 0.1.1 (shadow, обход 0.70, без «не знаю»), «после» — 0.1.2.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from cctv.engine import cctv_pipeline, person_diag
from cctv.engine.onvif_motion_gate import OnvifMotionGate
from detect_replay import NEW, NEW_C683851, OLD, Shot, figure, quiet, replay, scene, walk

T0 = 1_791_300_000.0  # произвольная точка отсчёта (UTC)


def sway(start, scores, level=60, value=220, box=(0.21, 0.36, 0.06, 0.23), step=0.5):
    """Человек стоит, покачиваясь: рамка почти на месте, гейт кадров открыт."""
    return walk(start, scores, box=box, step=step, level=level, dx=0.004, value=value)


class DoorOutBelowThresholdTest(unittest.TestCase):
    """20:17/20:18/20:20 — YOLO 0.21–0.25 при сигнале камеры."""

    def shots(self):
        scores = [0.12, 0.18, 0.21, 0.23, 0.19, 0.25, 0.22, 0.15, 0.10, 0.08]
        return quiet(T0, 20) + walk(T0 + 20, scores) + quiet(T0 + 25, 30)

    def test_before_missed(self):
        result = replay(self.shots(), signals=[T0 + 22], human=True, version=OLD)
        self.assertEqual([], result.events)

    def test_after_caught_by_camera_confirm(self):
        result = replay(self.shots(), signals=[T0 + 22], human=True, version=NEW)
        self.assertEqual(1, len(result.events))
        self.assertTrue(any("confirm=1" in line for line in result.log))
        events = [r for r in result.journal if r["kind"] == "event"]
        self.assertEqual(1, len(events))
        self.assertTrue(events[0]["confirm"])
        self.assertEqual(4, len(events[0]["box"]))

    def test_after_without_camera_signal_stays_silent_and_journals_reject(self):
        """Без сигнала камеры порог прежний: 0.25 — не человек, но след в журнале."""
        result = replay(self.shots(), human=True, version=NEW)
        self.assertEqual([], result.events)
        rejects = [r for r in result.journal if r["kind"] == "reject"]
        self.assertEqual(1, len(rejects))
        self.assertEqual("below_threshold", rejects[0]["reason"])
        self.assertEqual(0.25, rejects[0]["max_conf"])
        self.assertEqual(0, rejects[0]["camera_human"])

    def test_unhealthy_subscription_never_confirms(self):
        """Подтверждение не бывает fail-open: оборванная подписка не снижает порог."""
        gate = OnvifMotionGate("x", "http://192.0.2.10/onvif/Events", "u", "p")
        with mock.patch.object(cctv_pipeline, "HUMAN_GATE_MODE", "confirm"):
            self.assertFalse(cctv_pipeline.human_confirms(gate, T0))
            gate._healthy = True
            self.assertFalse(cctv_pipeline.human_confirms(gate, T0))
            gate.note_motion(T0 + 10)
            self.assertTrue(cctv_pipeline.human_confirms(gate, T0))          # кадр за 10 с до сигнала
            self.assertTrue(cctv_pipeline.human_confirms(gate, T0 + 69))     # 59 с после
            self.assertFalse(cctv_pipeline.human_confirms(gate, T0 - 10))    # 20 с до — вне окна
            self.assertFalse(cctv_pipeline.human_confirms(gate, T0 + 75))
        with mock.patch.object(cctv_pipeline, "HUMAN_GATE_MODE", "shadow"):
            self.assertFalse(cctv_pipeline.human_confirms(gate, T0))


class DoorOutSingleFrameTest(unittest.TestCase):
    """18:58 — один кадр 0.40 из серии, второго не набралось."""

    def shots(self):
        return quiet(T0, 20) + walk(T0 + 20, [0.10, 0.14, 0.40, 0.12, 0.09, 0.11]) + quiet(T0 + 23, 30)

    def test_before_missed(self):
        self.assertEqual([], replay(self.shots(), signals=[T0 + 25], human=True, version=OLD).events)

    def test_after_caught(self):
        self.assertEqual(1, len(replay(self.shots(), signals=[T0 + 25], human=True, version=NEW).events))

    def test_after_without_signal_reject_is_single_frame(self):
        result = replay(self.shots(), human=True, version=NEW_C683851)
        self.assertEqual([], result.events)
        reasons = [r["reason"] for r in result.journal if r["kind"] == "reject"]
        self.assertEqual(["single_frame"], reasons)

    def test_after_quiet_camera_reject_is_camera_quiet(self):
        """С 07.10 при молчащей камере порог 0.60: кадр 0.40 — «камера молчит», а не «один кадр»."""
        result = replay(self.shots(), human=True, version=NEW)
        self.assertEqual([], result.events)
        reasons = [r["reason"] for r in result.journal if r["kind"] == "reject"]
        self.assertEqual(["camera_quiet"], reasons)


class DoorOutStandingAtDoorTest(unittest.TestCase):
    """19:16:24 — 0.68, человек стоял у двери, весь кадр менялся (66 % вне рамки)."""

    def shots(self):
        before = sway(T0, [0.30] * 30, level=60, value=150)            # 15 с: стоит в сумерках
        after = sway(T0 + 15, [0.68, 0.66, 0.50, 0.30], level=100, value=200)  # свет сменился
        return quiet(T0 - 10, 10) + before + after + quiet(T0 + 17, 30)

    def test_before_cut_by_still_filter(self):
        result = replay(self.shots(), signals=[T0 + 2], human=True, version=OLD)
        self.assertEqual([], result.events)
        self.assertTrue(any("person_still" in line for line in result.log))

    def test_after_caught_with_camera_signal(self):
        self.assertEqual(1, len(replay(self.shots(), signals=[T0 + 2], human=True, version=NEW).events))

    def test_after_caught_without_camera_signal_unreliable_reference(self):
        """И без сигнала: эталон при смене всего кадра — «не знаю», а не «предмет»."""
        result = replay(self.shots(), human=True, version=NEW)
        self.assertEqual(1, len(result.events))
        self.assertTrue(any("still=unreliable_reference" in line for line in result.log))
        # Рамка и вердикт фильтра — в журнале: по нему разбирают ложные без лога движка.
        events = [r for r in result.journal if r["kind"] == "event"]
        self.assertEqual(4, len(events[0]["box"]))
        self.assertIn("still=unreliable_reference", events[0]["still"])


class CityLightChangeTest(unittest.TestCase):
    """Камера без сигнала людей: люди 0.42 и 0.56 при outside 15–30 %."""

    def shots(self, scores):
        calm = quiet(T0, 20, level=60)
        passing = walk(T0 + 20, scores, level=95, box=(0.43, 0.22, 0.19, 0.6), dx=0.01)
        return calm + passing + quiet(T0 + 20 + len(scores) / 2, 30, level=95)

    def test_before_cut(self):
        self.assertEqual([], replay(self.shots([0.42, 0.56, 0.45]), camera_id="city", version=OLD).events)

    def test_after_caught(self):
        self.assertEqual(1, len(replay(self.shots([0.42, 0.56, 0.45]), camera_id="city", version=NEW).events))


class FieldStaticObjectTest(unittest.TestCase):
    """Большое поле: предмет держит 0.53–0.60 — 0.1.2 не даёт по нему людей."""

    BOX = (0.48, 0.40, 0.036, 0.078)

    def object_frames(self, start, scores, level=60):
        return [Shot(start + i * 0.5, s, self.BOX, figure(scene(level, 7000 + i), self.BOX, 180))
                for i, s in enumerate(scores)]

    def test_calm_scene_object_no_event(self):
        scores = [0.53, 0.56, 0.60, 0.57, 0.55, 0.58] * 20
        shots = quiet(T0, 30, extra=(self.BOX, 180)) + self.object_frames(T0 + 30, scores)
        for version in (OLD, NEW):
            self.assertEqual([], replay(shots, camera_id="field", version=version, gate_mode="shadow").events)

    def test_dawn_light_change_known_object_no_event(self):
        """Рассвет: сменился весь кадр (как 06.10 06:16, inside 93 / outside 68) —
        предмет в памяти, «не знаю» к нему не применяется."""
        shots = (quiet(T0, 30, level=40, extra=(self.BOX, 120))
                 + self.object_frames(T0 + 30, [0.55, 0.56, 0.60, 0.57] * 10, level=90))
        result = replay(shots, camera_id="field", version=NEW, gate_mode="shadow",
                        static_boxes=[(T0 - 3600, self.BOX)])
        self.assertEqual([], result.events)
        self.assertTrue(any("still=known_static" in line for line in result.log))


class FieldGateBreaksSeriesTest(unittest.TestCase):
    """Большое поле 05.10 07:43–07:45: мелкая фигура меняет кадр через раз ниже
    пола гейта — YOLO видит 0.68–0.83 через кадр, а пропуск гейтом обнулял серию."""

    def shots(self):
        out = quiet(T0, 10)
        box = (0.30, 0.40, 0.03, 0.07)
        image = figure(scene(60, 1), box)
        for i in range(12):
            if i % 2 == 0:  # фигура сдвинулась — кадр изменился
                box = (box[0] + 0.01, box[1], box[2], box[3])
                image = figure(scene(60, 1), box)
            out.append(Shot(T0 + 10 + i * 0.5, 0.75, box, image))  # нечётный кадр = копия
        return out + quiet(T0 + 16, 20)

    def test_before_missed(self):
        self.assertEqual([], replay(self.shots(), camera_id="field", version=OLD).events)

    def test_after_hit_holds_gate_open(self):
        self.assertEqual(1, len(replay(self.shots(), camera_id="field", version=NEW).events))


class PairSummaryTest(unittest.TestCase):
    def test_classes(self):
        records = [
            # проход: событие на обеих камерах
            {"kind": "event", "camera": "door_in", "at": T0, "conf": 0.8},
            {"kind": "event", "camera": "door_out", "at": T0 + 30, "conf": 0.5, "confirm": True},
            # одиночное объяснимое: на второй камере никакого следа
            {"kind": "event", "camera": "door_in", "at": T0 + 1000, "conf": 0.6},
            # подозрительное: на door_out был сигнал камеры и отказ, события нет
            {"kind": "event", "camera": "door_in", "at": T0 + 2000, "conf": 0.7},
            {"kind": "camera_signal", "camera": "door_out", "at": T0 + 2010},
            {"kind": "reject", "camera": "door_out", "start": T0 + 2012, "end": T0 + 2020, "frames": 9,
             "max_conf": 0.25, "reason": "below_threshold", "snapshot": "door_out/x.jpg"},
            # камера видела человека, событий нет нигде
            {"kind": "camera_signal", "camera": "door_out", "at": T0 + 5000},
        ]
        summary = person_diag.summarize(records, [("door_in", "door_out")], 120)
        pair = summary["pairs"][0]
        self.assertEqual(1, pair["paired"])
        self.assertEqual(1, pair["single_explained"])
        self.assertEqual(1, len(pair["suspicious"]))
        item = pair["suspicious"][0]
        self.assertEqual(("door_in", "door_out"), (item["camera"], item["missing_on"]))
        self.assertEqual("door_out/x.jpg", item["trace"][1]["snapshot"])
        silent = summary["camera_signal_no_event"]
        self.assertEqual([T0 + 2010, T0 + 5000], [s["at"] for s in silent])
        self.assertTrue(silent[0]["partner_event"])
        text = person_diag.render(summary, "2026-10-06", ZoneInfo("Europe/Moscow"), "ru")
        self.assertIn("парных 1, одиночных объяснимых 1, подозрительных 1", text)
        self.assertIn("door_out/x.jpg", text)
        self.assertIn("не вердикт", text)
        english = person_diag.render(summary, "2026-10-06", ZoneInfo("Europe/Moscow"), "en")
        self.assertIn("paired 1, single explained 1, suspicious 1", english)
        self.assertIn("not a verdict", english)
        self.assertNotRegex(english, "[А-Яа-яЁё]")

    def test_journal_roundtrip_and_daily_files(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            journal = person_diag.Journal(pathlib.Path(tmp), ZoneInfo("Europe/Moscow"), log=lambda _l: None)
            journal.write({"kind": "event", "camera": "a", "at": 1791320400.0})  # 06.10 21:00 UTC = 07.10 00:00 МСК
            self.assertTrue((pathlib.Path(tmp) / "journal-2026-10-07.jsonl").exists())
            summary = person_diag.write_summary(journal, "2026-10-07", log=lambda _l: None)
            self.assertEqual(1, summary["cameras"]["a"]["events"])
            self.assertTrue((pathlib.Path(tmp) / "summary-2026-10-07.txt").exists())

    def test_day_boundary_follows_cctv_tz(self):
        """Сутки сводки — по CCTV_TZ: Катманду +05:45, Ньюфаундленд −02:30; без пояса — UTC."""
        at = 1791320400.0  # 06.10 21:00 UTC
        with mock.patch.dict(os.environ, {"CCTV_TZ": "Asia/Kathmandu"}):
            self.assertEqual(("2026-10-07", "02:45:00"), (person_diag.local_day(at), person_diag.local_time(at)))
        with mock.patch.dict(os.environ, {"CCTV_TZ": "America/St_Johns"}):
            self.assertEqual(("2026-10-06", "18:30:00"), (person_diag.local_day(at), person_diag.local_time(at)))
        for unset in ("", "Mars/Olympus", "../etc/passwd"):
            with mock.patch.dict(os.environ, {"CCTV_TZ": unset}):
                self.assertEqual("21:00:00", person_diag.local_time(at))


if __name__ == "__main__":
    unittest.main()
