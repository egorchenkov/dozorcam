#!/usr/bin/env python3
"""Состояние cctv-tg-bot: камеры и их маршруты, темы, токены кнопок, дедуп событий.

Platform хранит только то, что нужно интерфейсу: камеры с именем, площадкой и
тегом локации, темы (`camera_id -> message_thread_id`, темы локаций), последние
посты событий для склейки, непрозрачные токены callback'ов с TTL, отметки
виденных `event_id` и подписки на движение. Ни адресов камер, ни учётных данных, ни URL здесь нет — по контракту
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
           "act", "actall",
           # карта и карточка камеры: открыть карточку, страница карты, тег локации;
           # смена пресета маршрутов (/mode); снять приглашённого (/invite);
           # шаг мастера «Сюда, в этот чат / В группу с темами»; инструкция
           # «Android / iPhone / Desktop» про темы и права в группе
           "card", "map", "loc", "mode", "kick", "where", "client")

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
    thread_id  INTEGER,
    created_at REAL NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0,
    chat_id    INTEGER
);
CREATE TABLE IF NOT EXISTS frame_replies (
    message_id INTEGER NOT NULL,
    camera_id  TEXT NOT NULL,
    thread_id  INTEGER,
    center_at  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    chat_id    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
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
CREATE TABLE IF NOT EXISTS cameras (
    camera_id  TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    site       TEXT NOT NULL DEFAULT '',
    location   TEXT,
    mode       TEXT,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS location_topics (
    location   TEXT PRIMARY KEY,
    thread_id  INTEGER NOT NULL UNIQUE,
    title      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS event_posts (
    camera_id   TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    thread_key  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    posted_at   REAL NOT NULL,
    first_at    TEXT NOT NULL,
    person      INTEGER NOT NULL DEFAULT 0,
    merged      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera_id, chat_id, thread_key)
);
CREATE TABLE IF NOT EXISTS event_messages (
    event_id   TEXT NOT NULL,
    chat_id    INTEGER NOT NULL,
    thread_key INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    fresh      INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    PRIMARY KEY (event_id, chat_id, thread_key)
);
CREATE TABLE IF NOT EXISTS screens (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    thread_key INTEGER NOT NULL,
    view       TEXT NOT NULL DEFAULT 'map',
    home       INTEGER NOT NULL DEFAULT 0,
    rendered   TEXT NOT NULL DEFAULT '',
    shown_at   REAL NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS members (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
"""
# Таблицы, у которых до 0.3.0 тема была обязательной (NOT NULL) и не было чата:
# в плоском режиме темы нет, а чатов несколько (личка каждого допущенного).
# Данные в них короткоживущие (заявки медиа, связи «кадр → момент»), поэтому
# миграция — копия в новую схему; чат старых строк — группа установки (у заявок
# NULL, у кадров 0: id сообщения уникален только внутри чата, он часть ключа).
MIGRATED_TABLES = ("media_requests", "frame_replies")


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
    thread_id: int | None
    chat_id: int | None = None


@dataclass(frozen=True)
class FrameReply:
    camera_id: str
    thread_id: int | None
    center_at: str
    expired: bool
    chat_id: int | None = None


@dataclass(frozen=True)
class CameraRecord:
    """Камера глазами бота: имя, площадка из реестра, тег локации и режим маршрута.

    `location` — явный тег (None — не задан, берётся площадка); `mode` — режим
    маршрута этой камеры (None — общий пресет установки).
    """
    camera_id: str
    title: str
    site: str
    location: str | None
    mode: str | None
    status: str


@dataclass(frozen=True)
class EventPost:
    """Последний пост события камеры в одном чате/теме — кандидат на склейку."""
    message_id: int
    posted_at: float
    first_at: str
    person: bool
    merged: int


@dataclass(frozen=True)
class Screen:
    """Сообщение-экран бота: карта камер или карточка одной камеры.

    Навигация «один экран»: нажатие на карте правит это же сообщение в карточку
    и обратно. `view` — что показано («map», «map:<локация>», «card:<camera_id>»),
    `home` — закреплённая карта своего места (пульт, плоский чат, личка).
    """
    chat_id: int
    message_id: int
    thread_id: int | None
    view: str
    home: bool
    rendered: str
    shown_at: float = 0.0


class State:
    def __init__(self, path, *, now=time.time) -> None:
        self._now = now
        first_time = not os.path.exists(path)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate_nullable_threads()
        self.db.executescript(SCHEMA)
        self._adopt_topic_cameras()
        if first_time and path != ":memory:":
            os.chmod(path, 0o600)

    def _migrate_nullable_threads(self) -> None:
        """Базы до 0.3.0: тема в заявках и кадрах обязательна, чата нет — перестроить."""
        for table in MIGRATED_TABLES:
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if not columns or "chat_id" in columns:
                continue
            self.db.execute(f"ALTER TABLE {table} RENAME TO {table}_pre030")
            self.db.executescript(SCHEMA)
            keep = ", ".join(sorted(columns))
            self.db.execute(f"INSERT INTO {table}({keep}) SELECT {keep} FROM {table}_pre030")
            self.db.execute(f"DROP TABLE {table}_pre030")

    def _adopt_topic_cameras(self) -> None:
        """Камеры, заведённые до маршрутов, известны только по темам: перенести их.

        Режим и локацию не трогаем (None): у установки без настроек пресет —
        «тема на камеру», и маршрут каждой камеры остаётся её прежней темой.
        """
        self.db.execute(
            "INSERT OR IGNORE INTO cameras(camera_id, title, site, status, created_at) "
            "SELECT camera_id, title, '', status, created_at FROM topics"
        )

    def close(self) -> None:
        self.db.close()

    def now(self) -> float:
        return self._now()

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
        self.note_camera(camera_id, title)
        topic = self.topic_for(camera_id)
        assert topic is not None
        return topic

    def rename_topic(self, camera_id: str, title: str) -> None:
        self.db.execute("UPDATE topics SET title=? WHERE camera_id=?", (title, camera_id))
        self.db.execute("UPDATE cameras SET title=? WHERE camera_id=?", (title, camera_id))

    def retire_topic(self, camera_id: str) -> Topic | None:
        """Камера снята: тема закрывается, но связка остаётся архивной."""
        self.db.execute(
            "UPDATE topics SET status='retired', closed_at=? WHERE camera_id=?",
            (self._now(), camera_id),
        )
        self.db.execute("UPDATE cameras SET status='retired' WHERE camera_id=?", (camera_id,))
        return self.topic_for(camera_id)

    def active_topics(self) -> list[Topic]:
        rows = self.db.execute(
            "SELECT camera_id, thread_id, title, status FROM topics "
            "WHERE status='active' ORDER BY camera_id"
        ).fetchall()
        return [Topic(**dict(row)) for row in rows]

    # --- камеры и маршруты ---------------------------------------------------
    def note_camera(self, camera_id: str, title: str, site: str | None = None) -> CameraRecord:
        """Запомнить камеру (имя, площадку) и считать её действующей."""
        if not valid_camera_id(camera_id):
            raise ValueError(f"invalid camera_id: {camera_id!r}")
        self.db.execute(
            "INSERT INTO cameras(camera_id, title, site, status, created_at) VALUES(?,?,?,'active',?) "
            "ON CONFLICT(camera_id) DO UPDATE SET title=excluded.title, status='active', "
            "site=CASE WHEN ? IS NULL THEN cameras.site ELSE excluded.site END",
            (camera_id, title or camera_id, site or "", self._now(), site),
        )
        record = self.camera(camera_id)
        assert record is not None
        return record

    def camera(self, camera_id: str) -> CameraRecord | None:
        row = self.db.execute(
            "SELECT camera_id, title, site, location, mode, status FROM cameras WHERE camera_id=?",
            (camera_id,),
        ).fetchone()
        return CameraRecord(**dict(row)) if row else None

    def active_cameras(self) -> list[CameraRecord]:
        rows = self.db.execute(
            "SELECT camera_id, title, site, location, mode, status FROM cameras "
            "WHERE status='active' ORDER BY camera_id"
        ).fetchall()
        return [CameraRecord(**dict(row)) for row in rows]

    def retire_camera(self, camera_id: str) -> None:
        self.db.execute("UPDATE cameras SET status='retired' WHERE camera_id=?", (camera_id,))

    def set_camera_location(self, camera_id: str, location: str | None) -> None:
        """Тег локации камеры; None — снять (тогда локация — площадка из реестра)."""
        self.db.execute("UPDATE cameras SET location=? WHERE camera_id=?",
                        (location if location else None, camera_id))

    def set_camera_mode(self, camera_id: str, mode: str | None) -> None:
        """Режим маршрута одной камеры; None — общий пресет установки."""
        self.db.execute("UPDATE cameras SET mode=? WHERE camera_id=?", (mode, camera_id))

    def location_topic(self, location: str) -> int | None:
        row = self.db.execute(
            "SELECT thread_id FROM location_topics WHERE location=?", (location,)
        ).fetchone()
        return row["thread_id"] if row else None

    def bind_location_topic(self, location: str, thread_id: int, title: str) -> None:
        if self.camera_for_thread(thread_id) is not None:
            raise ValueError(f"topic {thread_id} belongs to a camera: reuse is forbidden")
        self.db.execute(
            "INSERT INTO location_topics(location, thread_id, title, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(location) DO UPDATE SET thread_id=excluded.thread_id, title=excluded.title",
            (location, thread_id, title, self._now()),
        )

    def location_topics(self) -> list[tuple[str, int, str]]:
        rows = self.db.execute(
            "SELECT location, thread_id, title FROM location_topics ORDER BY location"
        ).fetchall()
        return [(row["location"], row["thread_id"], row["title"]) for row in rows]

    # --- склейка событий ------------------------------------------------------
    def event_post(self, camera_id: str, chat_id: int, thread_id: int | None,
                   window_sec: float) -> EventPost | None:
        """Пост события этой камеры в этом чате/теме, если ему меньше `window_sec`."""
        row = self.db.execute(
            "SELECT message_id, posted_at, first_at, person, merged FROM event_posts "
            "WHERE camera_id=? AND chat_id=? AND thread_key=?",
            (camera_id, chat_id, thread_id or 0),
        ).fetchone()
        if row is None or self._now() - row["posted_at"] >= window_sec:
            return None
        return EventPost(row["message_id"], row["posted_at"], row["first_at"],
                         bool(row["person"]), row["merged"])

    def remember_event_post(self, camera_id: str, chat_id: int, thread_id: int | None,
                            message_id: int, first_at: str, person: bool) -> None:
        """Новый пост события — начало окна склейки."""
        self.db.execute(
            "INSERT INTO event_posts(camera_id, chat_id, thread_key, message_id, posted_at, "
            "first_at, person, merged) VALUES(?,?,?,?,?,?,?,0) "
            "ON CONFLICT(camera_id, chat_id, thread_key) DO UPDATE SET "
            "message_id=excluded.message_id, posted_at=excluded.posted_at, "
            "first_at=excluded.first_at, person=excluded.person, merged=0",
            (camera_id, chat_id, thread_id or 0, message_id, self._now(), first_at, int(person)),
        )

    def merge_event_post(self, camera_id: str, chat_id: int, thread_id: int | None,
                         person: bool) -> None:
        """Ещё одно событие вклеено в пост: окно не сдвигается (отсчёт от первого)."""
        self.db.execute(
            "UPDATE event_posts SET merged=merged+1, person=MAX(person, ?) "
            "WHERE camera_id=? AND chat_id=? AND thread_key=?",
            (int(person), camera_id, chat_id, thread_id or 0),
        )

    def forget_event_post(self, camera_id: str, chat_id: int, thread_id: int | None) -> None:
        self.db.execute("DELETE FROM event_posts WHERE camera_id=? AND chat_id=? AND thread_key=?",
                        (camera_id, chat_id, thread_id or 0))

    # --- пост события в каждом месте: клип движения — ответом на него ----------
    def remember_event_message(self, event_id: str, chat_id: int, thread_id: int | None,
                               message_id: int, *, fresh: bool) -> None:
        """Пост, в котором событие показано в этом месте. `fresh` — свой пост
        (со своим звуком), иначе событие вклеено в чужой пост правкой (тихо)."""
        self.db.execute(
            "INSERT OR REPLACE INTO event_messages(event_id, chat_id, thread_key, message_id, "
            "fresh, created_at) VALUES(?,?,?,?,?,?)",
            (event_id, chat_id, thread_id or 0, message_id, int(fresh), self._now()),
        )

    def event_message(self, event_id: str, chat_id: int, thread_id: int | None) -> tuple[int, bool] | None:
        """(message_id, свой ли пост) события в этом месте или None."""
        row = self.db.execute(
            "SELECT message_id, fresh FROM event_messages WHERE event_id=? AND chat_id=? AND thread_key=?",
            (event_id, chat_id, thread_id or 0),
        ).fetchone()
        return (row["message_id"], bool(row["fresh"])) if row is not None else None

    # --- экраны: карта и карточки -------------------------------------------
    def _screen(self, row) -> Screen:
        return Screen(row["chat_id"], row["message_id"], row["thread_key"] or None,
                      row["view"], bool(row["home"]), row["rendered"], row["shown_at"])

    def remember_screen(self, chat_id: int, thread_id: int | None, message_id: int, view: str,
                        *, home: bool = False) -> None:
        """Сообщение показывает `view`. Карта места одна: новая снимает отметку со старой."""
        if home:
            self.db.execute("UPDATE screens SET home=0 WHERE chat_id=? AND thread_key=?",
                            (chat_id, thread_id or 0))
        self.db.execute(
            "INSERT INTO screens(chat_id, message_id, thread_key, view, home, rendered, shown_at) "
            "VALUES(?,?,?,?,?,'',?) ON CONFLICT(chat_id, message_id) DO UPDATE SET "
            "view=excluded.view, home=MAX(screens.home, excluded.home), rendered='', "
            "shown_at=excluded.shown_at",
            (chat_id, message_id, thread_id or 0, view, int(home), self._now()),
        )

    def screen(self, chat_id: int, message_id: int) -> Screen | None:
        row = self.db.execute("SELECT * FROM screens WHERE chat_id=? AND message_id=?",
                              (chat_id, message_id)).fetchone()
        return self._screen(row) if row else None

    def home_screen(self, chat_id: int, thread_id: int | None) -> Screen | None:
        row = self.db.execute("SELECT * FROM screens WHERE chat_id=? AND thread_key=? AND home=1",
                              (chat_id, thread_id or 0)).fetchone()
        return self._screen(row) if row else None

    def screens(self) -> list[Screen]:
        rows = self.db.execute("SELECT * FROM screens ORDER BY chat_id, message_id").fetchall()
        return [self._screen(row) for row in rows]

    def remember_screen_text(self, chat_id: int, message_id: int, rendered: str) -> None:
        self.db.execute("UPDATE screens SET rendered=? WHERE chat_id=? AND message_id=?",
                        (rendered, chat_id, message_id))

    def forget_screen(self, chat_id: int, message_id: int) -> None:
        self.db.execute("DELETE FROM screens WHERE chat_id=? AND message_id=?", (chat_id, message_id))

    def prune_screens(self, chat_id: int, keep: int) -> list[int]:
        """Карточки по /cam копятся в ленте: живыми держим `keep` последних в чате,
        старые остаются в ленте как есть, просто больше не перерисовываются."""
        rows = self.db.execute(
            "SELECT message_id FROM screens WHERE chat_id=? AND home=0 "
            "ORDER BY shown_at DESC, message_id DESC", (chat_id,)).fetchall()
        stale = [row["message_id"] for row in rows[keep:]]
        for message_id in stale:
            self.forget_screen(chat_id, message_id)
        return stale

    # --- люди без группы (/invite) -----------------------------------------------
    def issue_invite(self, ttl_sec: int) -> str:
        """Одноразовый код приглашения: ссылка t.me/<бот>?start=inv<код>."""
        code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        self.db.execute("INSERT INTO invites(code, expires_at) VALUES(?,?)",
                        (code, self._now() + ttl_sec))
        return code

    def take_invite(self, code: str) -> bool:
        """Погасить код: True — код был действующим. Повтор того же кода — False."""
        row = self.db.execute("SELECT expires_at FROM invites WHERE code=?", (code,)).fetchone()
        if row is None:
            return False
        self.db.execute("DELETE FROM invites WHERE code=?", (code,))
        return row["expires_at"] > self._now()

    def add_member(self, user_id: int, name: str = "") -> None:
        self.db.execute(
            "INSERT INTO members(user_id, name, added_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
            (user_id, name[:64], self._now()))

    def remove_member(self, user_id: int) -> bool:
        removed = self.db.execute("DELETE FROM members WHERE user_id=?", (user_id,)).rowcount
        self.db.execute("DELETE FROM motion_subs WHERE user_id=?", (user_id,))
        self.db.execute("DELETE FROM pending_input WHERE user_id=?", (user_id,))
        return bool(removed)

    def is_member(self, user_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM members WHERE user_id=?", (user_id,)).fetchone() is not None

    def members(self) -> list[tuple[int, str]]:
        rows = self.db.execute("SELECT user_id, name FROM members ORDER BY added_at, user_id").fetchall()
        return [(row["user_id"], row["name"]) for row in rows]

    # --- счётчик событий за день -----------------------------------------------
    def count_event(self, day: str) -> int:
        """+1 событие за `day` (дата в поясе установки); вчерашний счёт обнуляется."""
        saved = self.get_service("events_today") or ""
        saved_day, _, count = saved.partition("=")
        total = (int(count) if saved_day == day and count.isdigit() else 0) + 1
        self.set_service("events_today", f"{day}={total}")
        return total

    def events_on(self, day: str) -> int:
        saved_day, _, count = (self.get_service("events_today") or "").partition("=")
        return int(count) if saved_day == day and count.isdigit() else 0

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
    def remember_request(self, request_id: str, camera_id: str, kind: str, thread_id: int | None,
                         chat_id: int | None = None) -> bool:
        """False, если такой `request_id` уже принят: повтор не порождает второй пост.

        Чат и тема — куда вернуть результат: тому, кто попросил (в плоском режиме
        это личка этого человека). Чат None — группа установки (строки до 0.3.0).
        """
        try:
            self.db.execute(
                "INSERT INTO media_requests(request_id, camera_id, kind, thread_id, created_at, chat_id) "
                "VALUES(?,?,?,?,?,?)",
                (request_id, camera_id, kind, thread_id, self._now(), chat_id),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def take_request(self, request_id: str) -> PendingRequest | None:
        """Забрать неотданный запрос; повторный `media.ready` вернёт None."""
        row = self.db.execute(
            "SELECT request_id, camera_id, kind, thread_id, delivered, chat_id "
            "FROM media_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None or row["delivered"]:
            return None
        self.db.execute("UPDATE media_requests SET delivered=1 WHERE request_id=?", (request_id,))
        return PendingRequest(row["request_id"], row["camera_id"], row["kind"], row["thread_id"],
                              row["chat_id"])

    def purge_stale_requests(self, ttl_sec: int) -> int:
        """Отданные заявки и заявки без ответа Bridge копились весь срок жизни
        базы: их не забирает никто, кроме этой уборки."""
        cur = self.db.execute(
            "DELETE FROM media_requests WHERE created_at <= ?", (self._now() - ttl_sec,)
        )
        return cur.rowcount

    # --- reply на опубликованный кадр -----------------------------------
    def remember_frame(self, message_id: int, camera_id: str, thread_id: int | None,
                       center_at: str, ttl_sec: int, chat_id: int = 0) -> None:
        """Связать Telegram-photo с конкретным моментом камеры на ограниченное время.

        id сообщения уникален только внутри чата: в личках двух людей он
        совпадает, поэтому чат — часть ключа (0 — группа установки, как до 0.3.0).
        """
        self.db.execute(
            "INSERT INTO frame_replies(message_id, camera_id, thread_id, center_at, expires_at, chat_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id, message_id) DO UPDATE SET "
            "camera_id=excluded.camera_id, thread_id=excluded.thread_id, "
            "center_at=excluded.center_at, expires_at=excluded.expires_at",
            (message_id, camera_id, thread_id, center_at, self._now() + ttl_sec, chat_id or 0),
        )

    def resolve_frame_reply(self, message_id: int, chat_id: int = 0) -> FrameReply | None:
        row = self.db.execute(
            "SELECT camera_id, thread_id, center_at, expires_at, chat_id FROM frame_replies "
            "WHERE message_id=? AND chat_id=?",
            (message_id, chat_id or 0),
        ).fetchone()
        if row is None:
            return None
        return FrameReply(row["camera_id"], row["thread_id"], row["center_at"],
                          row["expires_at"] <= self._now(), row["chat_id"])

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
        # Связь «событие → пост» нужна клипу движения (через ~15 с) — живёт столько же.
        self.db.execute("DELETE FROM event_messages WHERE created_at <= ?", (self._now() - ttl_sec,))
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
