#!/usr/bin/env python3
"""Приёмник событий Bridge: `POST /v1/events` — loopback без TLS или mTLS (CCTV_INTERNAL_TLS).

Сервер намеренно на stdlib: у сервиса не должно быть ни лишних зависимостей, ни
поверхности «умного» фреймворка. Разбор события отделён от транспорта функцией
`normalize_event`, чтобы контракт проверялся тестом без сети и сертификатов.
"""
from __future__ import annotations

import json
import re
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
EVENT_TYPES = ("camera.registered", "camera.retired", "motion.detected", "media.ready",
               "media.failed")


class EventRejected(ValueError):
    """Событие не соответствует контракту v1 — пост не создаётся."""


@dataclass(frozen=True)
class Media:
    url: str
    sha256: str
    bytes: int | None
    content_type: str


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    camera_id: str
    occurred_at: str
    request_id: str | None = None
    source_event_id: str | None = None
    kind: str | None = None
    error: str | None = None
    captured_at: str | None = None
    title: str | None = None
    site: str | None = None
    source: str | None = None
    media: Media | None = None


def _text(payload: dict, key: str, *, required: bool = True) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        if required:
            raise EventRejected(f"поле {key} отсутствует или не строка")
        return ""
    return value.strip()


def _media(payload: dict, *, kind: str | None, scheme: str = "https") -> Media | None:
    raw = payload.get("snapshot") or payload.get("download")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EventRejected("блок медиа не объект")
    url = _text(raw, "url")
    # Схема ссылки — та же, что у самого моста: при mTLS открытый http не пройдёт.
    if not url.startswith(f"{scheme}://"):
        raise EventRejected(f"media URL обязан быть {scheme}")
    sha = _text(raw, "sha256", required=False) or _text(payload, "sha256", required=False)
    if not SHA256_RE.match(sha):
        raise EventRejected("sha256 обязателен и должен быть hex(64)")
    size = raw.get("bytes", payload.get("bytes"))
    if size is not None and (not isinstance(size, int) or size <= 0):
        raise EventRejected("bytes должен быть положительным целым")
    default_type = "video/mp4" if kind == "clip" else "image/jpeg"
    content_type = payload.get("content_type") or raw.get("content_type") or default_type
    return Media(url=url, sha256=sha.lower(), bytes=size, content_type=str(content_type))


def normalize_event(payload: object, *, media_scheme: str = "https") -> Event:
    """Привести тело запроса к `Event` или отвергнуть его с понятной причиной."""
    if not isinstance(payload, dict):
        raise EventRejected("тело события не JSON-объект")
    event_type = _text(payload, "type")
    if event_type not in EVENT_TYPES:
        raise EventRejected(f"неизвестный тип события: {event_type}")

    from .state import valid_camera_id  # локальный импорт: state не тянет транспорт

    camera_id = _text(payload, "camera_id")
    if not valid_camera_id(camera_id):
        raise EventRejected("camera_id не соответствует формату реестра")

    kind = payload.get("kind") if event_type in ("media.ready", "media.failed") else None
    if event_type == "media.ready" and kind not in ("snapshot", "clip"):
        raise EventRejected("media.ready требует kind=snapshot|clip")
    if kind is not None and kind not in ("snapshot", "clip"):
        kind = None
    # Отказ обязан нести причину: без неё бот не сможет объяснить пользователю тишину.
    if event_type == "media.failed" and not _text(payload, "error", required=False):
        raise EventRejected("media.failed без кода error")

    media = _media(payload, kind=kind, scheme=media_scheme)
    if event_type == "media.ready" and media is None:
        raise EventRejected("media.ready без блока download")

    return Event(
        event_id=_text(payload, "event_id"),
        type=event_type,
        camera_id=camera_id,
        occurred_at=_text(payload, "occurred_at", required=False)
        or _text(payload, "captured_at", required=False),
        request_id=_text(payload, "request_id", required=False) or None,
        # Клип, привязанный к движению: Bridge выдаёт его сам, без запроса бота.
        source_event_id=_text(payload, "source_event_id", required=False) or None,
        kind=kind,
        error=_text(payload, "error", required=False) or None,
        captured_at=_text(payload, "captured_at", required=False) or None,
        title=_text(payload, "title", required=False) or None,
        site=_text(payload, "site", required=False) or None,
        source=_text(payload, "source", required=False) or None,
        media=media,
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "cctv-events/1"
    sys_version = ""

    def do_POST(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        if self.path.rstrip("/") != "/v1/events":
            return self._reply(404, "not_found")
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._reply(400, "bad_request")
        if length <= 0 or length > MAX_BODY:
            return self._reply(413, "payload_too_large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            event = normalize_event(payload, media_scheme=self.server.media_scheme)
        except (ValueError, EventRejected) as exc:
            self.server.log_line(f"событие отклонено: {exc}")
            return self._reply(400, "bad_request")
        try:
            self.server.submit(event)
        except Exception as exc:  # приёмник не должен падать из-за обработчика
            self.server.log_line(f"обработчик события отказал: {type(exc).__name__}")
            return self._reply(503, "unavailable")
        self._reply(202, "accepted")

    def _reply(self, code: int, status: str) -> None:
        body = json.dumps({"status": status}).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # В журнал не уходят ни URL, ни тело: только факт обращения.
        self.server.log_line(f"events {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


class EventServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg, submit, log_line=lambda _m: None) -> None:
        super().__init__((cfg.events_host, cfg.events_port), _Handler)
        self.submit = submit
        self.log_line = log_line
        self.media_scheme = getattr(cfg, "bridge_scheme", "https")
        if not getattr(cfg, "internal_tls", True):
            # Loopback без TLS: границу держит адрес (config.load не пустит не-loopback).
            return
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cfg.events_cert), str(cfg.events_key))
        context.load_verify_locations(str(cfg.events_client_ca))
        # Без клиентского сертификата от CA Bridge соединение не состоится.
        context.verify_mode = ssl.CERT_REQUIRED
        self.socket = context.wrap_socket(self.socket, server_side=True)

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="cctv-events", daemon=True)
        thread.start()
        return thread
