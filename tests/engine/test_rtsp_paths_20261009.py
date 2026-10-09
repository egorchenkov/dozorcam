#!/usr/bin/env python3
"""Камера по IP + логин/пароль без знания пути RTSP (0.3.0, состав 0.2.1, план 2.3).

Мост перебирает типовые пути марок (Reolink, TP-Link Tapo/VIGI, Uniview, Axis,
Xiongmai/XMEye, generic) по очереди и берёт первый живой; бот показывает, какой
сработал. XMEye — особый случай: логин и пароль в самом пути. Человек такой
адрес прислать не может (пароль — только отдельным удаляемым сообщением), путь
собирает мост, и наружу он уходит с затёртым паролем.

Камера — настоящий TCP-сервер RTSP на loopback: отвечает 200 на свой путь и
404 (или 401) на чужие, как это делают камеры.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import unittest
from unittest import mock

from cctv.engine import camera_discovery as discovery
from cctv.engine import cctv_provision as provision

SDP = "v=0\r\ns=Media\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


class FakeCamera:
    """RTSP-камера: DESCRIBE на `live` — 200 с SDP, на прочие пути — `other` (404/401)."""

    def __init__(self, live: set[str], other: int = 404) -> None:
        self.live, self.other = live, other
        self.paths: list[str] = []
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.server.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(3)
            try:
                while True:
                    data = conn.recv(8192)
                    if not data:
                        return
                    match = re.match(rb"DESCRIBE rtsp://[^/]+(/\S*) RTSP/1\.0", data)
                    path = match.group(1).decode() if match else ""
                    self.paths.append(path)
                    if path in self.live:
                        conn.sendall(("RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n"
                                      f"Content-Length: {len(SDP)}\r\n\r\n{SDP}").encode())
                    elif self.other == 401:
                        conn.sendall(b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n'
                                     b'WWW-Authenticate: Basic realm="cam"\r\n\r\n')
                    else:
                        conn.sendall(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n")
            except OSError:
                return


class RtspPathProbeTest(unittest.TestCase):
    def camera(self, *live: str, other: int = 404) -> FakeCamera:
        cam = FakeCamera(set(live), other)
        self.addCleanup(cam.close)
        patcher = mock.patch.object(discovery, "RTSP_PORT", cam.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cam

    def probe(self, vendor: str = "", user: str = "admin", password: str = "s3cr3t!x"):
        detected = discovery.Detected(host="127.0.0.1", vendor=vendor)
        discovery._probe_templates(detected, user, password, 3.0)
        return detected

    def test_every_vendor_path_is_found_and_its_substream_offered(self) -> None:
        cases = {
            "reolink": ("/h264Preview_01_main", "/h264Preview_01_sub"),
            "tapo": ("/stream1", "/stream2"),
            "uniview": ("/media/video1", "/media/video2"),
            "axis": ("/axis-media/media.amp", "/axis-media/media.amp?resolution=640x360"),
            "generic-11": ("/11", "/12"),
            "generic-ch00": ("/live/ch00_0", "/live/ch00_1"),
            "generic-videomain": ("/videoMain", "/videoSub"),
            "generic-onvif1": ("/onvif1", "/onvif2"),
            "generic-live": ("/live", ""),
        }
        for name, (main, sub) in cases.items():
            with self.subTest(name):
                self.camera(*(p for p in (main, sub) if p))
                detected = self.probe()
                self.assertEqual(name, detected.template)
                self.assertEqual("template", detected.source)
                self.assertTrue(detected.main_url.endswith(main), detected.main_url)
                self.assertTrue(detected.main_url.startswith("rtsp://admin:s3cr3t%21x@127.0.0.1:"))
                if sub:
                    self.assertTrue(detected.sub_url.endswith(sub), detected.sub_url)
                else:
                    self.assertEqual("", detected.sub_url)
                summary = detected.summary()
                self.assertEqual(main, summary["path"])
                self.assertEqual(discovery.TEMPLATES[name]["label"], summary["template"])
                self.assertNotIn("s3cr3t", str(summary))

    def test_substream_is_dropped_when_it_does_not_answer(self) -> None:
        self.camera("/h264Preview_01_main")
        detected = self.probe()
        self.assertEqual("reolink", detected.template)
        self.assertEqual("", detected.sub_url)

    def test_xmeye_carries_login_in_the_path_and_masks_it(self) -> None:
        main = "/user=admin&password=s3cr3t%21x&channel=1&stream=0.sdp"
        sub = "/user=admin&password=s3cr3t%21x&channel=1&stream=1.sdp"
        cam = self.camera(main, sub)
        detected = self.probe()
        self.assertEqual("xmeye", detected.template)
        self.assertTrue(detected.main_url.endswith(main))
        self.assertTrue(detected.sub_url.endswith(sub))
        # userinfo тоже есть: без него loopback-прокси движка не поднимет поток.
        self.assertTrue(detected.main_url.startswith("rtsp://admin:s3cr3t%21x@"))
        self.assertIn(main, cam.paths)
        # Наружу — ни в адресе, ни в пути пароля нет; путь показывается затёртым.
        summary = detected.summary()
        self.assertNotIn("s3cr3t", str(summary))
        self.assertEqual("/user=admin&password=***&channel=1&stream=0.sdp", summary["path"])
        self.assertEqual("Xiongmai/XMEye", summary["template"])
        self.assertNotIn("s3cr3t", provision.mask(detected.main_url))
        self.assertNotIn("s3cr3t", discovery.mask(detected.sub_url))

    def test_vendor_hint_goes_first(self) -> None:
        cam = self.camera("/stream1", "/h264Preview_01_main")
        detected = self.probe(vendor="TP-LINK")
        self.assertEqual("tapo", detected.template)
        self.assertEqual("/stream1", cam.paths[0])
        self.assertEqual("TP-LINK", detected.vendor)  # узнанное имя не переписываем
        cam2 = self.camera("/user=admin&password=s3cr3t%21x&channel=1&stream=0.sdp")
        detected = self.probe(vendor="NetSurveillance")
        self.assertEqual("xmeye", detected.template)
        self.assertEqual("xmeye", discovery.template_order("H264DVR 1.0")[0])
        self.assertTrue(cam2.paths[0].startswith("/user="))

    def test_order_brands_first_generic_last(self) -> None:
        order = discovery.template_order("")
        self.assertEqual(["hikvision", "dahua", "qualvision", "reolink", "tapo", "uniview", "axis",
                          "xmeye"], order[:8])
        self.assertTrue(all(name.startswith("generic") for name in order[8:]))
        self.assertEqual(len(discovery.TEMPLATES), len(order))
        self.assertEqual("uniview", discovery.template_order("Uniview IPC")[0])
        self.assertEqual("axis", discovery.template_order("AXIS")[0])

    def test_unknown_vendor_takes_its_label(self) -> None:
        self.camera("/media/video1")
        self.assertEqual("Uniview", self.probe().vendor)
        self.camera("/live/ch00_0")
        self.assertEqual("", self.probe().vendor)  # generic — марку не выдумываем

    def test_wrong_password_everywhere_is_auth_failed_not_no_stream(self) -> None:
        self.camera(other=401)
        with self.assertRaises(discovery.DiscoveryError) as caught:
            self.probe()
        self.assertEqual("auth_failed", caught.exception.code)

    def test_nothing_found_leaves_no_stream_for_the_caller(self) -> None:
        cam = self.camera()
        detected = self.probe()
        self.assertEqual("", detected.main_url)
        self.assertEqual(len(discovery.TEMPLATES), len(cam.paths))  # перебраны все пути

    def test_closed_rtsp_port_skips_the_whole_table_fast(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # порт свободен и закрыт
        with mock.patch.object(discovery, "RTSP_PORT", port):
            started = time.monotonic()
            detected = self.probe()
        self.assertEqual("", detected.main_url)
        self.assertLess(time.monotonic() - started, 2)

    def test_probe_by_ip_without_onvif_reports_the_path(self) -> None:
        self.camera("/h264Preview_01_main", "/h264Preview_01_sub")
        with mock.patch.object(discovery, "onvif_service", return_value=""):
            detected = discovery.probe("127.0.0.1", "admin", "s3cr3t!x", timeout=3)
        self.assertTrue(detected.verified)
        summary = detected.summary()
        self.assertEqual("template", summary["source"])
        self.assertEqual("/h264Preview_01_main", summary["path"])
        self.assertEqual("Reolink", summary["template"])
        self.assertTrue(summary["sub_url"].startswith("rtsp://***@127.0.0.1:"))

    def test_onvif_streams_have_no_template_path(self) -> None:
        detected = discovery.Detected(host="192.0.2.5", main_url="rtsp://a:b@192.0.2.5/x")
        self.assertEqual("", detected.summary()["path"])
        self.assertEqual("", detected.summary()["template"])


class MaskTest(unittest.TestCase):
    def test_path_and_query_passwords_are_masked(self) -> None:
        for url in ("rtsp://u:p@10.0.0.2:554/user=u&password=TopSecret&channel=1&stream=0.sdp",
                    "http://10.0.0.2/cgi-bin/api.cgi?cmd=Snap&user=u&pwd=TopSecret",
                    "rtsp://10.0.0.2/live?PASS=TopSecret"):
            with self.subTest(url):
                self.assertNotIn("TopSecret", discovery.mask(url))
                self.assertNotIn("TopSecret", provision.mask(url))
        self.assertEqual("rtsp://***@10.0.0.2/Streaming/Channels/101",
                         discovery.mask("rtsp://admin:x@10.0.0.2/Streaming/Channels/101"))
        self.assertTrue(discovery.has_path_secret("rtsp://h/user=a&password=b"))
        self.assertFalse(discovery.has_path_secret("rtsp://h/Streaming/Channels/101"))

    def test_registry_listing_hides_xmeye_password(self) -> None:
        listed = provision.summary({"cameras": [{
            "camera_id": "yard", "title": "Двор", "site": "Двор",
            "rtsp_url": "rtsp://admin:pw1@192.0.2.9:554/user=admin&password=pw1&channel=1&stream=0.sdp",
            "detect_rtsp_url": "rtsp://admin:pw1@192.0.2.9:554/user=admin&password=pw1&channel=1&stream=1.sdp"}]})
        self.assertNotIn("pw1", str(listed))


if __name__ == "__main__":
    unittest.main()
