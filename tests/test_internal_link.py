"""Связь мост↔бот вживую: настоящие HTTP-серверы моста и приёмника событий бота.

По умолчанию — loopback без TLS; CCTV_INTERNAL_TLS=1 включает mTLS в обе стороны
(бот → мост: реестр и медиа; мост → бот: события). Порты берутся у ядра, на
прод-порты тест не садится; камеры не нужны.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from cctv.bot import config as bot_config
from cctv.bot.bridge import Bridge as BotBridge, BridgeError
from cctv.bot.events import EventServer
from cctv.engine import cctv_bridge as engine

CAMERAS = {"cameras": [{"camera_id": "testcam", "title": "Тест", "site": "lab",
                        "rtsp_url": "rtsp://192.0.2.1/stream"}]}


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LinkMixin:
    tls = False

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.bridge_port, self.events_port = free_port(), free_port()
        scheme = "https" if self.tls else "http"
        self.engine_env = {
            "CCTV_BIND": "127.0.0.1", "CCTV_PORT": str(self.bridge_port),
            "CCTV_EVENTS_URL": f"{scheme}://127.0.0.1:{self.events_port}/v1/events",
            "CCTV_STATE_DIR": str(self.dir / "state"), "CCTV_BUFFER_DIR": str(self.dir / "buffer"),
        }
        self.bot_env = {
            "CCTV_BOT_TOKEN": "0:test", "CCTV_CHAT_ID": "-100", "CCTV_ALLOWED_USER_IDS": "7",
            "CCTV_BRIDGE_URL": f"{scheme}://127.0.0.1:{self.bridge_port}",
            "CCTV_EVENTS_HOST": "127.0.0.1", "CCTV_EVENTS_PORT": str(self.events_port),
            "CCTV_STATE_DIR": str(self.dir / "bot-state"), "CCTV_RUNTIME_DIR": str(self.dir / "bot-tmp"),
        }
        if self.tls:
            self.make_pki()
            self.engine_env.update({
                "CCTV_INTERNAL_TLS": "1",
                "CCTV_SERVER_CERT": self.pem("server.crt"), "CCTV_SERVER_KEY": self.pem("server.key"),
                "CCTV_CLIENT_CA": self.pem("ca.crt"),
                "CCTV_EVENTS_CERT": self.pem("client.crt"), "CCTV_EVENTS_KEY": self.pem("client.key"),
                "CCTV_EVENTS_CA": self.pem("ca.crt"),
            })
            self.bot_env.update({
                "CCTV_INTERNAL_TLS": "1",
                "CCTV_BRIDGE_CLIENT_CERT": self.pem("client.crt"),
                "CCTV_BRIDGE_CLIENT_KEY": self.pem("client.key"), "CCTV_BRIDGE_CA": self.pem("ca.crt"),
                "CCTV_EVENTS_CERT": self.pem("server.crt"), "CCTV_EVENTS_KEY": self.pem("server.key"),
                "CCTV_EVENTS_CLIENT_CA": self.pem("ca.crt"),
            })
        (self.dir / "bot-tmp").mkdir()
        patch = mock.patch.dict(os.environ, self.engine_env)
        patch.start()
        self.addCleanup(patch.stop)

        public = f"{scheme}://127.0.0.1:{self.bridge_port}"
        self.engine = engine.Bridge(CAMERAS, self.dir / "store", public)
        self.server = engine.build_server(self.engine, os.environ)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.cfg = bot_config.load(self.bot_env)
        self.client = BotBridge(self.cfg)
        self.addCleanup(self.client.close)

        self.events: list = []
        self.got_event = threading.Event()

        def submit(event):
            self.events.append(event)
            self.got_event.set()

        self.receiver = EventServer(self.cfg, submit)
        self.receiver.serve_in_thread()
        self.addCleanup(self.receiver.server_close)
        self.addCleanup(self.receiver.shutdown)

    # --- PKI ------------------------------------------------------------
    def pem(self, name: str) -> str:
        return str(self.dir / "pki" / name)

    def make_pki(self) -> None:
        pki = self.dir / "pki"
        pki.mkdir()

        def run(*args):
            subprocess.run(["openssl", *args], cwd=pki, check=True, capture_output=True)

        run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key", "-out", "ca.crt",
            "-subj", "/CN=cctv-test-ca", "-days", "1")
        (pki / "san.ext").write_text("subjectAltName=IP:127.0.0.1,DNS:localhost\n")
        for name in ("server", "client"):
            run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
                "-subj", f"/CN={name}")
            run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                "-CAcreateserial", "-out", f"{name}.crt", "-days", "1", "-extfile", "san.ext")

    # --- общие проверки -------------------------------------------------------
    def check_registry(self):
        cameras, storage = self.client.registry()
        self.assertEqual(["testcam"], [camera.camera_id for camera in cameras])
        self.assertIsNotNone(storage)

    def check_media_roundtrip(self):
        body = b"\xff\xd8 test frame"
        token = self.engine.issue_token(body, "image/jpeg")
        url = f"{self.cfg.bridge_base_url}/v1/media/{token}"
        got = self.client.download(url, kind="snapshot", expected_sha256=hashlib.sha256(body).hexdigest())
        try:
            self.assertEqual(len(body), got.size)
            self.assertEqual(body, pathlib.Path(got.path).read_bytes())
        finally:
            os.unlink(got.path)

    def check_event_push(self):
        body = b"\xff\xd8 motion"
        token = self.engine.issue_token(body, "image/jpeg")
        self.engine.push_event({
            "event_id": "ev-1", "type": "motion.detected", "camera_id": "testcam",
            "occurred_at": "2026-10-02T00:00:00Z", "source": "test",
            "snapshot": {"url": f"{self.engine.public_url}/v1/media/{token}",
                         "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}})
        self.assertTrue(self.got_event.wait(10), "событие моста не дошло до бота")
        event = self.events[0]
        self.assertEqual("motion.detected", event.type)
        self.assertTrue(self.client.is_bridge_url(event.media.url))


class LoopbackPlainLinkTest(LinkMixin, unittest.TestCase):
    tls = False

    def test_bot_reads_registry_over_plain_loopback(self):
        self.assertFalse(self.cfg.internal_tls)
        self.assertTrue(self.cfg.bridge_base_url.startswith("http://127.0.0.1:"))
        self.check_registry()

    def test_media_roundtrip(self):
        self.check_media_roundtrip()

    def test_engine_pushes_events_to_bot_without_tls(self):
        self.check_event_push()

    def test_https_media_url_is_foreign_in_plain_mode(self):
        self.assertFalse(self.client.is_bridge_url(f"https://127.0.0.1:{self.bridge_port}/v1/media/x"))


@unittest.skipUnless(shutil.which("openssl"), "нужен openssl для тестовой PKI")
class InternalTlsLinkTest(LinkMixin, unittest.TestCase):
    tls = True

    def test_tls_switch_turns_on_mtls(self):
        self.assertTrue(self.cfg.internal_tls)
        self.assertTrue(self.cfg.bridge_base_url.startswith("https://"))
        self.check_registry()

    def test_media_roundtrip(self):
        self.check_media_roundtrip()

    def test_engine_pushes_events_with_client_certificate(self):
        self.check_event_push()

    def test_bridge_refuses_plain_http_and_anonymous_tls(self):
        url = f"http://127.0.0.1:{self.bridge_port}/v1/cameras"
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(url, timeout=5).read()
        anonymous = ssl.create_default_context(cafile=self.pem("ca.crt"))
        with self.assertRaises((urllib.error.URLError, ssl.SSLError, ConnectionError, OSError)):
            urllib.request.urlopen(f"https://127.0.0.1:{self.bridge_port}/v1/cameras",
                                   context=anonymous, timeout=5).read()

    def test_event_receiver_refuses_client_without_certificate(self):
        anonymous = ssl.create_default_context(cafile=self.pem("ca.crt"))
        request = urllib.request.Request(f"https://127.0.0.1:{self.events_port}/v1/events",
                                         json.dumps({}).encode(), method="POST")
        with self.assertRaises((urllib.error.URLError, ssl.SSLError, ConnectionError, OSError)):
            urllib.request.urlopen(request, context=anonymous, timeout=5).read()
        self.assertEqual([], self.events)

    def test_plain_bot_client_cannot_reach_tls_bridge(self):
        plain = bot_config.load({**self.bot_env, "CCTV_INTERNAL_TLS": "0",
                                 "CCTV_BRIDGE_URL": f"http://127.0.0.1:{self.bridge_port}"})
        client = BotBridge(plain)
        self.addCleanup(client.close)
        with self.assertRaises(BridgeError):
            client.registry()


class LinkConfigGuardsTest(unittest.TestCase):
    BASE = {"CCTV_BOT_TOKEN": "0:t", "CCTV_CHAT_ID": "-1", "CCTV_ALLOWED_USER_IDS": "1"}

    def test_plain_link_must_stay_on_loopback(self):
        for url in ("http://192.0.2.10:8780", "http://bridge:8780", "https://127.0.0.1:8780"):
            with self.subTest(url=url), self.assertRaises(bot_config.ConfigError):
                bot_config.load({**self.BASE, "CCTV_BRIDGE_URL": url})
        with self.assertRaises(bot_config.ConfigError):
            bot_config.load({**self.BASE, "CCTV_EVENTS_HOST": "0.0.0.0"})

    def test_plain_link_needs_no_certificates(self):
        cfg = bot_config.load(dict(self.BASE))
        self.assertEqual("http://127.0.0.1:8780", cfg.bridge_base_url)
        self.assertIsNone(cfg.bridge_ca_bundle)

    def test_tls_requires_https_and_certificates(self):
        with self.assertRaises(bot_config.ConfigError):
            bot_config.load({**self.BASE, "CCTV_INTERNAL_TLS": "1",
                             "CCTV_BRIDGE_URL": "http://127.0.0.1:8780"})
        with self.assertRaises(bot_config.ConfigError):
            bot_config.load({**self.BASE, "CCTV_INTERNAL_TLS": "1",
                             "CCTV_BRIDGE_URL": "https://127.0.0.1:8780"})

    def test_engine_refuses_open_http_outside_loopback(self):
        with self.assertRaises(SystemExit):
            engine.server_context({"CCTV_BIND": "0.0.0.0"})
        with self.assertRaises(SystemExit):
            engine.server_context({"CCTV_INTERNAL_TLS": "1"})
        self.assertIsNone(engine.server_context({"CCTV_BIND": "127.0.0.1"}))

    def test_engine_never_sends_events_over_open_network(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"CCTV_EVENTS_URL": "http://192.0.2.5:8781/v1/events"}):
            bridge = engine.Bridge(CAMERAS, pathlib.Path(tmp), "http://127.0.0.1:1")
            with self.assertRaises(ValueError):
                bridge.events_context()
            with self.assertRaises(SystemExit):
                engine.check_events_url(bridge)


if __name__ == "__main__":
    unittest.main()
