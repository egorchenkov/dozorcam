"""Правка записи реестра из чата меняет только своё поле (аудит 09.10.2026, Б-13, Б-14).

Б-13: «Детекция людей: выключить» собирала запись из рабочих адресов моста —
адресов RTSP-прокси без учётки — и писала её заменой целиком: прокси падал,
вставал весь движок. Б-14: validate() не знал `camera_human_events` и
`detect_substream`, и любая правка (пароль, включение детекции) снимала ONVIF-гейт
и substream. Проверяется так, как это устроено в контейнере: мост видит адреса
прокси, а реестр на диске — настоящие адреса с учёткой.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cctv.engine import camera_discovery as discovery
from cctv.engine import cctv_provision as provision

# Как боевая запись камеры с ONVIF-гейтом: порядок ключей тот же, адреса и пароли — свои.
GATE_CAMERA = {
    "camera_id": "door", "title": "Дверь", "site": "Дача",
    "rtsp_url": "rtsp://admin:old%40pw@192.0.2.20:554/Streaming/Channels/101",
    "detect_rtsp_url": "rtsp://admin:old%40pw@192.0.2.20:554/Streaming/Channels/102",
    "snapshot_url": "http://192.0.2.20/ISAPI/Streaming/channels/101/picture",
    "snapshot_user": "admin", "snapshot_password": "old@pw",
    "motion_threshold": 0.4, "person_detection": True,
    "detect_substream": True, "camera_human_events": True,
}
NEIGHBOUR = {
    "camera_id": "yard", "title": "Двор", "site": "Дача",
    "rtsp_url": "rtsp://admin:yard@192.0.2.21:554/live",
    "snapshot_url": "http://192.0.2.21/snap.jpg", "snapshot_user": "admin",
    "snapshot_password": "yard", "detect_rtsp_url": "rtsp://admin:yard@192.0.2.21:554/live",
    "person_detection": True, "snapshot_aspect": 1.7778,
}


def proxied(cameras: list[dict]) -> dict:
    """Что видит мост: реестр после rtsp_credential_proxy (адреса без учётки)."""
    result = []
    for index, camera in enumerate(cameras):
        camera = dict(camera, rtsp_url=f"rtsp://127.0.0.1:{28554 + 2 * index}/{camera['camera_id']}")
        if camera.get("detect_rtsp_url"):
            camera["detect_rtsp_url"] = f"rtsp://127.0.0.1:{28555 + 2 * index}/{camera['camera_id']}-detect"
        result.append(camera)
    return {"cameras": result}


def lines_changed(before: str, after: str) -> list[tuple[str, str]]:
    old, new = before.splitlines(), after.splitlines()
    return [(a, b) for a, b in zip(old, new) if a != b] + [("", b) for b in new[len(old):]] \
        + [(a, "") for a in old[len(new):]]


class RegistryPatchThroughBridge(unittest.TestCase):
    def setUp(self) -> None:
        from cctv.engine import cctv_bridge

        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.registry = self.tmp / "state" / "cameras.json"
        self.registry.parent.mkdir()
        provision.save({"cameras": [dict(GATE_CAMERA), dict(NEIGHBOUR)]}, self.registry,
                       self.tmp / "backups")
        self.original = self.registry.read_text()
        env = {"CCTV_REGISTRY_FILE": str(self.registry), "CCTV_REGISTRY_SEED": "/nonexistent",
               "CCTV_STATE_DIR": str(self.tmp / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = cctv_bridge.Bridge(proxied([GATE_CAMERA, NEIGHBOUR]), self.tmp / "spool",
                                         "http://127.0.0.1:1")
        self.detected = discovery.Detected(
            host="192.0.2.20", vendor="Hikvision", model="DS-2CD2543G2-IS",
            main_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/101",
            sub_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/102",
            snapshot_url="http://192.0.2.20/ISAPI/Streaming/channels/101/picture", verified=True)
        original_probe = discovery.probe
        discovery.probe = lambda host, user, password, **kw: self.detected
        self.addCleanup(setattr, discovery, "probe", original_probe)

    def record(self, camera_id: str = "door") -> dict:
        return next(c for c in json.loads(self.registry.read_text())["cameras"]
                    if c["camera_id"] == camera_id)

    def test_detection_off_removes_only_the_flag(self) -> None:
        reply = self.bridge.update_camera("door", {"person_detection": False})
        self.assertEqual({"ok": True, "action": "updated", "camera_id": "door"}, reply)
        expected = {k: v for k, v in GATE_CAMERA.items() if k != "person_detection"}
        self.assertEqual(expected, self.record())
        self.assertEqual(list(expected), list(self.record()))  # порядок ключей тот же
        self.assertEqual(NEIGHBOUR, self.record("yard"))
        changed = lines_changed(self.original, self.registry.read_text())
        self.assertEqual(('      "person_detection": true,', '      "detect_substream": true,'), changed[0])
        self.assertEqual(len(self.original.splitlines()) - 1, len(self.registry.read_text().splitlines()))
        self.assertNotIn("127.0.0.1", self.registry.read_text())

    def test_detection_off_and_on_restores_the_file_byte_for_byte(self) -> None:
        self.bridge.update_camera("door", {"person_detection": False})
        self.bridge.update_camera("door", {"person_detection": True})
        self.assertEqual(self.original, self.registry.read_text())

    def test_detection_on_is_a_partial_update(self) -> None:
        """Включение шло писарю записью из одного флага, и писарь отказывал (bad_scheme)."""
        self.bridge.update_camera("yard", {"person_detection": False})
        reply = self.bridge.update_camera("yard", {"person_detection": True})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(NEIGHBOUR, self.record("yard"))

    def test_password_change_keeps_paths_and_flags(self) -> None:
        token = self.bridge.probe("192.0.2.20", "admin", "n:w")["probe_token"]
        self.detected.sub_url = "rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/101"
        reply = self.bridge.update_camera("door", {"probe_token": token})
        self.assertTrue(reply["ok"], reply)
        expected = dict(GATE_CAMERA,
                        rtsp_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/101",
                        detect_rtsp_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/102",
                        snapshot_password="n:w")
        self.assertEqual(expected, self.record())
        self.assertEqual(list(GATE_CAMERA), list(self.record()))
        self.assertEqual(NEIGHBOUR, self.record("yard"))

    def test_password_change_of_a_moved_camera_takes_the_new_address(self) -> None:
        self.detected.main_url = "rtsp://admin:n@192.0.2.30:554/live"
        self.detected.sub_url = "rtsp://admin:n@192.0.2.30:554/sub"
        self.detected.snapshot_url = "http://192.0.2.30/snap.jpg"
        token = self.bridge.probe("192.0.2.30", "admin", "n")["probe_token"]
        self.assertTrue(self.bridge.update_camera("door", {"probe_token": token})["ok"])
        record = self.record()
        self.assertEqual("rtsp://admin:n@192.0.2.30:554/live", record["rtsp_url"])
        self.assertEqual("http://192.0.2.30/snap.jpg", record["snapshot_url"])
        self.assertTrue(record["detect_substream"] and record["camera_human_events"])

    def test_password_change_by_stream_address_renews_the_snapshot_password(self) -> None:
        """Проба по адресу потока (камера вне поиска) снимка не приносит: снимок с той же
        учёткой получает новый пароль вместе с потоком, иначе кадры шли бы со старым."""
        self.detected = discovery.Detected(
            host="192.0.2.20", source="manual", verified=True,
            main_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/101",
            sub_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/102")
        token = self.bridge.probe("rtsp://192.0.2.20:554/Streaming/Channels/101", "admin", "n:w",
                                  "rtsp://192.0.2.20:554/Streaming/Channels/102")["probe_token"]
        self.assertTrue(self.bridge.update_camera("door", {"probe_token": token})["ok"])
        self.assertEqual(dict(GATE_CAMERA,
                              rtsp_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/101",
                              detect_rtsp_url="rtsp://admin:n%3Aw@192.0.2.20:554/Streaming/Channels/102",
                              snapshot_password="n:w"), self.record())

    def test_threshold_and_title_change_only_their_fields(self) -> None:
        self.bridge.update_camera("door", {"motion_threshold": 0.7, "title": "Дверь-2"})
        self.assertEqual(dict(GATE_CAMERA, motion_threshold=0.7, title="Дверь-2"), self.record())

    def test_same_value_is_not_a_change(self) -> None:
        reply = self.bridge.update_camera("door", {"person_detection": True})
        self.assertEqual("unchanged", reply["action"])
        self.assertEqual(self.original, self.registry.read_text())
        self.assertEqual([], list((self.tmp / "state").glob("registry-backups/*")))


class WriterKnowsTheRegistry(unittest.TestCase):
    def test_validate_keeps_every_field_the_engine_reads(self) -> None:
        from cctv.engine import cctv_bridge

        engine_fields = {f.name for f in dataclasses.fields(cctv_bridge.Camera)}
        self.assertEqual(engine_fields, set(provision.FIELDS))
        full = dict(GATE_CAMERA, person_gate_threshold=0.2, snapshot_aspect=1.5)
        self.assertEqual(engine_fields, set(provision.validate(full)))

    def test_flags_survive_validate(self) -> None:
        checked = provision.validate(GATE_CAMERA)
        self.assertIs(True, checked["camera_human_events"])
        self.assertIs(True, checked["detect_substream"])

    def test_proxy_address_without_credentials_is_refused(self) -> None:
        """Даже старый мост (replace с адресом прокси) не положит движок: запись отвергается."""
        tmp = pathlib.Path(tempfile.mkdtemp())
        path = tmp / "cameras.json"
        provision.save({"cameras": [dict(GATE_CAMERA)]}, path, tmp / "b")
        before = path.read_text()
        old_bridge = proxied([GATE_CAMERA])["cameras"][0]  # что слал 0.3.0 при выключении
        old_bridge.pop("person_detection")
        for camera in (old_bridge, dict(GATE_CAMERA, rtsp_url="rtsp://127.0.0.1:28554/door")):
            with self.assertRaises(provision.Invalid) as caught:
                provision.apply({"command": "upsert", "replace": True, "camera": camera},
                                path=path, backups=tmp / "b")
            self.assertEqual("registry.no_credentials", caught.exception.reply()["error_key"])
        self.assertEqual(before, path.read_text())

    def test_unset_removes_a_key_explicitly(self) -> None:
        tmp = pathlib.Path(tempfile.mkdtemp())
        path = tmp / "cameras.json"
        provision.save({"cameras": [dict(GATE_CAMERA)]}, path, tmp / "b")
        reply, restart = provision.apply({"command": "upsert", "camera": {"camera_id": "door"},
                                          "unset": ["motion_threshold"]}, path=path, backups=tmp / "b")
        self.assertTrue(reply["ok"] and restart)
        record = json.loads(path.read_text())["cameras"][0]
        self.assertEqual({k: v for k, v in GATE_CAMERA.items() if k != "motion_threshold"}, record)


if __name__ == "__main__":
    unittest.main()
