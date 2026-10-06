#!/usr/bin/env python3
"""Кадры person-detector берутся только из закрытых сегментов recorder'а."""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock
from unittest.mock import patch

from cctv.engine import cctv_pipeline  # noqa: E402
from cctv.engine.cctv_bridge import Camera  # noqa: E402


class FakeCapture:
    opened: list[str] = []

    def __init__(self, path: str) -> None:
        self.path, self.frames = path, ["main-frame"]
        type(self).opened.append(path)

    def isOpened(self): return True
    def get(self, _property): return 5
    def grab(self): return bool(self.frames)
    def read(self): return (True, self.frames.pop(0)) if self.frames else (False, None)
    def release(self): pass


def write(target: pathlib.Path, name: str, age: float = 5.0) -> pathlib.Path:
    path = target / name
    path.write_bytes(b"segment")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


class RecordedMainStreamTest(unittest.TestCase):
    def setUp(self):
        FakeCapture.opened = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.target = self.root / "buffer" / "cam"
        self.target.mkdir(parents=True)
        self.camera = Camera("cam", "cam", "cam", "rtsp://unused")

    def stream(self):
        return cctv_pipeline.RecordedMainStream(self.root, self.camera)

    def test_skips_old_buffer_and_opens_only_next_closed_segment(self):
        write(self.target, "2026-09-02T18:00:00Z.ts")
        stream = self.stream()
        with patch.object(cctv_pipeline.cv2, "VideoCapture", FakeCapture):
            self.assertEqual(stream.read(), (None, False))
            self.assertEqual(FakeCapture.opened, [])
            # Появился 18:00:05 — значит 18:00:00 закрыт, но он из старого буфера.
            write(self.target, "2026-09-02T18:00:05Z.ts")
            self.assertEqual(stream.read(), (None, False))
            self.assertEqual(FakeCapture.opened, [])
            write(self.target, "2026-09-02T18:00:10Z.ts")
            self.assertEqual(stream.read(), ("main-frame", False))
            self.assertEqual(FakeCapture.opened, [str(self.target / "2026-09-02T18:00:05Z.ts")])

    def test_open_segment_is_never_read_even_when_its_mtime_went_stale(self):
        """Главный дефект 21.09.2026: ffmpeg сбрасывает TS блоками по 0.5-1 МБ,
        поэтому mtime открытого файла застывает на секунды. Выдержка по времени
        считала его готовым, OpenCV декодировал обрезанный GOP — и в Telegram
        уезжал кадр, размазанный вертикальными полосами."""
        write(self.target, "2026-09-02T18:00:00Z.ts")
        stream = self.stream()
        with patch.object(cctv_pipeline.cv2, "VideoCapture", FakeCapture):
            self.assertEqual(stream.read(), (None, False))
            # Пишется 18:00:05, его mtime «протух» на 30 с — читать его нельзя.
            write(self.target, "2026-09-02T18:00:05Z.ts", age=30)
            self.assertEqual(stream.read(), (None, False))
            self.assertEqual(FakeCapture.opened, [])

    def test_freshly_created_segment_waits_out_the_settle_window(self):
        write(self.target, "2026-09-02T18:00:00Z.ts")
        stream = self.stream()
        with patch.object(cctv_pipeline.cv2, "VideoCapture", FakeCapture):
            self.assertEqual(stream.read(), (None, False))
            write(self.target, "2026-09-02T18:00:05Z.ts")
            self.assertEqual(stream.read(), (None, False))
            # 18:00:10 только что создан: 18:00:05 закрыт, но выдержка ещё идёт.
            write(self.target, "2026-09-02T18:00:10Z.ts", age=0)
            write(self.target, "2026-09-02T18:00:05Z.ts", age=0)
            self.assertEqual(stream.read(), (None, False))
            self.assertEqual(FakeCapture.opened, [])

    def test_frame_time_is_segment_start_plus_frame_offset(self):
        """Регрессия 23.09.2026: детектор отстаёт от записи на десятки секунд, и
        событие с временем «сейчас» центрировало клип мимо кадра с человеком."""

        class OffsetCapture(FakeCapture):
            def get(self, prop):
                return 3500.0 if prop == cctv_pipeline.cv2.CAP_PROP_POS_MSEC else 5

        write(self.target, "2026-09-02T18:00:00Z.ts")
        stream = self.stream()
        with patch.object(cctv_pipeline.cv2, "VideoCapture", OffsetCapture):
            stream.read()
            self.assertIsNone(stream.frame_captured_at)
            write(self.target, "2026-09-02T18:00:05Z.ts")
            write(self.target, "2026-09-02T18:00:10Z.ts")
            self.assertEqual(stream.read(), ("main-frame", False))
            expected = cctv_pipeline.segment_started_at(self.target / "2026-09-02T18:00:05Z.ts") + 3.5
            self.assertEqual(stream.frame_captured_at, expected)

    def read_through(self, stream, name):
        """Разобрать сегмент до конца: кадр, затем конец файла."""
        self.assertEqual(stream.read(), ("main-frame", False))
        self.assertEqual(stream.read(), (None, False))
        self.assertEqual(FakeCapture.opened[-1], str(self.target / name))

    def test_buffer_overtaking_the_cursor_is_logged_and_marks_detector_behind(self):
        """D-20261003-01: буфер удалял неразобранные сегменты, курсор молча
        перепрыгивал, а в журнале и статусе не было ни следа перегруза."""
        write(self.target, "2026-09-02T18:00:00Z.ts")
        stream = self.stream()
        with patch.object(cctv_pipeline.cv2, "VideoCapture", FakeCapture):
            stream.read()
            write(self.target, "2026-09-02T18:00:05Z.ts")
            write(self.target, "2026-09-02T18:00:10Z.ts")
            self.read_through(stream, "2026-09-02T18:00:05Z.ts")
            self.assertFalse(stream.behind())
            # Детектор отстал: буфер удалил 18:00:00…18:00:15, осталось с 18:00:20.
            for name in ("2026-09-02T18:00:00Z.ts", "2026-09-02T18:00:05Z.ts", "2026-09-02T18:00:10Z.ts"):
                (self.target / name).unlink()
            write(self.target, "2026-09-02T18:00:20Z.ts")
            write(self.target, "2026-09-02T18:00:25Z.ts")
            with mock.patch("builtins.print") as out:
                self.assertEqual(stream.read(), ("main-frame", False))
            self.assertEqual(FakeCapture.opened[-1], str(self.target / "2026-09-02T18:00:20Z.ts"))
            line = out.call_args[0][0]
            self.assertIn("detector_skipped_segments camera=cam count=2", line)
            self.assertIn("total=2", line)
            self.assertTrue(stream.behind())
            with mock.patch.object(cctv_pipeline, "DETECTOR_BEHIND_HOLD_SEC", 0):
                self.assertFalse(stream.behind())

    def test_deleting_only_the_parsed_segment_is_not_a_skip(self):
        write(self.target, "2026-09-02T18:00:00Z.ts")
        stream = self.stream()
        with patch.object(cctv_pipeline.cv2, "VideoCapture", FakeCapture):
            stream.read()
            write(self.target, "2026-09-02T18:00:05Z.ts")
            write(self.target, "2026-09-02T18:00:10Z.ts")
            self.read_through(stream, "2026-09-02T18:00:05Z.ts")
            (self.target / "2026-09-02T18:00:00Z.ts").unlink()
            (self.target / "2026-09-02T18:00:05Z.ts").unlink()
            write(self.target, "2026-09-02T18:00:15Z.ts")
            with mock.patch("builtins.print") as out:
                self.assertEqual(stream.read(), ("main-frame", False))
            out.assert_not_called()
            self.assertEqual(stream.skipped_segments, 0)
            self.assertFalse(stream.behind())


class PersonGateDefaultTest(unittest.TestCase):
    def test_default_is_enforce(self):
        """D-20261003-01: shadow по умолчанию гонял YOLO на каждом кадре (~10x CPU)."""
        import subprocess

        # Отдельный процесс: reload модуля подменил бы классы под другими тестами.
        env = {k: v for k, v in os.environ.items() if k != "CCTV_PERSON_GATE_MODE"}
        out = subprocess.run([sys.executable, "-c", "from cctv.engine import cctv_pipeline as p; "
                              "print(p.PERSON_GATE_MODE)"], env=env, capture_output=True,
                             text=True, check=True, cwd=pathlib.Path(__file__).resolve().parents[2])
        self.assertEqual("enforce", out.stdout.strip())


class FakeProcess:
    def __init__(self, stop_on: str) -> None:
        self.stop_on, self.signals, self.returncode = stop_on, [], None

    def communicate(self, timeout=None):
        if self.stop_on == "cycle" and not self.signals:
            raise __import__("subprocess").TimeoutExpired("ffmpeg", timeout)
        if self.stop_on == "never":
            if "kill" not in self.signals:
                raise __import__("subprocess").TimeoutExpired("ffmpeg", timeout)
            self.returncode = -9
            return (None, b"")
        self.returncode = 0
        return (None, b"")

    def terminate(self): self.signals.append("term")
    def kill(self): self.signals.append("kill")


class CyclingProcess(FakeProcess):
    """ffmpeg, который переживает N циклов обслуживания и потом выходит сам."""

    def __init__(self, cycles, returncode=0):
        super().__init__("self"); self.cycles, self.exit_code, self.waits = cycles, returncode, 0

    def communicate(self, timeout=None):
        if self.signals:
            self.returncode = 0; return (None, b"")
        self.waits += 1
        if self.waits <= self.cycles:
            raise __import__("subprocess").TimeoutExpired("ffmpeg", timeout)
        self.returncode = self.exit_code
        return (None, b"stream error")


class RecorderRunTest(unittest.TestCase):
    """ffmpeg живёт сквозь циклы обслуживания (25.09.2026): перезапуск каждые 130 с
    давал дыру 7–9 с в записи и на dacha3 — падение на первом пакете новой
    RTSP-сессии («first pts and dts value must be set»), 138 раз в сутки."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.target = pathlib.Path(self.tmp.name)
        self.camera = Camera("cam", "cam", "cam", "rtsp://unused")
        self.maintained = []
        self.patches = [mock.patch.object(cctv_pipeline, "maintain_buffer", self.maintained.append),
                        mock.patch.object(cctv_pipeline, "RECORDER_STALL_SEC", 45.0)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def touch(self, age):
        f = self.target / "seg.ts"; f.write_bytes(b"x")
        os.utime(f, (time.time() - age, time.time() - age))

    def test_cycle_end_keeps_ffmpeg_and_maintains_buffer(self):
        self.touch(age=3)
        process = CyclingProcess(cycles=3)
        cctv_pipeline.run_recorder(process, self.camera, self.target)
        self.assertEqual(process.signals, [])
        self.assertEqual(self.maintained, [self.target] * 3)

    def test_stalled_segments_restart_recorder_gracefully(self):
        self.touch(age=120)
        process = CyclingProcess(cycles=5)
        with mock.patch("builtins.print") as out:
            cctv_pipeline.run_recorder(process, self.camera, self.target)
        self.assertEqual(process.signals, ["term"])
        self.assertIn("no fresh segment", out.call_args[0][0])

    def test_first_cycle_without_segments_is_not_a_stall(self):
        # Сразу после старта файлов ещё нет — это не залипание, пока не вышел RECORDER_STALL_SEC.
        process = CyclingProcess(cycles=1)
        cctv_pipeline.run_recorder(process, self.camera, self.target)
        self.assertEqual(process.signals, [])

    def test_stream_error_is_reported_with_buffer_name(self):
        process = CyclingProcess(cycles=0, returncode=1)
        with mock.patch("builtins.print") as out:
            cctv_pipeline.run_recorder(process, self.camera, self.target)
        self.assertEqual(process.signals, [])
        self.assertIn(f"buffer={self.target.name}", out.call_args[0][0])
        self.assertIn("stream error", out.call_args[0][0])


class RecorderStopTest(unittest.TestCase):
    """Остановка рекордера закрывает сегмент SIGTERM, а не рвёт его SIGKILL."""

    def test_stop_sends_sigterm_not_sigkill(self):
        process = FakeProcess("cycle")
        cctv_pipeline.stop_recorder(process, Camera("cam", "cam", "cam", "rtsp://unused"))
        self.assertEqual(process.signals, ["term"])

    def test_process_that_ignores_sigterm_is_killed(self):
        process = FakeProcess("never")
        cctv_pipeline.stop_recorder(process, Camera("cam", "cam", "cam", "rtsp://unused"))
        self.assertEqual(process.signals, ["term", "kill"])


class DetectBufferTest(unittest.TestCase):
    """Детектор G5 читает записанный substream, а не main 4 Мп (23.09.2026)."""

    def test_substream_camera_reads_own_detect_buffer(self):
        camera = Camera("cam", "cam", "cam", "rtsp://main", detect_rtsp_url="rtsp://sub",
                        person_detection=True, detect_substream=True)
        stream = cctv_pipeline.RecordedMainStream(pathlib.Path("/s"), camera)
        self.assertEqual(stream.target, pathlib.Path("/s/buffer/cam.detect"))

    def test_without_substream_url_stays_on_main_buffer(self):
        camera = Camera("cam", "cam", "cam", "rtsp://main", detect_substream=True)
        self.assertEqual(cctv_pipeline.detect_buffer_name(camera), "cam")

    def test_detect_recorder_drops_audio(self):
        command, _ = cctv_pipeline.segment_command("rtsp://sub", pathlib.Path("/b"), audio=False)
        self.assertIn("-an", command)
        self.assertNotIn("aac", command)
        main, _ = cctv_pipeline.segment_command("rtsp://main", pathlib.Path("/b"))
        self.assertIn("aac", main)

    def test_aac_camera_audio_is_copied_not_transcoded(self):
        """dacha3 25.09.2026: перекодированный звук после битого NAL терял pts и ронял муксер."""
        main, _ = cctv_pipeline.segment_command("rtsp://main", pathlib.Path("/b"), audio_copy=True)
        self.assertEqual("copy", main[main.index("-c:a") + 1])
        self.assertNotIn("-b:a", main)
        detect, _ = cctv_pipeline.segment_command("rtsp://sub", pathlib.Path("/b"), audio=False, audio_copy=True)
        self.assertIn("-an", detect)

    def test_probe_audio_codec_only_for_rtsp(self):
        self.assertIsNone(cctv_pipeline.probe_audio_codec("/tmp/file.ts"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
