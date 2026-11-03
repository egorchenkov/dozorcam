"""Пригодность узла к роли — проверка раз в check_sec, без протоколов камер.

Движок: достаёт ли узел камеры реестра. Это только TCP-соединение на порт
потока, закрываемое сразу: ни запроса RTSP, ни ONVIF — сессий камер armed-узел
не занимает (у камер лимит сессий). Бот: отвечает ли Bot API на getMe —
getMe не конфликтует с опросом getUpdates активного узла.
"""
from __future__ import annotations

import json
import pathlib
import socket
import urllib.error
import urllib.parse
import urllib.request

CONNECT_TIMEOUT_SEC = 2.0
DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}


def camera_endpoints(registry: pathlib.Path) -> list[tuple[str, int]]:
    """(хост, порт) основного потока каждой камеры; адреса без учётки."""
    try:
        data = json.loads(registry.read_text())
    except (OSError, ValueError):
        return []
    endpoints = []
    for camera in data.get("cameras", []) if isinstance(data, dict) else []:
        if not isinstance(camera, dict) or camera.get("enabled") is False:
            continue
        parts = urllib.parse.urlsplit(str(camera.get("rtsp_url") or ""))
        try:
            port = parts.port or DEFAULT_PORTS.get(parts.scheme, 554)
        except ValueError:
            continue
        if parts.hostname:
            endpoints.append((parts.hostname, port))
    return endpoints


def reach(host: str, port: int, timeout: float = CONNECT_TIMEOUT_SEC) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def cameras(registry: pathlib.Path, connect=reach) -> dict:
    endpoints = camera_endpoints(registry)
    ok = sum(1 for host, port in endpoints if connect(host, port))
    # Пустой реестр — узел пригоден: ему нечего не достать.
    return {"fit": ok > 0 or not endpoints, "ok": ok, "total": len(endpoints)}


def telegram(api: str, token: str, timeout: float = 10.0) -> dict:
    if not token:
        return {"fit": False, "reason": "no_token"}
    try:
        with urllib.request.urlopen(f"{api}/bot{token}/getMe", timeout=timeout) as reply:
            ok = bool(json.loads(reply.read()).get("ok"))
        return {"fit": ok, "reason": "" if ok else "rejected"}
    except urllib.error.HTTPError as exc:
        return {"fit": False, "reason": "rejected" if exc.code in (401, 404) else f"http_{exc.code}"}
    except (urllib.error.URLError, OSError, ValueError):
        return {"fit": False, "reason": "unreachable"}
