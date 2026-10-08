#!/usr/bin/env python3
"""Состояние cctv-tg-bot: соответствие камер темам, токены кнопок, дедуп событий.

Platform хранит только то, что нужно интерфейсу: `camera_id -> message_thread_id`,
непрозрачные токены callback'ов с TTL, отметки виденных `event_id` и подписки на
движение. Ни адресов камер, ни учётных данных, ни URL здесь нет — по контракту
Bridge они не покидают контур cctv.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ACTIONS = ("snap", "clip", "stat", "sub", "pause", "resume", "rename", "retire", "panel",
           # заведение и правка камеры из чата: поиск, кандидат, настройки,
           # переключение детекции людей, удаление из реестра
           "add", "cand", "setup", "detect", "drop",
           # мастер: ввод адреса камеры вручную (камера вне поиска)
           "addr",
           # модель детектора людей: меню, выбор семейства, выбор файла (смена)
           "model", "mfam", "mset",
           # порог детектора по камерам: меню, «Откалибровать», ручной ввод, снять ручной
           "thr", "thrcal", "thrset", "thrauto",
           # активация новых Hikvision: одна камера, «Активировать все»
           "act", "actall")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    camera_id  TEXT PRIMARY KEY,
    thread_id  INTEGER NOT NULL UNIQUE,
    title      TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    closed_at  REAL
);
CREATE TABLE IF NOT EXISTS panels (
    camera_id  TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL,
    rendered   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS callbacks (
    token      TEXT PRIMARY KEY,
    camera_id  TEXT NOT NULL,
    action     TEXT NOT NULL,
    center_at  TEXT,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media_requests (
    request_id TEXT PRIMARY KEY,
    camera_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    thread_id  INTEGER NOT NULL,
    created_at REAL NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frame_replies (
    message_id INTEGER PRIMARY KEY,
    camera_id  TEXT NOT NULL,
    thread_id  INTEGER NOT NULL,
    center_at  TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    seen_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS health (
    camera_id TEXT PRIMARY KEY,
    state     TEXT NOT NULL,
    since     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS service (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_input (
    user_id    INTEGER PRIMARY KEY,
    camera_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS motion_subs (
    user_id   INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    PRIMARY KEY (user_id, camera_id)
);
"""


def valid_camera_id(camera_id: str) -> bool:
    """`camera_id` — ключ на всю жизнь камеры, поэтому формат проверяется строго."""
    return bool(CAMERA_ID_RE.match(camera_id or ""))


@dataclass(frozen=True)
class Topic:
    camera_id: str
    thread_id: int
    title: str
    status: str


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    camera_id: str
    kind: str
    thread_id: int


@dataclass(frozen=True)
class FrameReply:
    camera_id: str
    thread_id: int
    center_at: str
    expired: bool


class State:
    def __init__(self, path, *, now=time.time) -> None:
        self._now = now
        first_time = not os.path.exists(path)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if first_time and path != ":memory:":
            os.chmod(path, 0o600)

    def close(self) -> None:
        self.db.close()

    # --- темы -----------------------------------------------------------
    def topic_for(self, camera_id: str) -> Topic | None:
        row = self.db.execute(
            "SELECT camera_id, thread_id, title, status FROM topics WHERE camera_id=?",
            (camera_id,),
        ).fetchone()
        return Topic(**dict(row)) if row else None

    def camera_for_thread(self, thread_id: int) -> str | None:
        row = self.db.execute(
            "SELECT camera_id FROM topics WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return row["camera_id"] if row else None

    def bind_topic(self, camera_id: str, thread_id: int, title: str) -> Topic:
        """Связать камеру с темой. Тема никогда не переиспользуется под другой id."""
        if not valid_camera_id(camera_id):
            raise ValueError(f"invalid camera_id: {camera_id!r}")
        existing = self.camera_for_thread(thread_id)
        if existing is not None and existing != camera_id:
            raise ValueError(
                f"topic {thread_id} already belongs to camera {existing}: reuse is forbidden"
            )
        self.db.execute(
            "INSERT INTO topics(camera_id, thread_id, title, status, created_at) "
            "VALUES(?,?,?,'active',?) "
            "ON CONFLICT(camera_id) DO UPDATE SET title=excluded.title, "
            "status='active', closed_at=NULL",
            (camera_id, thread_id, title, self._now()),
        )
        topic = self.topic_for(camera_id)
        assert topic is not None
        return topic

    def rename_topic(self, camera_id: str, title: str) -> None:
        self.db.execute("UPDATE topics SET title=? WHERE camera_id=?", (title, camera_id))

    def retire_topic(self, camera_id: str) -> Topic | None:
        """Камера снята: тема закрывается, но связка остаётся архивной."""
        self.db.execute(
            "UPDATE topics SET status='retired', closed_at=? WHERE camera_id=?",
            (self._now(), camera_id),
        )
        return self.topic_for(camera_id)

    def active_topics(self) -> list[Topic]:
        rows = self.db.execute(
            "SELECT camera_id, thread_id, title, status FROM topics "
            "WHERE status='active' ORDER BY camera_id"
        ).fetchall()
        return [Topic(**dict(row)) for row in rows]

    # --- закреплённая панель камеры ---------------------------------------
    def bind_panel(self, camera_id: str, message_id: int) -> None:
        self.db.execute(
            "INSERT INTO panels(camera_id, message_id) VALUES(?,?) "
            "ON CONFLICT(camera_id) DO UPDATE SET message_id=excluded.message_id, rendered=''",
            (camera_id, message_id),
        )

    def panel_for(self, camera_id: str) -> tuple[int, str] | None:
        row = self.db.execute(
            "SELECT message_id, rendered FROM panels WHERE camera_id=?", (camera_id,)
        ).fetchone()
        return (row["message_id"], row["rendered"]) if row else None

    def remember_panel_text(self, camera_id: str, rendered: str) -> None:
        """Текст последней отрисовки: Telegram отвергает edit без изменений."""
        self.db.execute("UPDATE panels SET rendered=? WHERE camera_id=?", (rendered, camera_id))

    # --- сторож состояния -------------------------------------------------
    def note_health(self, camera_id: str, state: str) -> bool:
        """Запомнить состояние камеры. True — если оно сменилось.

        Сторож сообщает только о переходах: «нет кадров» каждую минуту — это шум,
        от которого перестают читать тему, а поломка выглядит как тишина.
        """
        row = self.db.execute("SELECT state FROM health WHERE camera_id=?", (camera_id,)).fetchone()
        if row is not None and row["state"] == state:
            return False
        self.db.execute(
            "INSERT INTO health(camera_id, state, since) VALUES(?,?,?) "
            "ON CONFLICT(camera_id) DO UPDATE SET state=excluded.state, since=excluded.since",
            (camera_id, state, self._now()),
        )
        return True

    def health_of(self, camera_id: str) -> str | None:
        row = self.db.execute("SELECT state FROM health WHERE camera_id=?", (camera_id,)).fetchone()
        return row["state"] if row else None

    # --- служебные значения ------------------------------------------------
    def get_service(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM service WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_service(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO service(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
        )

    def delete_service(self, key: str) -> None:
        self.db.execute("DELETE FROM service WHERE key=?", (key,))

    # --- ожидание ввода ----------------------------------------------------
    def expect_input(self, user_id: int, camera_id: str, kind: str, ttl_sec: int) -> None:
        self.db.execute(
            "INSERT INTO pending_input(user_id, camera_id, kind, expires_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET camera_id=excluded.camera_id, "
            "kind=excluded.kind, expires_at=excluded.expires_at",
            (user_id, camera_id, kind, self._now() + ttl_sec),
        )

    def take_input(self, user_id: int) -> tuple[str, str] | None:
        """Ожидание одноразовое: следующий текст того же пользователя — обычный."""
        row = self.db.execute(
            "SELECT camera_id, kind, expires_at FROM pending_input WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        self.db.execute("DELETE FROM pending_input WHERE user_id=?", (user_id,))
        if row["expires_at"] <= self._now():
            return None
        return row["camera_id"], row["kind"]

    # --- токены кнопок ---------------------------------------------------
    def issue_callback(self, camera_id: str, action: str, ttl_sec: int,
                       center_at: str | None = None) -> str:
        """Выдать непрозрачный токен: в callback-data не попадают id/адреса камер."""
        if action not in ACTIONS:
            raise ValueError(f"unknown button action: {action!r}")
        token = secrets.token_urlsafe(12)
        self.db.execute(
            "INSERT INTO callbacks(token, camera_id, action, center_at, expires_at) VALUES(?,?,?,?,?)",
            (token, camera_id, action, center_at, self._now() + ttl_sec),
        )
        return token

    def resolve_callback(self, token: str) -> tuple[str, str, str | None] | None:
        row = self.db.execute(
            "SELECT camera_id, action, center_at, expires_at FROM callbacks WHERE token=?",
            (token,),
        ).fetchone()
        if row is None or row["expires_at"] <= self._now():
            return None
        return row["camera_id"], row["action"], row["center_at"]

    def purge_expired_callbacks(self) -> int:
        cur = self.db.execute("DELETE FROM callbacks WHERE expires_at <= ?", (self._now(),))
        return cur.rowcount

    # --- запросы медиа ---------------------------------------------------
    def remember_request(self, request_id: str, camera_id: str, kind: str, thread_id: int) -> bool:
        """False, если такой `request_id` уже принят: повтор не порождает второй пост."""
        try:
            self.db.execute(
                "INSERT INTO media_requests(request_id, camera_id, kind, thread_id, created_at) "
                "VALUES(?,?,?,?,?)",
                (request_id, camera_id, kind, thread_id, self._now()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def take_request(self, request_id: str) -> PendingRequest | None:
        """Забрать неотданный запрос; повторный `media.ready` вернёт None."""
        row = self.db.execute(
            "SELECT request_id, camera_id, kind, thread_id, delivered "
            "FROM media_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None or row["delivered"]:
            return None
        self.db.execute("UPDATE media_requests SET delivered=1 WHERE request_id=?", (request_id,))
        return PendingRequest(row["request_id"], row["camera_id"], row["kind"], row["thread_id"])

    def purge_stale_requests(self, ttl_sec: int) -> int:
        """Отданные заявки и заявки без ответа Bridge копились весь срок жизни
        базы: их не забирает никто, кроме этой уборки."""
        cur = self.db.execute(
            "DELETE FROM media_requests WHERE created_at <= ?", (self._now() - ttl_sec,)
        )
        return cur.rowcount

    # --- reply на опубликованный кадр -----------------------------------
    def remember_frame(self, message_id: int, camera_id: str, thread_id: int,
                       center_at: str, ttl_sec: int) -> None:
        """Связать Telegram-photo с конкретным моментом камеры на ограниченное время."""
        self.db.execute(
            "INSERT INTO frame_replies(message_id, camera_id, thread_id, center_at, expires_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET "
            "camera_id=excluded.camera_id, thread_id=excluded.thread_id, "
            "center_at=excluded.center_at, expires_at=excluded.expires_at",
            (message_id, camera_id, thread_id, center_at, self._now() + ttl_sec),
        )

    def resolve_frame_reply(self, message_id: int) -> FrameReply | None:
        row = self.db.execute(
            "SELECT camera_id, thread_id, center_at, expires_at FROM frame_replies WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return FrameReply(row["camera_id"], row["thread_id"], row["center_at"],
                          row["expires_at"] <= self._now())

    def purge_expired_frames(self) -> int:
        cur = self.db.execute("DELETE FROM frame_replies WHERE expires_at <= ?", (self._now(),))
        return cur.rowcount

    # --- дедупликация событий -------------------------------------------
    def is_new_event(self, event_id: str) -> bool:
        try:
            self.db.execute(
                "INSERT INTO seen_events(event_id, seen_at) VALUES(?,?)",
                (event_id, self._now()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def forget_event(self, event_id: str) -> None:
        """Снять отметку о событии: доставка сорвалась, повтор моста обязан пройти.

        Архив живёт в теме Telegram — событие, не доехавшее до неё, не существует
        нигде. Дедупликация не должна превращать сбой сети в дыру в архиве.
        """
        self.db.execute("DELETE FROM seen_events WHERE event_id=?", (event_id,))

    def purge_seen_events(self, ttl_sec: int) -> int:
        cur = self.db.execute(
            "DELETE FROM seen_events WHERE seen_at <= ?", (self._now() - ttl_sec,)
        )
        return cur.rowcount

    # --- подписки на движение --------------------------------------------
    def toggle_motion(self, user_id: int, camera_id: str) -> bool:
        """Вернуть новое состояние подписки владельца на движение этой камеры."""
        row = self.db.execute(
            "SELECT 1 FROM motion_subs WHERE user_id=? AND camera_id=?", (user_id, camera_id)
        ).fetchone()
        if row:
            self.db.execute(
                "DELETE FROM motion_subs WHERE user_id=? AND camera_id=?", (user_id, camera_id)
            )
            return False
        self.db.execute(
            "INSERT INTO motion_subs(user_id, camera_id) VALUES(?,?)", (user_id, camera_id)
        )
        return True

    def motion_enabled(self, user_id: int, camera_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM motion_subs WHERE user_id=? AND camera_id=?", (user_id, camera_id)
        ).fetchone() is not None

    def motion_subscribers(self, camera_id: str) -> list[int]:
        rows = self.db.execute(
            "SELECT user_id FROM motion_subs WHERE camera_id=? ORDER BY user_id", (camera_id,)
        ).fetchall()
        return [row["user_id"] for row in rows]
