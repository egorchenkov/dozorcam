"""Ссылка владельца для установщика: ``python -m cctv.bot.owner_link`` в контейнере бота.

Установщик и ``dozorcam code`` показывают ``https://t.me/<бот>?start=<код>`` и QR,
чтобы grep по журналу исчез из инструкции. Код берётся из состояния бота — тот же,
что в строке SETUP журнала, и только пока владельца нет (после первого /start его
нет и в журнале: старые строки SETUP там остаются, поэтому журнал не источник).
Имя бота — из getMe.

Печатает ``<код> <ссылка>`` (ссылки нет, если getMe недоступен). Коды выхода:
0 — код есть; 3 — владелец уже назначен (или задан allow-list), мастер не нужен;
4 — бот ещё не записал состояние (только что стартовал) — спросить позже.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request

from .. import settings
from .bot import OWNER_KEY, SETUP_CODE_KEY

OWNER_SET, NOT_READY = 3, 4


def read_service(db: str, *keys: str) -> dict[str, str] | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(f"SELECT key, value FROM service WHERE key IN ({','.join('?' * len(keys))})",
                           keys).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return {key: value for key, value in rows if value}


def bot_username(token: str) -> str:
    if not token:
        return ""
    try:
        with urllib.request.urlopen(f"{settings.telegram_api()}/bot{token}/getMe", timeout=10) as response:
            return json.load(response)["result"].get("username") or ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def main() -> int:
    try:
        settings.apply("bot")
    except settings.SettingsError as exc:
        print(exc, file=sys.stderr)
        return NOT_READY
    if (os.environ.get("CCTV_ALLOWED_USER_IDS") or "").strip():
        return OWNER_SET
    service = read_service(str(settings.state_dir() / "bot.sqlite3"), OWNER_KEY, SETUP_CODE_KEY)
    if service is None:
        return NOT_READY
    if OWNER_KEY in service:
        return OWNER_SET
    code = service.get(SETUP_CODE_KEY)
    if not code:
        return NOT_READY
    name = bot_username(os.environ.get("CCTV_BOT_TOKEN", ""))
    print(code, f"https://t.me/{name}?start={code}" if name else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
