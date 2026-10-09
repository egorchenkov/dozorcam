"""Единый конфиг приложения: один каталог, по умолчанию /etc/cctv (только чтение).

Каталог содержит:

    config.toml    настройки; разделы [common], [engine], [bot]
    cameras.json   реестр камер (адреса и пароли камер — только здесь)
    secrets.toml   секреты бота (bot_token), режим 0600

Код движка и бота по-прежнему читает переменные CCTV_*: файл лишь подставляет
их значения. Ключ ``human_gate_mode`` раздела превращается в
``CCTV_HUMAN_GATE_MODE``; реальное окружение всегда сильнее файла. Разделы
нужны не для красоты: у моста и бота есть одноимённые переменные с разным
смыслом (CCTV_EVENTS_CERT — клиентский сертификат моста и серверный бота),
поэтому процесс получает только [common] и свой раздел.

Пути переопределяются переменными: CCTV_CONFIG_DIR, CCTV_STATE_DIR
(/var/lib/cctv/state — килобайты, на накопитель) и CCTV_BUFFER_DIR
(/var/lib/cctv/buffer — сегменты видео, tmpfs).
"""
from __future__ import annotations

import datetime
import os
import pathlib
import sys
import tomllib
import zoneinfo
from typing import MutableMapping

from . import i18n

DEFAULT_CONFIG_DIR = "/etc/cctv"
DEFAULT_STORAGE_ROOT = "/var/lib/cctv"
DEFAULT_STATE_DIR = DEFAULT_STORAGE_ROOT + "/state"
DEFAULT_BUFFER_DIR = DEFAULT_STORAGE_ROOT + "/buffer"
CONFIG_FILE = "config.toml"
SECRETS_FILE = "secrets.toml"
CAMERAS_FILE = "cameras.json"
COMPONENTS = ("engine", "bot")

# Внутренняя связь мост↔бот по умолчанию — loopback без TLS; порты свои, а не
# 443/8443, чтобы стенд и прод на одном узле не делили их случайно.
DEFAULT_BRIDGE_PORT = 8780
DEFAULT_EVENTS_PORT = 8781


class SettingsError(i18n.CodedError, RuntimeError):
    """Конфиг нечитаем или противоречив — запуск невозможен (ключи config.*)."""

    prefix = "config"


def config_dir(env: MutableMapping[str, str] | None = None) -> pathlib.Path:
    env = os.environ if env is None else env
    return pathlib.Path(env.get("CCTV_CONFIG_DIR") or DEFAULT_CONFIG_DIR)


def state_dir(env: MutableMapping[str, str] | None = None) -> pathlib.Path:
    env = os.environ if env is None else env
    return pathlib.Path(env.get("CCTV_STATE_DIR") or DEFAULT_STATE_DIR)


def buffer_dir(env: MutableMapping[str, str] | None = None) -> pathlib.Path:
    env = os.environ if env is None else env
    return pathlib.Path(env.get("CCTV_BUFFER_DIR") or DEFAULT_BUFFER_DIR)


def engine_state(storage: pathlib.Path) -> pathlib.Path:
    """Состояние движка: CCTV_STATE_DIR, иначе <storage>/state (раскладка тестов и прода)."""
    explicit = os.environ.get("CCTV_STATE_DIR")
    return pathlib.Path(explicit) if explicit else storage / "state"


def engine_buffer(storage: pathlib.Path) -> pathlib.Path:
    """Кольцевой буфер сегментов: CCTV_BUFFER_DIR, иначе <storage>/buffer."""
    explicit = os.environ.get("CCTV_BUFFER_DIR")
    return pathlib.Path(explicit) if explicit else storage / "buffer"


def internal_tls(env: MutableMapping[str, str] | None = None) -> bool:
    """mTLS между мостом и ботом — опция для разнесённой установки."""
    env = os.environ if env is None else env
    return (env.get("CCTV_INTERNAL_TLS") or "").strip().lower() in ("1", "true", "yes", "on")


def telegram_api(env: MutableMapping[str, str] | None = None) -> str:
    """Адрес Bot API: CCTV_TELEGRAM_API (свой telegram-bot-api, мок в CI), иначе api.telegram.org.

    Пустое значение (compose подставляет "" для незаполненной строки .env) — умолчание.
    """
    env = os.environ if env is None else env
    return (env.get("CCTV_TELEGRAM_API") or "https://api.telegram.org").strip().rstrip("/")


def time_zone(name: str | None = None) -> datetime.tzinfo | None:
    """Часовой пояс установки — CCTV_TZ (IANA, напр. ``Europe/Berlin``).

    Не задан или неизвестен — None: показываем UTC, а бот предупреждает в «Пульте».
    Это не TZ процесса: ffmpeg пишет имена сегментов под TZ=UTC, его не трогаем.
    """
    name = (os.environ.get("CCTV_TZ", "") if name is None else name).strip()
    if not name:
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None


def is_loopback_host(host: str | None) -> bool:
    import ipaddress

    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _scalar(key: str, value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, list) and all(isinstance(item, (int, float, str)) for item in value):
        return " ".join(str(item) for item in value)
    raise SettingsError("bad_value", key=key)


def _flatten(table: dict, source: str) -> dict[str, str]:
    values = {}
    for key, value in table.items():
        if isinstance(value, dict):
            continue  # разделы разбирает вызывающий
        name = key if key.startswith("CCTV_") else "CCTV_" + key.upper()
        values[name] = _scalar(f"{source}:{key}", value)
    return values


def _read_toml(path: pathlib.Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError("unreadable", path=str(path), reason=str(exc)) from exc


def load(component: str, env: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Значения CCTV_* для компонента из каталога конфига (без учёта окружения)."""
    if component not in COMPONENTS:
        raise SettingsError("unknown_component", name=repr(component))
    env = os.environ if env is None else env
    root = config_dir(env)
    values: dict[str, str] = {}
    for name in (CONFIG_FILE, SECRETS_FILE):
        path = root / name
        data = _read_toml(path)
        if not data:
            continue
        if name == SECRETS_FILE:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                print("[cctv] " + i18n.t("config.secrets_mode", i18n.env_lang(env),
                                         path=str(path), mode=f"{mode:04o}"), file=sys.stderr)
        unknown = [key for key, value in data.items()
                   if isinstance(value, dict) and key not in ("common", *COMPONENTS)]
        if unknown:
            raise SettingsError("unknown_sections", path=str(path), sections=", ".join(sorted(unknown)))
        values.update(_flatten(data, name))
        values.update(_flatten(data.get("common", {}), f"{name}[common]"))
        values.update(_flatten(data.get(component, {}), f"{name}[{component}]"))
    return values


def defaults(component: str, env: MutableMapping[str, str]) -> dict[str, str]:
    """Пути и адреса по умолчанию: всё на одном узле, связь по loopback."""
    root = config_dir(env)
    tls = internal_tls(env)
    scheme = "https" if tls else "http"
    result = {
        "CCTV_CAMERA_CONFIG": str(root / CAMERAS_FILE),
        "CCTV_STORAGE_ROOT": DEFAULT_STORAGE_ROOT,
        "CCTV_STATE_DIR": str(state_dir(env)),
        "CCTV_BUFFER_DIR": str(buffer_dir(env)),
    }
    if component == "engine":
        bind = env.get("CCTV_BIND") or "127.0.0.1"
        port = env.get("CCTV_PORT") or str(DEFAULT_BRIDGE_PORT)
        result.update({
            "CCTV_BIND": bind,
            "CCTV_PORT": port,
            "CCTV_PUBLIC_URL": f"{scheme}://{bind}:{port}",
            "CCTV_PROVISION_SOCKET": "/run/cctv/provision.sock",
            "CCTV_PROVISION_BACKUPS": str(state_dir(env) / "registry-backups"),
        })
        if not tls:
            # Без TLS приёмник бота — на том же узле; с TLS адрес задаётся явно.
            result["CCTV_EVENTS_URL"] = f"http://127.0.0.1:{DEFAULT_EVENTS_PORT}/v1/events"
    else:
        result.update({
            "CCTV_BRIDGE_URL": f"{scheme}://127.0.0.1:{DEFAULT_BRIDGE_PORT}",
            "CCTV_EVENTS_HOST": "127.0.0.1",
            "CCTV_EVENTS_PORT": str(DEFAULT_EVENTS_PORT),
        })
    return result


def apply(component: str, env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """Дописать в окружение значения из файла и умолчания; окружение не перетирается.

    Вызывается точкой входа ДО импорта модулей движка: они читают CCTV_* при импорте.
    """
    env = os.environ if env is None else env
    for key, value in load(component, env).items():
        env.setdefault(key, value)
    for key, value in defaults(component, env).items():
        env.setdefault(key, value)
    return env
