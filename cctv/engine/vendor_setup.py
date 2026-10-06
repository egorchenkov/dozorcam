#!/usr/bin/env python3
"""Кто производитель и ждёт ли камера первичной настройки — без единой попытки входа.

Автоактивация есть только у Hikvision (`hikvision_activation`). Остальным
вендорам мастер /add показывает инструкцию, как задать первый пароль вручную, —
для этого достаточно знать марку. Марка берётся из того, что камера отдаёт
любому: заголовок Server/WWW-Authenticate RTSP и HTTP, страница `GET /`.

Логин не пробуется ни в каком виде, даже пустым паролем: у Dahua, Uniview,
Hanwha неудачные входы считаются и блокируют учётку, а у новой камеры её ещё
нет — догадки здесь дешевле блокировки. Явный признак «ждёт настройки» без
пароля документирован только у Axis (VAPIX systemready: `needsetup`); у
остальных его нет или он закрыт SDK — тогда состояние «не знаем» (None).
Разбор по вендорам — docs/vendor-activation.md.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request

HTTP_TIMEOUT = 3.0
BODY_LIMIT = 64 * 1024

# Марка → признаки в баннере/странице (нижний регистр). Порядок важен: OEM-
# марки раньше платформ, чьи следы они несут в веб-интерфейсе. Признаки только
# характерные: «tp-link» или «web service» есть и у роутеров, а адрес с одним
# веб-интерфейсом и узнанной маркой попадает в поиск как камера.
BRAND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tantos", ("tantos",)),
    ("ezviz", ("ezviz",)),
    ("hikvision", ("hikvision", "hiwatch", "doc/page/login.asp", "ds-2cd")),
    ("imou", ("imou",)),
    ("dahua", ("dahua", "rpc2_login", "/rpc2", "dhvideowhmode")),
    ("uniview", ("uniview", "/lapi/")),
    ("axis", ("axis communications", "axis-cgi", "/axis-")),
    ("hanwha", ("hanwha", "wisenet", "samsung techwin", "/stw-cgi", "techwin")),
    ("reolink", ("reolink",)),
    ("vigi", ("vigi",)),
    ("milesight", ("milesight",)),
    ("tvt", ("nvms-9000", "tvtcctv")),
    ("xiongmai", ("xiongmai", "xmeye", "netsurveillance", "netsurveillancewebcam")),
    ("ajax", ("ajax systems",)),
)
# Марки, у которых бот умеет активировать сам.
AUTO_ACTIVATION = frozenset({"hikvision"})
KNOWN_BRANDS = frozenset(brand for brand, _ in BRAND_HINTS)


def brand_from_text(*texts: str) -> str:
    """Марка по баннерам/странице. Пусто — не узнали."""
    hay = " ".join(t for t in texts if t).lower()
    if not hay:
        return ""
    for brand, needles in BRAND_HINTS:
        if any(needle in hay for needle in needles):
            return brand
    return ""


def _request(url: str, *, data: bytes | None = None, timeout: float) -> tuple[int, dict, str]:
    headers = {"User-Agent": "cctv-discovery"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.status, {k.lower(): v for k, v in response.headers.items()},
                    response.read(BODY_LIMIT).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read(BODY_LIMIT).decode("utf-8", "replace") if exc.fp else ""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            ValueError):
        return 0, {}, ""


def web_fingerprint(base: str, *, timeout: float = HTTP_TIMEOUT) -> str:
    """Текст для поиска марки: заголовки и начало `GET /` (без авторизации).

    401 тоже годится: realm в WWW-Authenticate часто называет марку."""
    status, headers, body = _request(base.rstrip("/") + "/", timeout=timeout)
    if not status:
        return ""
    title = re.search(r"(?is)<title>(.*?)</title>", body)
    parts = [headers.get("server", ""), headers.get("www-authenticate", ""),
             title.group(1) if title else "", body]
    return " ".join(parts)


def axis_needs_setup(base: str, *, timeout: float = HTTP_TIMEOUT) -> bool | None:
    """VAPIX systemready.cgi отвечает без пароля: `needsetup: yes` — у камеры
    ещё нет учётки root/администратора (AXIS OS 10+ без пароля по умолчанию)."""
    payload = json.dumps({"apiVersion": "1.0", "method": "systemready"}).encode()
    status, _, body = _request(base.rstrip("/") + "/axis-cgi/systemready.cgi",
                               data=payload, timeout=timeout)
    if status != 200:
        return None
    try:
        data = json.loads(body).get("data") or {}
    except (ValueError, AttributeError):
        return None
    flag = str(data.get("needsetup", "")).lower()
    return {"yes": True, "no": False}.get(flag)


# Марка → проверка «ждёт первичной настройки» без пароля.
SETUP_CHECKS = {"axis": axis_needs_setup}


def needs_setup(brand: str, base: str, *, timeout: float = HTTP_TIMEOUT) -> bool | None:
    check = SETUP_CHECKS.get(brand)
    if check is None:
        return None
    try:
        return check(base, timeout=timeout)
    except Exception:  # чужая прошивка не должна ронять опрос сети
        return None
