"""Связь агентов слоя: HTTP(S) + подпись HMAC-SHA256 общим секретом.

Подпись — поверх транспорта, а не вместо него: без TLS агенты слушают только
loopback (config.load это проверяет), между машинами — mTLS с CA установки.
Подписаны метод, путь, время и тело; запрос старше SKEW_SEC или уже виденный
(повтор той же подписи) отклоняется.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

from .config import Failover

SKEW_SEC = 60
TIME_HEADER = "X-Dozorcam-Time"
SIGN_HEADER = "X-Dozorcam-Sign"


class WireError(RuntimeError):
    """Агент не ответил или ответил не по протоколу."""


def sign(secret: bytes, method: str, path: str, stamp: str, body: bytes) -> str:
    message = b"\n".join((method.upper().encode(), path.encode(), stamp.encode(), body))
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class Verifier:
    """Проверка подписи с окном времени и памятью виденных подписей (повторы)."""

    def __init__(self, secret: bytes, clock=time.time) -> None:
        self.secret, self.clock = secret, clock
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def check(self, method: str, path: str, stamp: str | None, signature: str | None, body: bytes) -> bool:
        if not stamp or not signature:
            return False
        try:
            age = abs(self.clock() - float(stamp))
        except ValueError:
            return False
        if age > SKEW_SEC:
            return False
        if not hmac.compare_digest(sign(self.secret, method, path, stamp, body), signature):
            return False
        with self.lock:
            now = self.clock()
            self.seen = {key: until for key, until in self.seen.items() if until > now}
            if signature in self.seen:
                return False
            self.seen[signature] = now + 2 * SKEW_SEC
        return True


def client_context(cfg: Failover) -> ssl.SSLContext | None:
    """mTLS к соседу: свой сертификат установки и CA установки.

    Имя хоста не сверяется: CA выпущен только для этой установки, и один
    сертификат узлов не обязан перечислять адреса всех машин (docs/failover.md).
    """
    if not cfg.tls:
        return None
    context = ssl.create_default_context(cafile=cfg.tls_ca)
    context.check_hostname = False
    context.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    return context


def server_context(cfg: Failover) -> ssl.SSLContext | None:
    if not cfg.tls:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    context.load_verify_locations(cfg.tls_ca)
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class Client:
    def __init__(self, cfg: Failover, clock=time.time) -> None:
        self.cfg, self.clock = cfg, clock
        self.context = client_context(cfg)

    def call(self, base: str, method: str, path: str, payload: dict | None = None, *,
             timeout: float = 5.0, raw: bool = False):
        """JSON-ответ агента (или байты при raw); сбой — WireError."""
        body = b"" if payload is None else json.dumps(payload).encode()
        stamp = f"{self.clock():.6f}"
        headers = {TIME_HEADER: stamp, SIGN_HEADER: sign(self.cfg.secret, method, path, stamp, body)}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=body if method == "POST" else None,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.context) as reply:
                data = reply.read()
        except urllib.error.HTTPError as exc:
            raise WireError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise WireError(type(exc).__name__) from exc
        if raw:
            return data
        try:
            result = json.loads(data)
        except ValueError as exc:
            raise WireError("not json") from exc
        if not isinstance(result, dict):
            raise WireError("not an object")
        return result
