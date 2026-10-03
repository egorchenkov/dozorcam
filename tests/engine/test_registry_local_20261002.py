"""Э4: реестр камер пишет мост сам (контейнер — без root-сокета писаря).

CCTV_REGISTRY_FILE включает запись в файл state теми же правилами писаря;
cameras.json каталога конфига — только исходник. Плюс заведение камеры по
адресу потока (камера вне поиска) и порог «мало места» от движка.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import tempfile
import threading
import unittest
from unittest import mock

from cctv.engine import camera_discovery as discovery

SDP = "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


class LocalRegistryWriter(unittest.TestCase):
    def setUp(self) -> None:
        from cctv.engine import cctv_bridge

        self.module = cctv_bridge
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.seed = self.tmp / "config" / "cameras.json"
        self.seed.parent.mkdir()
        self.registry = self.tmp / "state" / "cameras.json"
        env = {"CCTV_REGISTRY_FILE": str(self.registry), "CCTV_REGISTRY_SEED": str(self.seed),
               "CCTV_STATE_DIR": str(self.tmp / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.detected = discovery.Detected(
            host="127.0.0.1", source="manual",
            main_url="rtsp://stand:pw@127.0.0.1:28680/replay",
            sub_url="rtsp://stand:pw@127.0.0.1:28680/replay-detect", verified=True)
        original = discovery.probe
        discovery.probe = lambda host, user, password, **kw: self.detected
        self.addCleanup(setattr, discovery, "probe", original)

    def bridge(self, cameras=()):
        return self.module.Bridge({"cameras": list(cameras)}, self.tmp / "spool", "http://127.0.0.1:1")

    def test_first_camera_from_empty_config_lands_in_state(self) -> None:
        bridge = self.bridge()
        self.assertEqual({"ok": True, "cameras": []}, bridge.registry_config())
        self.assertFalse(self.registry.exists())  # чтение не создаёт файл и не дёргает рестарт
        token = bridge.probe("rtsp://127.0.0.1:28680/replay", "stand", "pw",
                             "rtsp://127.0.0.1:28680/replay-detect")["probe_token"]
        added = bridge.add_camera({"camera_id": "replay", "title": "Стенд", "probe_token": token})
        self.assertEqual({"ok": True, "action": "added", "camera_id": "replay"}, added)
        camera = json.loads(self.registry.read_text())["cameras"][0]
        self.assertEqual("rtsp://stand:pw@127.0.0.1:28680/replay", camera["rtsp_url"])
        self.assertEqual("rtsp://stand:pw@127.0.0.1:28680/replay-detect", camera["detect_rtsp_url"])
        self.assertEqual(0o640, self.registry.stat().st_mode & 0o777)
        listed = bridge.registry_config()["cameras"]
        self.assertNotIn("pw", json.dumps(listed))  # список — без паролей

    def test_seed_from_config_dir_is_kept_on_first_write(self) -> None:
        self.seed.write_text(json.dumps({"cameras": [{
            "camera_id": "old", "title": "Old", "rtsp_url": "rtsp://u:p@192.0.2.10:554/a"}]}))
        bridge = self.bridge()
        self.assertEqual(["old"], [c["camera_id"] for c in bridge.registry_config()["cameras"]])
        token = bridge.probe("rtsp://127.0.0.1:28680/replay", "stand", "pw")["probe_token"]
        bridge.add_camera({"camera_id": "replay", "title": "Стенд", "probe_token": token})
        ids = [c["camera_id"] for c in json.loads(self.registry.read_text())["cameras"]]
        self.assertEqual(["old", "replay"], ids)
        self.assertEqual("old", json.loads(self.seed.read_text())["cameras"][0]["camera_id"])

    def test_invalid_entry_is_refused_with_a_reason(self) -> None:
        self.detected.main_url = "rtsp://u:p@8.8.8.8:554/live"
        bridge = self.bridge()
        token = bridge.probe("rtsp://8.8.8.8/live", "u", "p")["probe_token"]
        reply = bridge.add_camera({"camera_id": "x", "title": "X", "probe_token": token})
        self.assertFalse(reply["ok"])
        self.assertIn("приватной", reply["error"])
        self.assertFalse(self.registry.exists())

    def test_storage_reports_engine_free_space_threshold(self) -> None:
        health = self.bridge().storage_health()
        self.assertEqual(self.module.MIN_FREE_BYTES, health["min_free_bytes"])


class ManualStreamProbe(unittest.TestCase):
    """Камера вне поиска: адрес потока целиком, Digest как у камеры."""

    def serve(self, codes: list[int]) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.addCleanup(server.close)

        def loop():
            for code in codes:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(4096)
                    conn.sendall(b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest '
                                 b'realm="cam", nonce="abc"\r\n\r\n')
                    conn.recv(4096)
                    if code == 200:
                        conn.sendall(("RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Type: application/sdp\r\n"
                                      f"Content-Length: {len(SDP)}\r\n\r\n{SDP}").encode())
                    else:
                        conn.sendall(f"RTSP/1.0 {code} X\r\nCSeq: 2\r\n\r\n".encode())

        threading.Thread(target=loop, daemon=True).start()
        return server.getsockname()[1]

    def test_url_with_detector_stream(self) -> None:
        port = self.serve([200, 200])
        found = discovery.probe(f"rtsp://127.0.0.1:{port}/replay", "stand", "p@ss",
                                detect_url=f"rtsp://127.0.0.1:{port}/replay-detect", timeout=3)
        self.assertTrue(found.verified)
        self.assertEqual(f"rtsp://stand:p%40ss@127.0.0.1:{port}/replay", found.main_url)
        self.assertTrue(found.sub_url.endswith("/replay-detect"))
        self.assertEqual("rtsp://***@127.0.0.1:%d/replay" % port, found.summary()["main_url"])

    def test_wrong_password_is_reported(self) -> None:
        port = self.serve([401])
        with self.assertRaises(discovery.DiscoveryError):
            discovery.probe(f"rtsp://127.0.0.1:{port}/replay", "stand", "bad", timeout=3)

    def test_detector_stream_of_another_host_is_refused(self) -> None:
        with self.assertRaises(discovery.DiscoveryError):
            discovery.probe_url("rtsp://127.0.0.1:1/a", "u", "p", detect_url="rtsp://10.0.0.1/b",
                                timeout=0.2)

    def test_name_instead_of_ip_is_refused(self) -> None:
        with self.assertRaises(discovery.DiscoveryError):
            discovery.probe("camera.local", "u", "p", timeout=0.2)


class AutoNetworks(unittest.TestCase):
    def test_local_networks_are_private_slash_24(self) -> None:
        import ipaddress

        for network in discovery.local_networks():
            parsed = ipaddress.ip_network(network)
            self.assertTrue(parsed.is_private)
            self.assertEqual(24, parsed.prefixlen)

    def test_ws_discovery_parses_xaddrs(self) -> None:
        reply = (b'<d:XAddrs>http://192.0.2.64/onvif/device_service '
                 b'http://8.8.8.8:80/onvif/device_service</d:XAddrs>')

        class FakeSocket:
            def __init__(self, *a): self.sent = 0
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def setsockopt(self, *a): pass
            def settimeout(self, *a): pass
            def sendto(self, data, addr): self.sent += 1
            def recvfrom(self, size):
                if self.sent == 1:
                    self.sent = 2
                    return reply, ("192.0.2.64", 3702)
                raise socket.timeout()

        with mock.patch.object(discovery.socket, "socket", FakeSocket):
            self.assertEqual(["192.0.2.64"], discovery.ws_discover(timeout=0.3))


if __name__ == "__main__":
    unittest.main()
