#!/usr/bin/env python3
"""Управление камерой из бота: пауза, имя, снятие — и что пауза глушит событие.

Реестр камер (/etc/cctv-bridge/cameras.json) принадлежит root и мосту на запись
недоступен: адреса и пароли камер кнопка в чате менять не должна. Поэтому
оперативное состояние живёт отдельным оверлеем, и проверяется здесь именно он —
что состояние переживает перезапуск, что пауза видна в реестре и что
поставленная на паузу камера перестаёт слать события.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

from cctv.engine import cctv_bridge  # noqa: E402
from cctv.engine import cctv_pipeline  # noqa: E402
from cctv.engine.cctv_bridge import Bridge, BridgeError  # noqa: E402

CONFIG = {"cameras": [
    {"camera_id": "dacha", "title": "Дача", "site": "Дача",
     "rtsp_url": "rtsp://user:pw@198.51.100.201:554/ch0"},
    {"camera_id": "city", "title": "Город", "site": "Город",
     "rtsp_url": "rtsp://user:pw@192.0.2.10:554/Streaming/Channels/101"},
]}


class CameraControl(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = pathlib.Path(self.tmp.name)
        self.bridge = Bridge(CONFIG, self.storage, "https://cctv-bridge:9443")
        self.addCleanup(self.tmp.cleanup)

    def status_of(self, camera_id: str, bridge: Bridge | None = None) -> str:
        registry = (bridge or self.bridge).registry()
        return next(c["status"] for c in registry["cameras"] if c["camera_id"] == camera_id)

    def title_of(self, camera_id: str) -> str:
        registry = self.bridge.registry()
        return next(c["title"] for c in registry["cameras"] if c["camera_id"] == camera_id)

    def test_pause_shows_in_registry_and_not_as_broken_stream(self) -> None:
        """Пауза не должна выглядеть как «нет кадров»: это разные новости."""
        self.assertEqual(self.status_of("dacha"), "unavailable")  # сегментов нет
        self.bridge.set_override("dacha", "pause")
        self.assertEqual(self.status_of("dacha"), "paused")
        self.assertEqual(self.status_of("city"), "unavailable")  # соседа не задело

    def test_resume_returns_camera_to_live_status(self) -> None:
        self.bridge.set_override("dacha", "pause")
        self.bridge.set_override("dacha", "resume")
        self.assertEqual(self.status_of("dacha"), "unavailable")
        self.assertEqual(json.loads(self.bridge.overrides_path.read_text()), {})

    def test_state_survives_restart(self) -> None:
        """Оверлей на диске: перезапуск моста не должен снимать паузу молча."""
        self.bridge.set_override("dacha", "pause")
        again = Bridge(CONFIG, self.storage, "https://cctv-bridge:9443")
        self.assertEqual(self.status_of("dacha", again), "paused")

    def test_rename_changes_only_title(self) -> None:
        self.bridge.set_override("dacha", "rename", "Дача · двор")
        self.assertEqual(self.title_of("dacha"), "Дача · двор")
        self.assertEqual(self.title_of("city"), "Город")

    def test_rename_requires_non_empty_title(self) -> None:
        for bad in (None, "", "   "):
            with self.assertRaises(BridgeError):
                self.bridge.set_override("dacha", "rename", bad)

    def test_unknown_camera_and_action_rejected(self) -> None:
        with self.assertRaises(BridgeError) as unknown:
            self.bridge.set_override("нет-такой", "pause")
        self.assertEqual(unknown.exception.code, "not_found")
        with self.assertRaises(BridgeError):
            self.bridge.set_override("dacha", "delete-everything")

    def test_retire_is_a_separate_state(self) -> None:
        self.bridge.set_override("dacha", "retire")
        self.assertEqual(self.status_of("dacha"), "retired")

    def test_registry_reports_storage_budget(self) -> None:
        """Архив живёт в Telegram, диск транзитный — сторожим переполнение."""
        (self.storage / "media" / "x.bin").write_bytes(b"0" * 1024)
        storage = self.bridge.registry()["storage"]
        self.assertGreaterEqual(storage["used_bytes"], 1024)
        self.assertEqual(storage["budget_bytes"], cctv_bridge.STORAGE_BUDGET_BYTES)
        self.assertGreater(storage["free_bytes"], 0)


class PauseStopsEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = pathlib.Path(self.tmp.name)
        (self.storage / "state").mkdir(parents=True, exist_ok=True)
        cctv_pipeline._PAUSE_CACHE["at"] = 0.0
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: cctv_pipeline._PAUSE_CACHE.update({"at": 0.0, "value": frozenset()}))

    def write(self, data: dict) -> None:
        (self.storage / "state" / "overrides.json").write_text(json.dumps(data))
        cctv_pipeline._PAUSE_CACHE["at"] = 0.0  # тест не ждёт истечения кэша

    def test_paused_and_retired_cameras_are_muted(self) -> None:
        self.write({"dacha": {"status": "paused"}, "city": {"status": "retired"}})
        muted = cctv_pipeline.paused_cameras(self.storage)
        self.assertEqual(muted, frozenset({"dacha", "city"}))

    def test_rename_alone_does_not_mute(self) -> None:
        """Переименование — не пауза: событие обязано дойти."""
        self.write({"dacha": {"title": "Двор"}})
        self.assertEqual(cctv_pipeline.paused_cameras(self.storage), frozenset())

    def test_missing_or_broken_file_never_mutes(self) -> None:
        """Отказ оверлея не должен глушить видеонаблюдение: молчание опаснее шума."""
        self.assertEqual(cctv_pipeline.paused_cameras(self.storage), frozenset())
        self.write("не json")
        (self.storage / "state" / "overrides.json").write_text("{сломано")
        cctv_pipeline._PAUSE_CACHE["at"] = 0.0
        self.assertEqual(cctv_pipeline.paused_cameras(self.storage), frozenset())


if __name__ == "__main__":
    unittest.main()
