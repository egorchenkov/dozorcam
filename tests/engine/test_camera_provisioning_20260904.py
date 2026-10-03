#!/usr/bin/env python3
"""Заведение камеры из чата: поиск, определение параметров, запись в реестр.

Проверяется ровно то, чем этот путь опасен: пароль не должен появиться нигде,
кроме реестра; писарь не должен пускать в реестр чужие адреса; правка не должна
терять соседние поля записи.
"""
from __future__ import annotations

import json
import pathlib
import socket
import sys
import tempfile
import time
import threading
import unittest
import xml.etree.ElementTree as ET

from cctv.engine import camera_discovery as discovery  # noqa: E402
from cctv.engine import cctv_provision as provision  # noqa: E402

PROFILES_SOAP = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><trt:GetProfilesResponse>
  <trt:Profiles token="Profile_1"><tt:Name>MainStream</tt:Name>
   <tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>
    <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
    <tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit></tt:RateControl>
   </tt:VideoEncoderConfiguration></trt:Profiles>
  <trt:Profiles token="Profile_2"><tt:Name>SubStream</tt:Name>
   <tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>
    <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
    <tt:RateControl><tt:FrameRateLimit>12</tt:FrameRateLimit></tt:RateControl>
   </tt:VideoEncoderConfiguration></trt:Profiles>
 </trt:GetProfilesResponse></s:Body></s:Envelope>"""

SDP = ("v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\ns=Media\r\n"
       "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\n")


class DiscoveryUnits(unittest.TestCase):
    def test_scan_refuses_public_and_oversized_networks(self) -> None:
        """Развёртка сети — не сканер интернета: чужие и огромные сети отвергаются."""
        with self.assertRaises(discovery.DiscoveryError):
            discovery.parse_networks("8.8.8.0/24")
        with self.assertRaises(discovery.DiscoveryError):
            discovery.parse_networks("10.0.0.0/8")
        nets = discovery.parse_networks("192.0.2.0/24, 198.51.100.0/24")
        self.assertEqual(["192.0.2.0/24", "198.51.100.0/24"], [str(n) for n in nets])

    def test_password_never_survives_masking(self) -> None:
        url = "rtsp://admin:s3cr3t@192.0.2.10:554/Streaming/Channels/101"
        self.assertNotIn("s3cr3t", discovery.mask(url))
        self.assertEqual("rtsp://***@192.0.2.10:554/Streaming/Channels/101",
                         discovery.mask(url))

    def test_credentials_are_url_encoded(self) -> None:
        """Пароль со спецсимволами обязан пережить попадание в URL потока."""
        url = discovery.with_credentials("rtsp://192.0.2.10:554/live", "ro meg", "p@ss/w:d")
        self.assertEqual("rtsp://ro%20meg:p%40ss%2Fw%3Ad@192.0.2.10:554/live", url)

    def test_profiles_are_parsed_with_resolution_and_codec(self) -> None:
        profiles = discovery._profiles(ET.fromstring(PROFILES_SOAP))
        self.assertEqual(["Profile_1", "Profile_2"], [p.token for p in profiles])
        self.assertEqual((1920, 1080, "H264", 25),
                         (profiles[0].width, profiles[0].height, profiles[0].encoding,
                          profiles[0].fps))
        self.assertGreater(profiles[0].pixels, profiles[1].pixels)

    def test_stream_uri_is_rehosted_to_the_reachable_address(self) -> None:
        """Камера отдаёт свой внутренний адрес — доверяем тому, по которому дошли."""
        self.assertEqual("rtsp://192.0.2.10:554/live",
                         discovery._rehost("rtsp://203.0.113.5:554/live", "192.0.2.10"))

    def test_slug_makes_camera_id_from_russian_title(self) -> None:
        self.assertEqual("gorod", discovery.slugify("Город"))
        self.assertEqual("dvor-u-vorot", discovery.slugify("Двор у ворот"))
        self.assertEqual("", discovery.slugify("   "))


class RtspProbe(unittest.TestCase):
    """DESCRIBE с Digest: без него проверка потока молча считала бы 401 успехом."""

    def setUp(self) -> None:
        self.requests: list[bytes] = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.close)

    def _serve(self) -> None:
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            self.requests.append(conn.recv(4096))
            conn.sendall(b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest '
                         b'realm="cam", nonce="abc", qop="auth"\r\n\r\n')
            self.requests.append(conn.recv(4096))
            conn.sendall(("RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Type: application/sdp\r\n"
                          f"Content-Length: {len(SDP)}\r\n\r\n{SDP}").encode())

    def test_digest_handshake_and_sdp(self) -> None:
        url = f"rtsp://admin:s3cr3t@127.0.0.1:{self.port}/Streaming/Channels/101"
        code, sdp = discovery.rtsp_describe(url, timeout=3)
        self.assertEqual(200, code)
        self.assertEqual("H265", discovery.codec_of(sdp))
        self.assertIn(b"Authorization: Digest", self.requests[1])
        for chunk in self.requests:
            self.assertNotIn(b"s3cr3t", chunk)  # пароль по проводу не уходит


class ProvisionRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.path = self.tmp / "cameras.json"
        self.backups = self.tmp / "backups"
        self.path.write_text(json.dumps({"cameras": [{
            "camera_id": "city", "title": "Город", "site": "Город",
            "rtsp_url": "rtsp://admin:old@192.0.2.10:554/Streaming/Channels/101",
            "person_detection": True, "motion_threshold": 1.0}]}))

    def apply(self, command: dict):
        return provision.apply(command, path=self.path, backups=self.backups)

    def cameras(self) -> list[dict]:
        return json.loads(self.path.read_text())["cameras"]

    def test_registry_refuses_addresses_outside_the_private_network(self) -> None:
        """Кнопка в чате не должна уметь направить мост на чужой хост в интернете."""
        with self.assertRaises(provision.Invalid):
            provision.validate({"camera_id": "evil",
                                "rtsp_url": "rtsp://u:p@8.8.8.8:554/live"})
        with self.assertRaises(provision.Invalid):
            provision.validate({"camera_id": "evil", "rtsp_url": "http://192.0.2.10/x"})
        with self.assertRaises(provision.Invalid):
            provision.validate({"camera_id": "с кириллицей",
                                "rtsp_url": "rtsp://192.0.2.10:554/live"})

    def test_snapshot_and_detect_stream_must_belong_to_the_same_camera(self) -> None:
        with self.assertRaises(provision.Invalid):
            provision.validate({"camera_id": "cam", "rtsp_url": "rtsp://192.0.2.10:554/a",
                                "detect_rtsp_url": "rtsp://192.0.2.11:554/b"})
        with self.assertRaises(provision.Invalid):
            provision.validate({"camera_id": "cam", "rtsp_url": "rtsp://192.0.2.10:554/a",
                                "snapshot_url": "http://192.0.2.11/snap"})

    def test_unknown_fields_do_not_reach_the_registry(self) -> None:
        entry = provision.validate({"camera_id": "cam", "rtsp_url": "rtsp://192.0.2.10:554/a",
                                    "command": "rm -rf /", "extra": {"x": 1}})
        self.assertEqual({"camera_id", "title", "site", "rtsp_url"}, set(entry))

    def test_add_edit_and_delete_round_trip(self) -> None:
        reply, restart = self.apply({"command": "upsert", "camera": {
            "camera_id": "dvor", "title": "Двор", "site": "Двор",
            "rtsp_url": "rtsp://admin:new@192.0.2.11:554/live",
            "detect_rtsp_url": "rtsp://admin:new@192.0.2.11:554/sub"}})
        self.assertEqual({"ok": True, "action": "added", "camera_id": "dvor"}, reply)
        self.assertTrue(restart)
        self.assertEqual(2, len(self.cameras()))

        # Смена пароля не должна ронять калибровку и детекцию соседних полей.
        self.apply({"command": "upsert", "camera": {
            "camera_id": "city",
            "rtsp_url": "rtsp://admin:fresh@192.0.2.10:554/Streaming/Channels/101"}})
        kept = next(c for c in self.cameras() if c["camera_id"] == "city")
        self.assertEqual("rtsp://admin:fresh@192.0.2.10:554/Streaming/Channels/101",
                         kept["rtsp_url"])
        self.assertTrue(kept["person_detection"])
        self.assertEqual(1.0, kept["motion_threshold"])

        reply, restart = self.apply({"command": "delete", "camera_id": "dvor"})
        self.assertEqual("deleted", reply["action"])
        self.assertEqual(["city"], [c["camera_id"] for c in self.cameras()])
        missing, restart = self.apply({"command": "delete", "camera_id": "dvor"})
        self.assertEqual({"ok": False, "error": "not_found"}, missing)
        self.assertFalse(restart)

    def test_every_change_leaves_a_backup(self) -> None:
        self.apply({"command": "upsert", "camera": {
            "camera_id": "dvor", "rtsp_url": "rtsp://192.0.2.11:554/live"}})
        self.assertEqual(1, len(list(self.backups.glob("cameras.*.json"))))

    def test_listing_never_returns_passwords(self) -> None:
        reply, _ = self.apply({"command": "list"})
        text = json.dumps(reply, ensure_ascii=False)
        self.assertNotIn("old", json.loads(text)["cameras"][0]["rtsp_url"])
        self.assertEqual("rtsp://***@192.0.2.10:554/Streaming/Channels/101",
                         reply["cameras"][0]["rtsp_url"])
        self.assertEqual("admin", reply["cameras"][0]["username"])
        self.assertEqual("192.0.2.10", reply["cameras"][0]["host"])

    def test_socket_round_trip_answers_before_restart(self) -> None:
        """Мост перезапускает сам себя: ответ обязан уйти раньше перезапуска."""
        restarts: list[str] = []
        original_peer, original_restart = provision.peer_allowed, provision.restart_chain
        provision.peer_allowed = lambda conn: True
        provision.restart_chain = lambda **kw: restarts.append("restart")
        original_config, original_backups = provision.CONFIG_PATH, provision.BACKUP_DIR
        provision.CONFIG_PATH, provision.BACKUP_DIR = self.path, self.backups
        sock_path = str(self.tmp / "provision.sock")
        thread = threading.Thread(target=provision.serve, args=(sock_path, lambda m: None),
                                  daemon=True)
        thread.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            for attempt in range(100):  # сокет появляется в файловой системе до listen()
                try:
                    client.connect(sock_path)
                    break
                except OSError:
                    threading.Event().wait(0.05)
            else:
                self.fail("писарь не начал слушать сокет")
            client.sendall(json.dumps({"command": "upsert", "camera": {
                "camera_id": "dvor", "title": "Двор",
                "rtsp_url": "rtsp://admin:new@192.0.2.11:554/live"}}).encode() + b"\n")
            reply = json.loads(client.recv(65536).decode().split("\n")[0])
            client.close()
        finally:
            provision.peer_allowed, provision.restart_chain = original_peer, original_restart
            provision.CONFIG_PATH, provision.BACKUP_DIR = original_config, original_backups
        self.assertTrue(reply["ok"])
        self.assertIn("dvor", [c["camera_id"] for c in self.cameras()])

    def test_restart_never_blocks_the_answer(self) -> None:
        """Мост перезапускает сам себя: рестарт уходит в фон и с задержкой."""
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return type("Done", (), {"returncode": 0})()

        provision.restart_chain(("cctv-bridge.service",), runner=runner, delay=0.2,
                                log=lambda m: None)
        self.assertEqual([], calls)  # вызов вернулся до перезапуска
        for _ in range(50):
            if calls:
                break
            threading.Event().wait(0.05)
        self.assertEqual([["/usr/bin/systemctl", "restart", "cctv-bridge.service"]], calls)


class BridgeProvisioningApi(unittest.TestCase):
    """Мост между чатом и писарем: токен вместо пароля и никаких чужих записей."""

    def setUp(self) -> None:
        from cctv.engine import cctv_bridge

        self.module = cctv_bridge
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        config = {"cameras": [{"camera_id": "city", "title": "Город",
                               "site": "Город",
                               "rtsp_url": "rtsp://admin:old@192.0.2.10:554/live"}]}
        self.bridge = cctv_bridge.Bridge(config, self.tmp, "https://cctv-bridge")
        self.sent: list[dict] = []
        self.bridge.provision = lambda command: (self.sent.append(command)
                                                 or {"ok": True, "action": "added"})
        self.detected = discovery.Detected(
            host="192.0.2.11", vendor="Hikvision", model="DS-2CD",
            main_url="rtsp://admin:s3cr3t@192.0.2.11:554/Streaming/Channels/101",
            sub_url="rtsp://admin:s3cr3t@192.0.2.11:554/Streaming/Channels/102",
            snapshot_url="http://192.0.2.11/ISAPI/Streaming/channels/101/picture",
            verified=True)
        self.original_probe = discovery.probe
        discovery.probe = lambda host, user, password, **kw: self.detected
        self.addCleanup(setattr, discovery, "probe", self.original_probe)

    def test_probe_returns_a_token_and_no_password(self) -> None:
        result = self.bridge.probe("192.0.2.11", "admin", "s3cr3t")
        self.assertTrue(result["ok"])
        self.assertNotIn("s3cr3t", json.dumps(result, ensure_ascii=False))
        self.assertEqual("rtsp://***@192.0.2.11:554/Streaming/Channels/101",
                         result["summary"]["main_url"])

        added = self.bridge.add_camera({"camera_id": "dvor", "title": "Двор",
                                        "probe_token": result["probe_token"]})
        self.assertTrue(added["ok"])
        camera = self.sent[0]["camera"]
        # Пароль появляется ровно один раз и только на пути к писарю реестра.
        self.assertEqual("rtsp://admin:s3cr3t@192.0.2.11:554/Streaming/Channels/101",
                         camera["rtsp_url"])
        self.assertEqual("rtsp://admin:s3cr3t@192.0.2.11:554/Streaming/Channels/102",
                         camera["detect_rtsp_url"])
        self.assertEqual("admin", camera["snapshot_user"])
        self.assertNotIn("person_detection", camera)  # новая камера начинает без YOLO

    def test_probe_token_is_single_use_and_scoped(self) -> None:
        token = self.bridge.probe("192.0.2.11", "admin", "s3cr3t")["probe_token"]
        self.bridge.add_camera({"camera_id": "dvor", "title": "Двор", "probe_token": token})
        with self.assertRaises(self.module.BridgeError):
            self.bridge.add_camera({"camera_id": "third", "title": "Третья",
                                    "probe_token": "выдуманный"})

    def test_duplicate_and_unknown_cameras_are_refused(self) -> None:
        token = self.bridge.probe("192.0.2.11", "admin", "s3cr3t")["probe_token"]
        clash = self.bridge.add_camera({"camera_id": "city", "title": "Ещё раз",
                                        "probe_token": token})
        self.assertFalse(clash["ok"])
        with self.assertRaises(self.module.BridgeError):
            self.bridge.delete_camera("нет-такой")

    def test_turning_person_detection_off_replaces_the_whole_entry(self) -> None:
        """Писарь сливает записи, поэтому снятие флага идёт явной заменой."""
        self.bridge.update_camera("city", {"person_detection": False})
        command = self.sent[-1]
        self.assertTrue(command["replace"])
        self.assertNotIn("person_detection", command["camera"])
        self.assertEqual("rtsp://admin:old@192.0.2.10:554/live", command["camera"]["rtsp_url"])

    def test_scan_rejects_networks_outside_the_private_range(self) -> None:
        result = self.bridge.start_scan("8.8.8.0/24")
        self.assertFalse(result["ok"])
        self.assertIn("не приватная", result["error"])

    def test_scan_marks_candidates_already_in_the_registry(self) -> None:
        """Кандидат на адресе заведённой камеры — это она сама, а не новая камера.

        Без метки бот показывал бы её кнопкой «добавить», и повторный поиск
        по той же сети каждый раз предлагал бы завести дубль (баг 04.09:
        автопоиск нашёл городскую камеру 192.0.2.10).
        """
        self.bridge._scans["s1"] = {
            "status": "done", "started": time.time(), "finished": time.time(),
            "networks": ["192.0.2.0/24"],
            "candidates": [{"host": "192.0.2.10", "ports": [80, 554]},
                           {"host": "192.0.2.11", "ports": [554]}]}
        result = self.bridge.scan_status("s1")
        marks = {c["host"]: c["registered_camera_id"] for c in result["candidates"]}
        self.assertEqual("city", marks["192.0.2.10"])
        self.assertEqual("", marks["192.0.2.11"])


if __name__ == "__main__":
    unittest.main()
