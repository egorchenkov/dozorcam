#!/usr/bin/env python3
"""CCTV-интерфейс: состояние, контракт событий и безопасная загрузка медиа.

Проверяются настоящие модули сервиса; подменён только HTTP-транспорт Bridge,
потому что предмет проверки — граница доступа и идемпотентность, а не сеть.
"""
from __future__ import annotations

import hashlib
import pathlib
import ssl
import sys
import tempfile
import unittest


import httpx  # noqa: E402

from cctv.bot import config  # noqa: E402
from cctv.bot.bridge import Bridge, BridgeError, MediaRejected, build_ssl_context  # noqa: E402
from cctv.bot.events import EventRejected, normalize_event  # noqa: E402
from cctv.bot.state import State  # noqa: E402

BRIDGE = "https://cctv-bridge.internal"


def make_config(tmp: pathlib.Path) -> config.Config:
    material = tmp / "pem"
    material.write_text("not-a-real-key", encoding="utf-8")
    return config.Config(
        bot_token="0:test", chat_id=-100500, allowed_user_ids=frozenset({7}),
        bridge_base_url=BRIDGE, bridge_client_cert=material, bridge_client_key=material,
        bridge_ca_bundle=material, state_dir=tmp, runtime_dir=tmp,
        max_snapshot_bytes=64, max_clip_bytes=128, lang="ru",
    )


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = State(":memory:")
        self.addCleanup(self.state.close)

    def test_registration_is_idempotent(self):
        first = self.state.bind_topic("city", 11, "Город")
        second = self.state.bind_topic("city", 11, "Город")
        self.assertEqual(first.thread_id, second.thread_id)
        self.assertEqual(1, len(self.state.active_topics()))

    def test_thread_is_never_reused_by_another_camera(self):
        self.state.bind_topic("city", 11, "Город")
        with self.assertRaises(ValueError):
            self.state.bind_topic("moskovskaya", 11, "Московская")

    def test_retired_camera_keeps_archive_link(self):
        self.state.bind_topic("city", 11, "Город")
        self.state.retire_topic("city")
        self.assertEqual([], self.state.active_topics())
        self.assertEqual("retired", self.state.topic_for("city").status)

    def test_camera_id_format_is_enforced(self):
        for bad in ("../etc", "rtsp://x", "камера", "", "a" * 65):
            with self.assertRaises(ValueError, msg=bad):
                self.state.bind_topic(bad, 12, "x")

    def test_callback_token_hides_camera_and_expires(self):
        token = self.state.issue_callback("city", "snap", 60)
        self.assertNotIn("city", token)
        self.assertEqual(("city", "snap", None), self.state.resolve_callback(token))
        expired = self.state.issue_callback("city", "snap", -1)
        self.assertIsNone(self.state.resolve_callback(expired))
        self.assertIsNone(self.state.resolve_callback("подделка"))

    def test_event_id_is_deduplicated(self):
        self.assertTrue(self.state.is_new_event("e1"))
        self.assertFalse(self.state.is_new_event("e1"))

    def test_media_request_is_delivered_once(self):
        self.state.remember_request("r1", "city", "snapshot", 11)
        self.assertFalse(self.state.remember_request("r1", "city", "snapshot", 11))
        self.assertIsNotNone(self.state.take_request("r1"))
        self.assertIsNone(self.state.take_request("r1"))


class EventContractTest(unittest.TestCase):
    def motion(self, **over):
        payload = {
            "event_id": "e1", "type": "motion.detected", "camera_id": "city",
            "occurred_at": "2026-08-24T10:00:00Z",
            "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": "a" * 64, "bytes": 100},
        }
        payload.update(over)
        return payload

    def test_valid_motion_is_accepted(self):
        event = normalize_event(self.motion())
        self.assertEqual("city", event.camera_id)
        self.assertEqual(100, event.media.bytes)

    def test_plain_http_media_is_rejected(self):
        bad = self.motion(snapshot={"url": "http://x/y", "sha256": "a" * 64})
        with self.assertRaises(EventRejected):
            normalize_event(bad)

    def test_missing_or_malformed_checksum_is_rejected(self):
        for sha in ("", "deadbeef"):
            with self.assertRaises(EventRejected, msg=sha):
                normalize_event(self.motion(snapshot={"url": f"{BRIDGE}/m", "sha256": sha}))

    def test_unknown_type_and_camera_are_rejected(self):
        with self.assertRaises(EventRejected):
            normalize_event(self.motion(type="shell.exec"))
        with self.assertRaises(EventRejected):
            normalize_event(self.motion(camera_id="../../etc/passwd"))

    def test_media_ready_requires_kind_and_download(self):
        base = {"event_id": "e2", "type": "media.ready", "camera_id": "city",
                "request_id": "r1", "captured_at": "2026-08-24T10:00:00Z"}
        with self.assertRaises(EventRejected):
            normalize_event(dict(base, kind="snapshot"))
        with self.assertRaises(EventRejected):
            normalize_event(dict(base, download={"url": f"{BRIDGE}/m", "sha256": "a" * 64}))
        ok = normalize_event(dict(base, kind="clip",
                                  download={"url": f"{BRIDGE}/m", "sha256": "b" * 64, "bytes": 10}))
        self.assertEqual("video/mp4", ok.media.content_type)


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)

    def bridge_with(self, body: bytes, status: int = 200) -> Bridge:
        def handler(_request):
            return httpx.Response(status, content=body, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        bridge = Bridge(self.cfg, client=client)
        self.addCleanup(client.close)
        return bridge

    def test_valid_media_is_downloaded_and_verified(self):
        body = b"jpeg-bytes"
        bridge = self.bridge_with(body)
        got = bridge.download(f"{BRIDGE}/v1/media/opaque", kind="snapshot",
                              expected_sha256=hashlib.sha256(body).hexdigest(),
                              declared_bytes=len(body))
        self.assertEqual(len(body), got.size)
        self.assertTrue(pathlib.Path(got.path).is_file())

    def test_checksum_mismatch_is_rejected_and_temp_file_removed(self):
        bridge = self.bridge_with(b"tampered")
        before = set(self.tmp.iterdir())
        with self.assertRaises(MediaRejected):
            bridge.download(f"{BRIDGE}/v1/media/opaque", kind="snapshot", expected_sha256="c" * 64)
        self.assertEqual(before, set(self.tmp.iterdir()))

    def test_oversized_stream_is_cut_by_limit(self):
        bridge = self.bridge_with(b"x" * 1000)
        with self.assertRaises(MediaRejected) as ctx:
            bridge.download(f"{BRIDGE}/v1/media/opaque", kind="snapshot", expected_sha256="")
        self.assertEqual("media_too_large", ctx.exception.code)

    def test_url_outside_bridge_is_refused(self):
        bridge = self.bridge_with(b"x")
        for url in ("https://evil.example/m", "file:///etc/passwd", f"http://{'cctv-bridge.internal'}/m"):
            with self.assertRaises(MediaRejected, msg=url):
                bridge.download(url, kind="snapshot", expected_sha256="")

    def test_download_error_body_is_read_in_stream_mode(self):
        """В стриме тело ошибки не прочитано: без read() код маскировался в unavailable."""
        def handler(_request):
            return httpx.Response(413, json={"error": "media_too_large"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        bridge = Bridge(self.cfg, client=client)
        with self.assertRaises(BridgeError) as ctx:
            bridge.download(f"{BRIDGE}/v1/media/opaque", kind="snapshot", expected_sha256="")
        self.assertEqual("media_too_large", ctx.exception.code)

    def test_download_404_means_expired_media_not_missing_camera(self):
        """404 на выдаче — истёкшая ссылка; «камера не найдена» здесь ложь."""
        def handler(_request):
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        bridge = Bridge(self.cfg, client=client)
        with self.assertRaises(BridgeError) as ctx:
            bridge.download(f"{BRIDGE}/v1/media/opaque", kind="snapshot", expected_sha256="")
        self.assertEqual("unavailable", ctx.exception.code)

    def test_clip_window_empty_survives_error_normalisation(self):
        """clip_window_empty не в KNOWN_ERRORS превращался в generic unavailable."""
        self.assertEqual("clip_window_empty", BridgeError("clip_window_empty").code)
        self.assertEqual("storage_capacity", BridgeError("storage_capacity").code)

    def test_bridge_error_code_is_normalised(self):
        def handler(_request):
            return httpx.Response(503, json={"error": "camera_offline"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        with self.assertRaises(BridgeError) as ctx:
            Bridge(self.cfg, client=client).cameras()
        self.assertEqual("camera_offline", ctx.exception.code)


class MutualTlsTest(unittest.TestCase):
    """Клиентский сертификат обязан реально попадать в TLS-контекст.

    Проверка появилась после живого прогона: httpx 0.28 при `verify=<путь>`
    возвращает контекст, не применив `cert=...`, и соединение молча теряет
    клиентскую половину mTLS. Со стороны бота это выглядит как рабочая
    конфигурация, поэтому ловится только явным утверждением.
    """

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_client_never_relies_on_httpx_cert_argument(self):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        # Материал в конфиге фиктивный, поэтому подменяется сборка контекста:
        # предмет проверки — что именно Bridge передаёт транспорту.
        from cctv.bot import bridge as bridge_module

        marker = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        original_client, original_ctx = httpx.Client, bridge_module.build_ssl_context
        httpx.Client, bridge_module.build_ssl_context = FakeClient, lambda _cfg: marker
        try:
            Bridge(make_config(self.tmp))
        finally:
            httpx.Client, bridge_module.build_ssl_context = original_client, original_ctx
        self.assertNotIn("cert", captured, "cert=... в httpx 0.28 игнорируется при verify=<путь>")
        self.assertIs(captured.get("verify"), marker)

    def test_context_fails_loudly_when_material_is_not_a_key(self):
        # Материал в make_config — не ключ; попытка загрузить его обязана упасть,
        # иначе «контекст собран» ничего не доказывает.
        with self.assertRaises((ssl.SSLError, OSError)):
            build_ssl_context(make_config(self.tmp))


class ConfigTest(unittest.TestCase):
    def base_env(self, tmp: pathlib.Path) -> dict[str, str]:
        material = tmp / "pem"
        material.write_text("x", encoding="utf-8")
        return {
            "CCTV_BOT_TOKEN": "0:test", "CCTV_CHAT_ID": "-100500",
            "CCTV_ALLOWED_USER_IDS": "7 8", "CCTV_BRIDGE_URL": BRIDGE,
            "CCTV_BRIDGE_CLIENT_CERT": str(material), "CCTV_BRIDGE_CLIENT_KEY": str(material),
            "CCTV_BRIDGE_CA": str(material), "CCTV_INTERNAL_TLS": "1",
        }

    def test_plain_http_bridge_is_refused(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        env = self.base_env(tmp) | {"CCTV_BRIDGE_URL": "http://bridge"}
        with self.assertRaises(config.ConfigError):
            config.load(env)

    def test_half_configured_event_receiver_is_refused(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        env = self.base_env(tmp) | {"CCTV_EVENTS_CERT": str(tmp / "pem")}
        with self.assertRaises(config.ConfigError):
            config.load(env)

    def test_allow_list_must_be_numeric(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        with self.assertRaises(config.ConfigError):
            config.load(self.base_env(tmp) | {"CCTV_ALLOWED_USER_IDS": "@roman"})
        # Пустой allow-list — не отказ: владельца назначит мастер (/start <код>).
        cfg = config.load(self.base_env(tmp) | {"CCTV_ALLOWED_USER_IDS": "  "})
        self.assertEqual(frozenset(), cfg.allowed_user_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
