"""Уведомление владельцу — личкой от самого бота видеонаблюдения.

Отдельного канала, сервиса доставки или чужого токена у приложения нет: тот же
бот, что ведёт темы камер, пишет владельцу в личные сообщения. Токен —
CCTV_BOT_TOKEN (``/etc/cctv/secrets.toml``), адресаты — CCTV_OWNER_IDS, а если
их нет — CCTV_ALLOWED_USER_IDS. Личка доходит только тому, кто хоть раз нажал
у бота «Старт»: Telegram не даёт боту писать первым.

Вызов (точка входа ``cctv-notify`` или ``python -m cctv notify``):

    cctv-notify --text '...' [--detail '...'] [--silent] [--dry-run]

Этим пользуются OnFailure-юниты: упавший мост бот видит лишь как «реестр
недоступен», а о самом боте — некому, кроме этой команды. Тот же текст в окне
CCTV_NOTIFY_WINDOW_S (по умолчанию 300 с) повторно не отправляется.

Коды возврата: 0 — доставлено хотя бы одному владельцу (или схлопнуто), 1 — не
доставлено никому, 2 — не настроено (нет токена или адресатов), 64 — аргументы.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

from . import i18n

PREFIX_KEY = "owner_notify.prefix"
TG_API = "https://api.telegram.org/bot{token}/{method}"
ATTEMPTS = 3
BACKOFF_S = (2, 8)


def prefix(env=None) -> str:
    """«🔴 Видеонаблюдение:» на языке установки (CCTV_LANG, иначе en)."""
    return i18n.t(PREFIX_KEY, i18n.env_lang(env))


def log(msg: str) -> None:
    print(f"[cctv-notify] {msg}", file=sys.stderr, flush=True)


def owner_ids(env=None) -> list[str]:
    env = os.environ if env is None else env
    raw = env.get("CCTV_OWNER_IDS") or env.get("CCTV_ALLOWED_USER_IDS") or ""
    return [chunk for chunk in raw.replace(",", " ").split() if chunk.lstrip("-").isdigit()]


def dedup_path(env=None) -> pathlib.Path:
    env = os.environ if env is None else env
    explicit = env.get("CCTV_NOTIFY_STATE")
    if explicit:
        return pathlib.Path(explicit)
    from .settings import state_dir

    return state_dir(env) / "notify-dedup.json"


def seen_recently(text: str, now: float, path: pathlib.Path, window: float) -> bool:
    """Уведомление — не сирена: тот же текст в окне — молчим."""
    if window <= 0:
        return False
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if now - float(state.get(digest, 0) or 0) < window:
        return True
    state = {key: value for key, value in state.items()
             if now - float(value or 0) < max(window * 10, 3600)}
    state[digest] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        log(f"состояние схлопывания не сохранено ({path}): {exc.__class__.__name__}")
    return False


def telegram_send(token: str, chat_id: str, text: str, silent: bool = False) -> str:
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "disable_notification": silent}).encode("utf-8")
    request = urllib.request.Request(TG_API.format(token=token, method="sendMessage"),
                                     data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8", "replace"))
    if not body.get("ok"):
        raise RuntimeError(str(body.get("description", "not ok")))
    return str(body.get("result", {}).get("message_id", ""))


def send_one(token: str, chat_id: str, text: str, silent: bool) -> bool:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            message_id = telegram_send(token, chat_id, text, silent)
        except (urllib.error.URLError, RuntimeError, OSError, ValueError) as exc:
            # Текст исключения urllib может нести URL с токеном — в журнал только класс.
            log(f"владелец {chat_id}: попытка {attempt} не удалась ({exc.__class__.__name__})")
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
            continue
        print(f"cctv-notify: owner={chat_id} result=sent message_id={message_id}")
        return True
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="notify the bot owner in a private chat")
    parser.add_argument("--text", required=True, help="human-readable text, no internals")
    parser.add_argument("--detail", default="", help="internals: process log only")
    parser.add_argument("--silent", action="store_true", help="no sound (information)")
    parser.add_argument("--dry-run", action="store_true")
    try:
        opts = parser.parse_args(argv)
    except SystemExit as exc:
        return 64 if exc.code else 0

    token = (os.environ.get("CCTV_BOT_TOKEN") or "").strip()
    owners = owner_ids()
    if not token or not owners:
        log("не настроено: нужны CCTV_BOT_TOKEN и CCTV_OWNER_IDS (или CCTV_ALLOWED_USER_IDS)")
        return 2
    if opts.detail:
        log(f"подробности: {opts.detail}")
    text = f"{prefix()} {opts.text}"
    window = float(os.environ.get("CCTV_NOTIFY_WINDOW_S", "300"))
    if seen_recently(text, time.time(), dedup_path(), window):
        print("cctv-notify: result=deduped")
        return 0
    if opts.dry_run:
        print(f"cctv-notify: result=dry-run owners={len(owners)}")
        return 0
    delivered = [owner for owner in owners if send_one(token, owner, text, opts.silent)]
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())
