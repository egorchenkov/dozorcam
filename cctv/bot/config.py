#!/usr/bin/env python3
"""Конфигурация бота: переменные CCTV_*, никаких секретов в репозитории.

Значения приходят из окружения, которое точка входа заполняет из каталога
конфига (/etc/cctv, см. cctv.settings). Связь с мостом по умолчанию — loopback
без TLS; CCTV_INTERNAL_TLS=1 включает mTLS для разнесённой установки.

Сервис детерминированный: нет LLM-клиента, нет proxy, нет свободного текста.
Поэтому и конфиг узкий — Telegram, Bridge, лимиты. Любое неизвестное значение
приводит к отказу старта, а не к «мягкой» работе с дырой в границе доступа.
"""
from __future__ import annotations

import os
import pathlib
import urllib.parse
from dataclasses import dataclass

from .. import i18n, settings

DEFAULT_STATE_DIR = settings.DEFAULT_STATE_DIR
DEFAULT_RUNTIME_DIR = settings.DEFAULT_BUFFER_DIR + "/bot"
DEFAULT_BRIDGE_URL = f"http://127.0.0.1:{settings.DEFAULT_BRIDGE_PORT}"


class ConfigError(i18n.CodedError, RuntimeError):
    """Старт невозможен: окружение задано неполно или противоречиво (ключи config.*)."""

    prefix = "config"


def _require(env: dict[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ConfigError("required", key=key)
    return value


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError("not_number", key=key, value=repr(raw)) from exc


def _allow_list(raw: str, key: str = "CCTV_ALLOWED_USER_IDS") -> frozenset[int]:
    users: set[int] = set()
    for chunk in raw.replace(",", " ").split():
        try:
            users.add(int(chunk))
        except ValueError as exc:
            raise ConfigError("not_user_id", key=key, value=repr(chunk)) from exc
    if not users:
        raise ConfigError("no_users", key=key)
    return frozenset(users)


def _existing_file(path: str, key: str) -> pathlib.Path:
    resolved = pathlib.Path(path).expanduser()
    if not resolved.is_file():
        raise ConfigError("file_missing", key=key, path=str(resolved))
    return resolved


@dataclass(frozen=True)
class Config:
    bot_token: str
    # Группа и владелец могут быть не заданы: тогда их назначает мастер
    # первого запуска (одноразовый код в журнале → /start <код> → группа) и
    # хранит в состоянии бота. Заданные здесь значения сильнее мастера.
    chat_id: int | None
    allowed_user_ids: frozenset[int]
    bridge_base_url: str
    bridge_client_cert: pathlib.Path | None
    bridge_client_key: pathlib.Path | None
    bridge_ca_bundle: pathlib.Path | None
    state_dir: pathlib.Path
    runtime_dir: pathlib.Path
    events_host: str = "127.0.0.1"
    events_port: int = settings.DEFAULT_EVENTS_PORT
    events_cert: pathlib.Path | None = None
    events_key: pathlib.Path | None = None
    events_client_ca: pathlib.Path | None = None
    max_snapshot_bytes: int = 8 * 1024 * 1024
    max_clip_bytes: int = 48 * 1024 * 1024
    clip_duration_sec: int = 30
    bridge_timeout_sec: int = 15
    # Ожидание ответа Telegram на отправку медиа. Клип основного потока Telegram
    # обрабатывает дольше дефолтных 5 с PTB: сообщение публикуется, а бот ловит
    # TimedOut и шлёт повтор — на стенде 02.10.2026 так выходило 2–3 копии клипа.
    tg_media_timeout_sec: int = 120
    callback_ttl_sec: int = 3600
    panel_callback_ttl_sec: int = 7 * 24 * 3600
    event_dedup_ttl_sec: int = 24 * 3600
    # Кому бот пишет в личку об авариях (мост молчит, служба упала).
    owner_ids: frozenset[int] = frozenset()
    internal_tls: bool = True
    # Язык интерфейса по умолчанию (en, ru, …); пусто — по language_code владельца.
    lang: str = ""

    @property
    def db_path(self) -> pathlib.Path:
        return self.state_dir / "bot.sqlite3"

    @property
    def bridge_scheme(self) -> str:
        return urllib.parse.urlsplit(self.bridge_base_url).scheme

    @property
    def events_enabled(self) -> bool:
        """Без TLS приёмник событий есть всегда (loopback), с TLS — только с тройкой mTLS."""
        return not self.internal_tls or self.events_cert is not None

    def max_bytes_for(self, kind: str) -> int:
        return self.max_clip_bytes if kind == "clip" else self.max_snapshot_bytes


def load(env: dict[str, str] | None = None) -> Config:
    """Собрать конфиг из окружения; отсутствующее обязательное — это отказ."""
    env = dict(os.environ if env is None else env)
    tls = settings.internal_tls(env)

    base_url = (env.get("CCTV_BRIDGE_URL") or "").strip().rstrip("/")
    if not base_url:
        if tls:
            raise ConfigError("required", key="CCTV_BRIDGE_URL")
        base_url = DEFAULT_BRIDGE_URL
    parts = urllib.parse.urlsplit(base_url)
    if tls and parts.scheme != "https":
        raise ConfigError("bridge_needs_https")
    if not tls:
        if parts.scheme != "http":
            raise ConfigError("bridge_needs_http")
        if not settings.is_loopback_host(parts.hostname):
            raise ConfigError("bridge_not_loopback")

    state_dir = pathlib.Path(env.get("CCTV_STATE_DIR") or DEFAULT_STATE_DIR)
    runtime_dir = pathlib.Path(env.get("CCTV_RUNTIME_DIR") or DEFAULT_RUNTIME_DIR)
    events_host = env.get("CCTV_EVENTS_HOST") or "127.0.0.1"

    events_cert = env.get("CCTV_EVENTS_CERT") if tls else None
    events_key = env.get("CCTV_EVENTS_KEY") if tls else None
    events_ca = env.get("CCTV_EVENTS_CLIENT_CA") if tls else None
    if any((events_cert, events_key, events_ca)) and not all((events_cert, events_key, events_ca)):
        raise ConfigError("events_triplet")
    if not tls and not settings.is_loopback_host(events_host):
        raise ConfigError("events_not_loopback")

    if tls:
        client_cert = _existing_file(_require(env, "CCTV_BRIDGE_CLIENT_CERT"), "CCTV_BRIDGE_CLIENT_CERT")
        client_key = _existing_file(_require(env, "CCTV_BRIDGE_CLIENT_KEY"), "CCTV_BRIDGE_CLIENT_KEY")
        ca_bundle = _existing_file(_require(env, "CCTV_BRIDGE_CA"), "CCTV_BRIDGE_CA")
    else:
        client_cert = client_key = ca_bundle = None

    allowed_raw = (env.get("CCTV_ALLOWED_USER_IDS") or "").strip()
    allowed = _allow_list(allowed_raw) if allowed_raw else frozenset()
    owners_raw = (env.get("CCTV_OWNER_IDS") or "").strip()
    owners = _allow_list(owners_raw, "CCTV_OWNER_IDS") if owners_raw else allowed
    chat_raw = (env.get("CCTV_CHAT_ID") or "").strip()
    try:
        chat_id = int(chat_raw) if chat_raw else None
    except ValueError as exc:
        raise ConfigError("not_number", key="CCTV_CHAT_ID", value=repr(chat_raw)) from exc

    return Config(
        bot_token=_require(env, "CCTV_BOT_TOKEN"),
        chat_id=chat_id,
        allowed_user_ids=allowed,
        bridge_base_url=base_url,
        bridge_client_cert=client_cert,
        bridge_client_key=client_key,
        bridge_ca_bundle=ca_bundle,
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        events_host=events_host,
        events_port=_int(env, "CCTV_EVENTS_PORT", settings.DEFAULT_EVENTS_PORT),
        events_cert=_existing_file(events_cert, "CCTV_EVENTS_CERT") if events_cert else None,
        events_key=_existing_file(events_key, "CCTV_EVENTS_KEY") if events_key else None,
        events_client_ca=_existing_file(events_ca, "CCTV_EVENTS_CLIENT_CA") if events_ca else None,
        max_snapshot_bytes=_int(env, "CCTV_MAX_SNAPSHOT_BYTES", 8 * 1024 * 1024),
        max_clip_bytes=_int(env, "CCTV_MAX_CLIP_BYTES", 48 * 1024 * 1024),
        clip_duration_sec=_int(env, "CCTV_CLIP_DURATION_SEC", 30),
        bridge_timeout_sec=_int(env, "CCTV_BRIDGE_TIMEOUT_SEC", 15),
        tg_media_timeout_sec=_int(env, "CCTV_TG_MEDIA_TIMEOUT_SEC", 120),
        callback_ttl_sec=_int(env, "CCTV_CALLBACK_TTL_SEC", 3600),
        panel_callback_ttl_sec=_int(env, "CCTV_PANEL_CALLBACK_TTL_SEC", 7 * 24 * 3600),
        owner_ids=owners,
        internal_tls=tls,
        lang=(env.get("CCTV_LANG") or "").strip(),
    )
