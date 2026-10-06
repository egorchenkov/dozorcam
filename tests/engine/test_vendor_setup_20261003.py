"""Марка и «ждёт первичной настройки» у камер без автоактивации — без входа.

Камеры — маленькие HTTP-эмуляторы на 127.0.0.x: отдают страницу `GET /` как
веб-интерфейс вендора, у Axis — VAPIX systemready.cgi. Проверяется, что опрос
узнаёт марку, помечает новую камеру `activation="manual"` и при этом не шлёт
ни одной авторизации и ни одного запроса входа: у новой камеры учётки ещё нет,
а у настроенной лишние попытки ведут к блокировке.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from cctv.engine import camera_discovery as discovery
from cctv.engine import vendor_setup
from hik_emulator import free_port

DAHUA_PAGE = ("<html><head><title>WEB SERVICE</title></head><body>"
              "<script src='/jsBase/lib/jquery.js'></script>"
              "<script>var url='/RPC2_Login';</script></body></html>")
AXIS_PAGE = ("<html><head><meta http-equiv='refresh' content='0; url=/camera/index.html'>"
             "<title>AXIS</title></head><body>Axis Communications</body></html>")
ROUTER_PAGE = "<html><head><title>TP-Link Wireless Router</title></head><body>login</body></html>"


class WebCamera:
    """Веб-интерфейс камеры: страница `/`, опционально systemready Axis."""

    def __init__(self, ip: str, port: int, page: str, *, server: str = "",
                 needsetup: str | None = None) -> None:
        self.ip, self.port, self.page, self.server = ip, port, page, server
        self.needsetup = needsetup
        self.requests: list[tuple[str, str, dict]] = []

    @property
    def base(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def start(self) -> "WebCamera":
        camera = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, body: str, ctype: str = "text/html") -> None:
                data = body.encode()
                self.send_response(status)
                if camera.server:
                    self.send_header("Server", camera.server)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                camera.requests.append(("GET", self.path, dict(self.headers)))
                if self.path == "/":
                    return self._reply(200, camera.page)
                return self._reply(404, "")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                camera.requests.append(("POST", self.path, dict(self.headers)))
                if self.path == "/axis-cgi/systemready.cgi" and camera.needsetup is not None:
                    assert json.loads(body)["method"] == "systemready"
                    return self._reply(200, json.dumps({"apiVersion": "1.0", "data": {
                        "systemready": "yes", "needsetup": camera.needsetup,
                        "uptime": "42", "bootid": "x"}}), "application/json")
                return self._reply(404, "")

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer((self.ip, self.port), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()


class BrandTextTest(unittest.TestCase):
    def test_brands_from_banners_and_pages(self) -> None:
        cases = {
            "dahua": DAHUA_PAGE,
            "axis": AXIS_PAGE,
            "uniview": "<script src='/LAPI/V1.0/System/DeviceInfo'></script>",
            "hanwha": "<title>Wisenet WEBVIEWER</title>",
            "reolink": "Server: Reolink",
            "vigi": "<title>VIGI C340</title>",
            "milesight": "Milesight Network Camera",
            "xiongmai": "NETSurveillance WEB",
            "tantos": "<title>TANTOS IPC</title>",
            "hikvision": "doc/page/login.asp?_1",
            "ezviz": "EZVIZ C6N",
            "tvt": "<title>NVMS-9000</title>",
        }
        for brand, text in cases.items():
            with self.subTest(brand=brand):
                self.assertEqual(brand, vendor_setup.brand_from_text(text))

    def test_router_pages_are_not_cameras(self) -> None:
        for text in (ROUTER_PAGE, "<title>WEB SERVICE</title>", "", "nginx", "RouterOS"):
            with self.subTest(text=text):
                self.assertEqual("", vendor_setup.brand_from_text(text))

    def test_every_brand_has_an_instruction_in_the_bot(self) -> None:
        from cctv.bot import bot

        self.assertEqual(set(vendor_setup.KNOWN_BRANDS), set(bot.MANUAL_SETUP_BRANDS))
        self.assertLessEqual(set(vendor_setup.KNOWN_BRANDS), set(bot.BRAND_NAMES))


class IdentifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.port = free_port()
        for name, value in (("ACTIVATION_WEB_PORTS", (self.port,)), ("ONVIF_PORTS", ())):
            patcher = mock.patch.object(discovery, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def camera(self, octet: int, page: str, **kwargs) -> WebCamera:
        camera = WebCamera(f"127.0.0.{octet}", self.port, page, **kwargs).start()
        self.addCleanup(camera.stop)
        return camera

    def identify(self, camera: WebCamera, ports=None) -> discovery.Candidate:
        return discovery.identify(
            discovery.Candidate(host=camera.ip, ports=list(ports or [self.port])), timeout=2)

    def assert_no_login(self, camera: WebCamera) -> None:
        self.assertTrue(camera.requests)
        for method, path, headers in camera.requests:
            self.assertNotIn("Authorization", headers, path)
            self.assertNotIn("login", path.lower())
            self.assertIn(path, ("/", "/SDK/activateStatus", "/axis-cgi/systemready.cgi"))

    def test_axis_waiting_for_setup(self) -> None:
        camera = self.camera(21, AXIS_PAGE, needsetup="yes")
        candidate = self.identify(camera, [self.port, 554])
        self.assertEqual("axis", candidate.brand)
        self.assertIs(False, candidate.activated)
        self.assertEqual("manual", candidate.activation)
        self.assertEqual("manual", candidate.as_dict()["activation"])
        self.assertEqual("axis", candidate.as_dict()["brand"])
        self.assert_no_login(camera)

    def test_axis_already_set_up_goes_the_usual_way(self) -> None:
        camera = self.camera(22, AXIS_PAGE, needsetup="no")
        candidate = self.identify(camera, [self.port, 554])
        self.assertIs(True, candidate.activated)
        self.assertEqual("", candidate.activation)

    def test_web_only_dahua_is_offered_manual_setup(self) -> None:
        camera = self.camera(23, DAHUA_PAGE)
        candidate = self.identify(camera)
        self.assertEqual("dahua", candidate.brand)
        self.assertEqual("dahua", candidate.vendor)
        self.assertIsNone(candidate.activated)
        self.assertEqual("manual", candidate.activation)
        self.assert_no_login(camera)

    def test_dahua_with_rtsp_is_a_regular_candidate(self) -> None:
        # RTSP открыт — состояние неизвестно, путь «логин и пароль» как раньше.
        camera = self.camera(24, DAHUA_PAGE)
        with mock.patch.object(discovery, "rtsp_banner", return_value=""):
            candidate = self.identify(camera, [self.port, 554])
        self.assertEqual("dahua", candidate.brand)
        self.assertEqual("", candidate.activation)

    def test_router_is_not_a_candidate(self) -> None:
        camera = self.camera(25, ROUTER_PAGE)
        candidate = self.identify(camera)
        self.assertEqual("", candidate.brand)
        self.assertEqual("", candidate.activation)
        with mock.patch.object(discovery, "SCAN_PORTS", (self.port,)):
            found = discovery.scan(["127.0.0.25/32"], ports=(self.port,), timeout=1)
        self.assertEqual([], found)

    def test_scan_lists_manual_cameras(self) -> None:
        self.camera(26, DAHUA_PAGE)
        found = discovery.scan(["127.0.0.26/32"], ports=(self.port,), timeout=2)
        self.assertEqual(["127.0.0.26"], [c.host for c in found])
        self.assertEqual("manual", found[0].activation)

    def test_broken_firmware_does_not_break_identify(self) -> None:
        camera = self.camera(27, AXIS_PAGE, needsetup="yes")
        with mock.patch.object(vendor_setup, "axis_needs_setup", side_effect=RuntimeError), \
                mock.patch.dict(vendor_setup.SETUP_CHECKS,
                                {"axis": vendor_setup.axis_needs_setup}):
            candidate = self.identify(camera)
        # Проверка упала — не знаем; одна веб-морда марки → инструкция.
        self.assertIsNone(candidate.activated)
        self.assertEqual("manual", candidate.activation)
        with mock.patch.object(vendor_setup, "web_fingerprint", side_effect=RuntimeError):
            candidate = self.identify(camera)
        self.assertEqual("", candidate.brand)

    def test_silent_host(self) -> None:
        self.assertIsNone(vendor_setup.axis_needs_setup(f"http://127.0.0.28:{self.port}",
                                                        timeout=1))
        self.assertEqual("", vendor_setup.web_fingerprint(f"http://127.0.0.28:{self.port}",
                                                          timeout=1))


if __name__ == "__main__":
    unittest.main()
