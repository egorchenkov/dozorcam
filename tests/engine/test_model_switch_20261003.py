#!/usr/bin/env python3
"""Смена модели детектора на ходу (model_switch): очередь не теряется, сбой — откат.

Две части. Первая — подменённая сборка детекторов: так детерминированно видно,
какая модель разобрала какой кадр и что во время долгой загрузки новой модели
кадры продолжает разбирать прежняя. Вторая — настоящие веса трёх семейств
(если лежат в кэше бенча, как в test_person_reference_frames): битый файл и файл
чужого семейства откатываются, а детектор после отката по-прежнему видит человека.
"""
from __future__ import annotations

import json
import pathlib
import queue
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from cctv.engine import cctv_bridge, model_switch, person_models
from cctv.engine.model_switch import ModelManager, catalog, request_switch
from cctv.engine.person_models import DetectorSettings

from test_person_reference_frames import PASTE, iou, model_file, scene


class FakeDetector:
    """Детектор-метка: «видит человека» на каждом кадре и говорит, какой он модели."""

    def __init__(self, settings: DetectorSettings, delay: float = 0.0) -> None:
        self.name = f"{settings.family}/{settings.model.name}"
        self.confidence = settings.confidence
        self.delay = delay

    def detect(self, frame):
        time.sleep(self.delay)
        return True, 0.9, (0.0, 0.0, 0.1, 0.1)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.state = self.root / "state"
        self.models = self.root / "models"
        self.state.mkdir()
        self.models.mkdir()
        for name in ("yolov5n.onnx", "yolox_tiny.onnx", "yolov8n.onnx", "broken.onnx"):
            (self.models / name).write_bytes(b"x")
        # Прод: семейство не задано, лежит прежний файл образа → YOLOv5n как раньше.
        self.legacy = self.models / "yolov5n.onnx"
        self.env = {"CCTV_MODEL_DIRS": str(self.models)}
        self.built: list[tuple[str, bool]] = []
        self.build_delay = 0.0
        self.logs: list[str] = []
        # resolve() берёт путь прежнего файла образа аргументом по умолчанию.
        patcher = mock.patch.object(person_models.resolve, "__defaults__",
                                    (person_models.os.environ, self.legacy))
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, settings: DetectorSettings, probe: bool = False):
        self.built.append((settings.model.name, probe))
        time.sleep(self.build_delay)
        if settings.model.name == "broken.onnx":
            raise RuntimeError("cv2.error: failed to parse ONNX")  # как readNetFromONNX на мусоре
        if settings.model.name == "yolov8n.onnx" and settings.family != "yolov8":
            raise RuntimeError("person_model_family_mismatch")
        return FakeDetector(settings)

    def manager(self) -> ModelManager:
        return ModelManager(self.state, self.env, build=self.build, log=self.logs.append)

    def status(self) -> dict:
        return json.loads((self.state / model_switch.STATUS_NAME).read_text())


class StartupTest(Base):
    def test_without_bot_choice_production_model_is_unchanged(self):
        models = self.manager()
        holder = models.register("dacha")
        self.assertEqual((holder.settings.family, holder.settings.model, holder.settings.confidence,
                          holder.settings.reason), ("yolov5", self.legacy, 0.35, "legacy_model_file"))
        self.assertEqual(self.built, [("yolov5n.onnx", False)])  # без пробы — как до мультимодели
        self.assertFalse((self.state / model_switch.CHOICE_NAME).exists())

    def test_bot_choice_survives_restart(self):
        models = self.manager()
        models.register("dacha")
        request_switch(self.state, "yolox", "yolox_tiny.onnx", self.env)
        self.assertEqual(models.poll(), "ok")
        restarted = self.manager()
        holder = restarted.register("dacha")
        self.assertEqual((holder.settings.family, holder.settings.model.name, holder.settings.confidence),
                         ("yolox", "yolox_tiny.onnx", 0.30))
        self.assertIsNone(restarted.poll(), "обработанная заявка не исполняется повторно")

    def test_broken_choice_at_start_rolls_back_to_config(self):
        (self.state / model_switch.CHOICE_NAME).write_text('{"family": "yolox", "model": "broken.onnx"}')
        holder = self.manager().register("dacha")
        self.assertEqual(holder.settings.family, "yolov5")
        self.assertFalse((self.state / model_switch.CHOICE_NAME).exists())
        self.assertEqual(self.status()["state"], "rolled_back")
        self.assertTrue(any("person_model_rollback stage=start" in line for line in self.logs))


class SwitchQueueTest(Base):
    def test_events_before_during_and_after_reload_are_all_processed(self):
        """Очередь кадров камеры разбирается без пропусков, пока модель меняется.

        Поток камеры читает очередь непрерывно; смену заказывает «бот» (заявка
        моста) посреди очереди, а сборка новой модели нарочно долгая — всё это
        время кадры обязана разбирать прежняя модель.
        """
        models = self.manager()
        holder = models.register("dacha")
        frames: queue.Queue = queue.Queue()
        seen: list[tuple[int, str]] = []
        done = threading.Event()

        def camera_loop():
            while True:
                item = frames.get()
                if item is None:
                    done.set()
                    return
                found, _score, _box = holder.detect(np.zeros((4, 4, 3), np.uint8))
                self.assertTrue(found)
                seen.append((item, holder._detector.name))

        threading.Thread(target=camera_loop, daemon=True).start()
        for i in range(50):  # события до смены
            frames.put(i)
        self.build_delay = 0.4
        request_switch(self.state, "yolox", "yolox_tiny.onnx", self.env)
        switcher = threading.Thread(target=models.poll)
        switcher.start()
        for i in range(50, 150):  # события во время загрузки новой модели
            frames.put(i)
            time.sleep(0.002)
        switcher.join(5)
        for i in range(150, 200):  # события после смены
            frames.put(i)
        frames.put(None)
        self.assertTrue(done.wait(5))

        self.assertEqual([i for i, _ in seen], list(range(200)), "кадр потерян или переставлен")
        names = [name for _, name in seen]
        self.assertEqual(names[0], "yolov5/yolov5n.onnx")
        self.assertEqual(names[-1], "yolox/yolox_tiny.onnx")
        switch_at = names.index("yolox/yolox_tiny.onnx")
        self.assertGreater(switch_at, 50, "во время загрузки кадры разбирала прежняя модель")
        self.assertEqual(set(names[switch_at:]), {"yolox/yolox_tiny.onnx"}, "подмена ровно одна")
        self.assertEqual(self.status()["state"], "ok")
        self.assertEqual(self.status()["active"]["family"], "yolox")

    def test_all_cameras_switch_together_and_hook_runs(self):
        models = self.manager()
        holders = [models.register(name) for name in ("city", "dacha")]
        calls = []
        models.on_switch(lambda previous, current, cameras: calls.append((previous.family, current.family, cameras)))
        models.on_switch(lambda *_: 1 / 0)  # сломанный хук смену не откатывает
        self.assertEqual(models.switch({"family": "yolov8", "model": "yolov8n.onnx"}), "ok")
        self.assertEqual({h.settings.family for h in holders}, {"yolov8"})
        self.assertEqual({h.generation for h in holders}, {1})
        self.assertEqual(calls, [("yolov5", "yolov8", ["city", "dacha"])])
        self.assertTrue(any("person_model_hook_failed" in line for line in self.logs))
        self.assertTrue(all(probe for name, probe in self.built if name == "yolov8n.onnx"))


class RollbackTest(Base):
    def assert_rolled_back(self, family: str, model: str, code: str):
        models = self.manager()
        holder = models.register("dacha")
        before = holder._detector
        request_switch(self.state, family, model, self.env)
        self.assertEqual(models.poll(), "rolled_back")
        self.assertIs(holder._detector, before, "прежний детектор не отпускался")
        self.assertEqual(holder.detect(np.zeros((4, 4, 3), np.uint8))[0], True, "детектор работает")
        status = self.status()
        self.assertEqual((status["state"], status["error"], status["active"]["family"]),
                         ("rolled_back", code, "yolov5"))
        self.assertFalse((self.state / model_switch.CHOICE_NAME).exists(), "битый выбор не запомнен")
        menu = catalog(self.state, self.env)
        self.assertEqual(menu["switch"]["state"], "rolled_back")
        self.assertEqual(menu["active"]["model"], "yolov5n.onnx")

    def test_broken_file_rolls_back(self):
        self.assert_rolled_back("yolox", "broken.onnx", "person_model_load_failed")

    def test_file_of_another_family_rolls_back(self):
        self.assert_rolled_back("yolox", "yolov8n.onnx", "person_model_family_mismatch")

    def test_file_removed_after_request_rolls_back(self):
        models = self.manager()
        models.register("dacha")
        request_switch(self.state, "yolox", "yolox_tiny.onnx", self.env)
        (self.models / "yolox_tiny.onnx").unlink()
        real_build = self.build

        def build(settings, probe=False):
            if not settings.model.is_file():
                raise RuntimeError("person_model_missing")
            return real_build(settings, probe)

        models.build = build
        self.assertEqual(models.poll(), "rolled_back")
        self.assertEqual(self.status()["error"], "person_model_missing")


class NoWorkingModelTest(Base):
    def test_camera_waiting_for_model_starts_after_switch_from_bot(self):
        """Неизвестное семейство в конфиге: камера ждёт (а не умирает), выбор из бота её запускает."""
        self.env["CCTV_PERSON_MODEL_FAMILY"] = "rtdetr"
        models = self.manager()
        with self.assertRaises(ValueError):
            models.register("dacha")  # конвейер пишет person_model_unavailable и ждёт
        woke = []
        waiter = threading.Thread(target=lambda: woke.append(models.wait_change(0, 5)))
        waiter.start()
        self.assertEqual(models.switch({"family": "yolox", "model": "yolox_tiny.onnx"}), "ok")
        waiter.join(5)
        self.assertEqual(woke, [1])
        self.assertEqual(models.register("dacha").settings.family, "yolox")

    def test_pipeline_uses_switchable_detectors(self):
        from cctv.engine import cctv_pipeline

        with mock.patch.object(cctv_pipeline, "PERSON_MODELS", None), \
                mock.patch.object(cctv_pipeline, "ModelManager") as manager:
            first = cctv_pipeline.person_models_for(self.root)
            self.assertIs(cctv_pipeline.person_models_for(self.root), first)
        manager.assert_called_once()
        first.start.assert_called_once()


class BridgeSideTest(Base):
    def test_catalog_lists_families_files_and_active_model(self):
        menu = catalog(self.state, self.env)
        self.assertEqual([f["family"] for f in menu["families"]], ["yolov5", "yolox", "yolov8"])
        self.assertEqual([(m["model"], m["hint"]) for m in menu["models"]],
                         [("broken.onnx", None), ("yolov5n.onnx", "yolov5"), ("yolov8n.onnx", "yolov8"),
                          ("yolox_tiny.onnx", "yolox")])
        self.assertEqual(menu["active"]["family"], "yolov5")  # конвейер ещё не отчитался — тот же выбор
        request_switch(self.state, "yolov8", "yolov8n.onnx", self.env)
        self.assertEqual(catalog(self.state, self.env)["switch"]["state"], "pending")

    def test_request_accepts_only_known_family_and_listed_file_name(self):
        with self.assertRaises(ValueError):
            request_switch(self.state, "rtdetr", "yolov8n.onnx", self.env)
        for name in ("../state/x.onnx", "/etc/passwd", "absent.onnx", ""):
            with self.subTest(name=name), self.assertRaises(LookupError):
                request_switch(self.state, "yolox", name, self.env)
        self.assertFalse((self.state / model_switch.REQUEST_NAME).exists())

    def test_bridge_maps_errors_to_contract_codes(self):
        bridge = cctv_bridge.Bridge.__new__(cctv_bridge.Bridge)
        bridge.state_dir = self.state
        with mock.patch.dict("os.environ", self.env):
            with self.assertRaises(cctv_bridge.BridgeError) as missing:
                bridge.switch_detector_model({"family": "yolox", "model": "absent.onnx"})
            self.assertEqual(missing.exception.code, "not_found")
            with self.assertRaises(cctv_bridge.BridgeError) as family:
                bridge.switch_detector_model({"family": "nope", "model": "yolox_tiny.onnx"})
            self.assertEqual(family.exception.code, "unavailable")
            self.assertEqual(bridge.switch_detector_model({"family": "yolox", "model": "yolox_tiny.onnx"})["state"],
                             "pending")


class RealWeightsTest(unittest.TestCase):
    """Настоящие ONNX: смена YOLOv5n → YOLOX → битый файл → чужое семейство."""

    def setUp(self) -> None:
        sources = {name: model_file(name) for name in ("yolov5n.onnx", "yolox_tiny.onnx", "yolov8n.onnx")}
        missing = [name for name, path in sources.items() if path is None]
        if missing:
            self.skipTest(f"нет весов {', '.join(missing)} (кэш бенча)")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.state, models = root / "state", root / "models"
        self.state.mkdir()
        models.mkdir()
        for name, path in sources.items():
            shutil.copy(path, models / name)
        (models / "broken.onnx").write_bytes(b"\x08\x07not an onnx graph" * 64)
        self.env = {"CCTV_MODEL_DIRS": str(models), "CCTV_PERSON_MODEL_FAMILY": "yolov5"}
        self.frame = scene()

    def sees_person(self, holder) -> None:
        found, score, box = holder.detect(self.frame)
        self.assertTrue(found, f"{holder.settings.family}: score {score:.2f}")
        self.assertGreater(iou(box, PASTE), 0.5)

    def test_switch_and_rollback_on_real_models(self):
        models = ModelManager(self.state, self.env, log=lambda _l: None)
        holder = models.register("dacha")
        self.sees_person(holder)
        request_switch(self.state, "yolox", "yolox_tiny.onnx", self.env)
        self.assertEqual(models.poll(), "ok")
        self.assertEqual(holder.settings.family, "yolox")
        self.sees_person(holder)
        # Чужой граф падает по-разному: на входе другого размера (load_failed), в
        # разборе выхода адаптером (output_invalid) или на сверке формы (mismatch).
        wrong = ("person_model_family_mismatch", "person_model_output_invalid", "person_model_load_failed")
        for family, name, code in (("yolov8", "broken.onnx", ("person_model_load_failed",)),
                                   ("yolox", "yolov8n.onnx", wrong),
                                   ("yolov8", "yolox_tiny.onnx", wrong),
                                   ("yolov5", "yolov8n.onnx", wrong)):
            with self.subTest(family=family, file=name):
                request_switch(self.state, family, name, self.env)
                self.assertEqual(models.poll(), "rolled_back")
                status = json.loads((self.state / model_switch.STATUS_NAME).read_text())
                self.assertIn(status["error"], code)
                self.assertEqual(status["active"]["family"], "yolox")
                self.assertEqual(holder.settings.family, "yolox")
                self.sees_person(holder)


if __name__ == "__main__":
    unittest.main()
