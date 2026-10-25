"""Мастер /add: реестр на DNS-именах и строгий признак ONVIF (25.10.2026).

1. Реестр ведётся на именах (rtsp://gate.cams.example/…), поиск видит камеры по
   IP. Мост сравнивал имя с адресом буквально, и все заведённые камеры
   приходили в мастер «новыми» — на выбор любой бот просил логин и пароль.
   Теперь ключи сверки — имя и все его адреса; DNS молчит или отказал — мастер
   отвечает как раньше, без падения и без долгого ожидания.
2. ONVIF признавался по любому разбираемому XML в ответ на GetSystemDateAndTime:
   веб-морда с HTML-страницей на 80/8080 попадала в «новые камеры».
"""
from __future__ import annotations

import dataclasses
import pathlib
import socket
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from cctv.engine import camera_discovery as discovery
from cctv.engine import cctv_bridge

NAMES = {"gate.cams.example": ["192.0.2.21"],
         "yard.cams.example": ["192.0.2.22", "2001:db8::22"],
         "snap.cams.example": ["192.0.2.23"]}


def addrinfo(addresses: list[str]) -> list[tuple]:
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
             (a, 0, 0, 0) if ":" in a else (a, 0)) for a in addresses]


class RegistryOnDnsNames(unittest.TestCase):
    def setUp(self) -> None:
        config = {"cameras": [
            {"camera_id": "gate", "title": "Ворота", "site": "Дача",
             "rtsp_url": "rtsp://admin:x@gate.cams.example:554/Streaming/Channels/101"},
            {"camera_id": "yard", "title": "Двор", "site": "Дача",
             "rtsp_url": "rtsp://admin:x@yard.cams.example/live",
             "detect_rtsp_url": "rtsp://admin:x@yard.cams.example/sub"},
            {"camera_id": "porch", "title": "Крыльцо", "site": "Город",
             "rtsp_url": "rtsp://admin:x@192.0.2.24/live",
             "snapshot_url": "http://snap.cams.example/ISAPI/Streaming/channels/101/picture"},
        ]}
        self.bridge = cctv_bridge.Bridge(config, pathlib.Path(tempfile.mkdtemp()),
                                         "https://cctv-bridge")
        self.addCleanup(self.bridge._resolver.shutdown, wait=False, cancel_futures=True)
        self.bridge._scans["s1"] = {
            "status": "done", "started": time.time(), "finished": time.time(),
            "networks": ["192.0.2.0/24"],
            "candidates": [{"host": f"192.0.2.{octet}", "ports": [80, 554]}
                           for octet in (21, 22, 23, 24, 30)]}
        self.lookups: list[str] = []

    def resolve(self, name, *args, **kwargs):
        self.lookups.append(name)
        if name not in NAMES:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return addrinfo(NAMES[name])

    def marks(self) -> dict[str, str]:
        result = self.bridge.scan_status("s1")
        self.assertTrue(result["ok"])
        return {c["host"]: c["registered_camera_id"] for c in result["candidates"]}

    def test_cameras_registered_by_name_are_not_new(self) -> None:
        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", self.resolve):
            marks = self.marks()
        self.assertEqual({"192.0.2.21": "gate", "192.0.2.22": "yard", "192.0.2.23": "porch",
                          "192.0.2.24": "porch", "192.0.2.30": ""}, marks)

    def test_dns_answers_are_cached_between_status_polls(self) -> None:
        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", self.resolve):
            self.marks()
            self.marks()
        self.assertEqual(sorted(NAMES), sorted(self.lookups))

    def test_dns_failure_does_not_break_the_wizard(self) -> None:
        def broken(name, *args, **kwargs):
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", broken):
            marks = self.marks()
        # Без DNS сверка — как раньше: по буквальному адресу из URL.
        self.assertEqual({"192.0.2.21": "", "192.0.2.22": "", "192.0.2.23": "",
                          "192.0.2.24": "porch", "192.0.2.30": ""}, marks)

    def test_failed_lookup_is_retried_soon(self) -> None:
        def broken(name, *args, **kwargs):
            self.lookups.append(name)
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", broken), \
                mock.patch.object(cctv_bridge, "HOST_RESOLVE_FAIL_TTL_SEC", -1):
            self.marks()
        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", self.resolve):
            self.assertEqual("gate", self.marks()["192.0.2.21"])

    def test_hanging_dns_is_waited_for_no_longer_than_the_timeout(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hanging(name, *args, **kwargs):
            release.wait(10)
            return self.resolve(name)

        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", hanging), \
                mock.patch.object(cctv_bridge, "HOST_RESOLVE_TIMEOUT_SEC", 0.3):
            started = time.monotonic()
            marks = self.marks()
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual("", marks["192.0.2.21"])
            # Ответ DNS дошёл — следующий опрос статуса берёт его из кэша.
            release.set()
            deadline = time.monotonic() + 5
            while self.marks()["192.0.2.21"] != "gate" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual("gate", self.marks()["192.0.2.21"])

    def test_broken_url_in_the_registry_is_skipped(self) -> None:
        porch = self.bridge.cameras["porch"]
        self.bridge.cameras["porch"] = dataclasses.replace(porch, snapshot_url="http://[broken/picture")
        with mock.patch.object(cctv_bridge.socket, "getaddrinfo", self.resolve):
            self.assertEqual("porch", self.marks()["192.0.2.24"])


SOAP_TIME = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>
  <tt:DateTimeType>NTP</tt:DateTimeType>
 </tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body>
</s:Envelope>"""
HTML_PAGE = """<!DOCTYPE html>
<html><head><title>Router</title></head><body><p>Login</p></body></html>"""
OTHER_SOAP = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
 <s:Body><Hello xmlns="urn:example"/></s:Body></s:Envelope>"""
SOAP11_TIME = """<e:Envelope xmlns:e="http://schemas.xmlsoap.org/soap/envelope/">
 <e:Body><GetSystemDateAndTimeResponse xmlns="http://www.onvif.org/ver10/device/wsdl"/></e:Body>
</e:Envelope>"""


class Device:
    """Устройство на 127.0.0.1: на любой POST отдаёт заданный ответ."""

    def __init__(self, body: str, content_type: str) -> None:
        payload = body.encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class StrictOnvifSign(unittest.TestCase):
    def identify(self, body: str, content_type: str) -> discovery.Candidate:
        device = Device(body, content_type)
        self.addCleanup(device.stop)
        with mock.patch.object(discovery, "ONVIF_PORTS", (device.port,)), \
                mock.patch.object(discovery, "ACTIVATION_WEB_PORTS", ()):
            return discovery.identify(
                discovery.Candidate(host="127.0.0.1", ports=[device.port]), timeout=2)

    def test_html_page_is_not_onvif(self) -> None:
        candidate = self.identify(HTML_PAGE, "text/html")
        self.assertEqual("", candidate.onvif_url)
        self.assertFalse(candidate.as_dict().get("onvif"))

    def test_real_soap_reply_is_onvif(self) -> None:
        candidate = self.identify(SOAP_TIME, "application/soap+xml")
        self.assertTrue(candidate.onvif_url.endswith(discovery.ONVIF_PATHS[0]))
        self.assertTrue(candidate.as_dict().get("onvif"))

    def test_html_web_ui_is_not_a_scan_candidate(self) -> None:
        device = Device(HTML_PAGE, "text/html")
        self.addCleanup(device.stop)
        with mock.patch.object(discovery, "ONVIF_PORTS", (device.port,)), \
                mock.patch.object(discovery, "ACTIVATION_WEB_PORTS", ()):
            found = discovery.scan(["127.0.0.1/32"], ports=(device.port,), timeout=2)
        self.assertEqual([], found)

    def test_reply_shapes(self) -> None:
        for body, expected in ((SOAP_TIME, True), (SOAP11_TIME, True), (HTML_PAGE, False),
                               (OTHER_SOAP, False), ("<GetSystemDateAndTimeResponse/>", False)):
            with self.subTest(body[:40]):
                root = ET.fromstring(body.encode().split(b"\n", 1)[1]
                                     if body.startswith("<!DOCTYPE") else body.encode())
                self.assertIs(expected, discovery.is_onvif_time_reply(root))
        self.assertFalse(discovery.is_onvif_time_reply(None))


if __name__ == "__main__":
    unittest.main()
