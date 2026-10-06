#!/usr/bin/env python3
"""Логика cctv-tg-bot: темы, кнопки, публикация кадров и событий движения.

Ничего свободного: любой неизвестный текст получает подсказку `/menu` и никуда
не передаётся. Каждый callback заново проверяет allow-list и принадлежность
камеры, каждое действие подтверждается немедленно и имеет идемпотентный
`request_id`.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .. import i18n
from .bridge import Bridge, BridgeError
from .events import Event
from .state import State

# Тексты живут в каталогах i18n (cctv/i18n/locales/*.json): таблицы ниже держат
# ключи каталога, текст на языке бота получается через `CctvBot._t`.
ACTION_TITLES = {
    "snap": "action.snap",
    "clip": "action.clip",
    "stat": "action.stat",
}
ERROR_TEXT = {
    "camera_offline": "error.camera_offline",
    "not_found": "error.not_found",
    "timeout": "error.timeout",
    "media_too_large": "error.media_too_large",
    "unavailable": "error.unavailable",
    "clip_window_empty": "error.clip_window_empty",
    "storage_capacity": "error.storage_full",
    "storage_full": "error.storage_full",
}
# Незнакомый код не должен превращаться в молчание: причина всё равно попадёт в тему.
MOSCOW = ZoneInfo("Europe/Moscow")
# Архив живёт в темах Telegram: событие, не доехавшее до темы, не существует
# нигде. Поэтому отправка повторяется, а неудача снимает отметку дедупликации.
DELIVERY_ATTEMPTS = 3
DELIVERY_BACKOFF_SEC = (3, 12)
# Telegram разбирает присланное видео сам только пока файл невелик: замер
# 21.09.2026 на клипах камеры G5 — 9.96 МБ разобран (width 2688, height 1520,
# duration 27), 11.4 МБ принят «как есть» и вернулся как video 320x320
# duration=0. Клиент рисует по этим числам квадрат, и клип 16:9 выглядит
# сплющенным. Камеры G5 пишут 2688x1520, 30 с такого потока — это 11–24 МБ,
# то есть у новых камер порог перейдён всегда. Поэтому геометрию, длительность
# и превью считаем сами и передаём явно: с ними Telegram берёт наши числа и
# размер файла роли уже не играет.
VIDEO_PROBE_TIMEOUT_SEC = 20
CLIP_FILENAME = "clip.mp4"  # имя клипа в Telegram, от него зависит mime
VIDEO_THUMB_WIDTH = 320  # лимит Telegram для thumbnail — 320 px и 200 КБ
CONSOLE_KEY = "console_thread"
CONSOLE_PANEL_KEY = "console_panel"
CONSOLE_CAMERA = "console"
CONSOLE_TITLE = "console.title"
# Иконки тем берутся из набора getForumTopicIconStickers — он доступен ботам без
# premium. Камеры своей иконки в наборе нет, поэтому наблюдение — «глаза».
TOPIC_ICON_CAMERA = "5357121491508928442"    # 👀 — площадка неизвестна
TOPIC_ICON_CONSOLE = "5350554349074391003"  # 💻
# Площадка узнаётся по имени темы и названию площадки, а не по camera_id: на
# даче камер будет несколько, и каждая новая должна получать иконку дачи
# сама, без правки кода. Порядок важен — совпадает первое вхождение.
SITE_ICONS = (
    ("дач", "5312486108309757006"),   # 🏠 дача
    ("dacha", "5312486108309757006"),
    ("house", "5312486108309757006"),
    ("город", "5350548830041415279"),  # 🏛 город
    ("city", "5350548830041415279"),
)
ICONS_KEY = "topic_icons_v2"
# Мастер первого запуска: владелец, группа, язык и одноразовый код — в state.
OWNER_KEY = "owner_id"
CHAT_KEY = "chat_id"
LANG_KEY = "lang"
SETUP_CODE_KEY = "setup_code"
# Буквы без похожих (0/O, 1/I/L): код переписывают из журнала руками.
SETUP_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SETUP_CODE_LEN = 10
# Перебор кода: после стольких неверных попыток пользователь больше не пробует.
SETUP_CODE_ATTEMPTS = 5
# После записи в реестр ждём, пока новая камера даст кадры, и шлём первый снимок.
FIRST_FRAME_WAIT_SEC = 120
FIRST_FRAME_POLL_SEC = 5
INPUT_TTL_SEC = 300
# Опрос сети идёт на мосту; бот только ждёт результата и показывает его.
SCAN_POLL_SEC = 3
# Смена модели: движок собирает и проверяет детекторы всех камер, прежде чем
# подменить, — на 8 камерах это секунды; ждём с запасом.
MODEL_POLL_SEC = 1
MODEL_WAIT_SEC = 120
MODEL_FILES_SHOWN = 12
# Ввод ручного порога: «auto» снимает его и возвращает автокалибровку.
THRESHOLD_AUTO_WORDS = ("auto", "авто", "automatic", "автоматически")
SCAN_WAIT_SEC = 180
# Активация новых Hikvision: камера — до минуты (RSA-ключ, активация, ONVIF,
# проба потока), пакет до 16 камер. Мост ведёт задание сам, бот только ждёт.
ACTIVATION_POLL_SEC = 3
ACTIVATION_WAIT_SEC = 900
ACTIVATION_MAX_HOSTS = 16
# Запись в реестр перезапускает цепочку моста: до сверки тем даём ей встать.
RESTART_WAIT_SEC = 25
STATUS_TEXT = {"online": "status.online", "paused": "status.paused",
               "retired": "status.retired", "unavailable": "status.unavailable"}
CONSOLE_STATE_MARK = {"ok": "🟢", "no_frames": "🔴", "paused": "⏸", "retired": "🗑",
                      "detector_stalled": "🟠", "detector_blind": "🟠", "detector_disabled": "🟠",
                      "detector_behind": "🟡"}
CONSOLE_STATE_TEXT = {state: f"console.state.{state}" for state in (
    "ok", "no_frames", "paused", "retired",
    "detector_stalled", "detector_blind", "detector_disabled", "detector_behind")}
HEALTH_TEXT = {state: f"health.{state}" for state in (
    "ok", "no_frames", "detector_stalled", "detector_blind", "detector_disabled", "detector_behind")}
# Мост целиком — авария всего видеонаблюдения, а не одной камеры: о ней бот пишет
# владельцу в личку (отдельного канала оповещений у приложения нет). Один
# пропущенный опрос — ещё не авария: мост мог перезапускаться.
BRIDGE_DOWN_AFTER = 2
BRIDGE_DOWN_KEY = "bridge.down"
BRIDGE_UP_KEY = "bridge.up"
BRIDGE_HEALTH_KEY = "_bridge"
DEFAULT_ERROR_TEXT = "error.default"
UNKNOWN_KEY = "hint.unknown"
# Вторая строка у каждой просьбы не для красоты: многострочный ответ на кнопку
# Telegram показывает окном с «ОК», однострочный — исчезающей подсказкой.
INPUT_PROMPTS = {"rename": "input.rename", "retire": "input.retire",
                 "drop": "input.drop", "creds": "input.creds"}
# Ввод, в котором приходит секрет: сообщение стирается до любого сетевого вызова.
SECRET_INPUTS = ("creds", "actpw")
# Слово подтверждения снятия/удаления: принимается на любом языке каталога.
CONFIRM_WORDS = {"retire": "confirm.retire", "drop": "confirm.drop",
                 "activate": "confirm.activate", "generate": "confirm.generate"}
# Итог активации по камере → строка каталога.
ACTIVATION_OUTCOME_TEXT = {"activated": "act.outcome.activated",
                           "unverified": "act.outcome.unverified",
                           "already_active": "act.outcome.already_active",
                           "failed": "act.outcome.failed"}
ACTIVATION_STAGE_TEXT = {stage: f"act.stage.{stage}" for stage in (
    "not_hikvision", "no_answer", "challenge_refused", "challenge_format", "challenge_decrypt",
    "activate_refused", "not_applied", "login_refused", "login_failed", "onvif_enable",
    "onvif_user", "onvif_login", "stream", "internal")}
# Протоколы, которыми бот активирует сам (Hikvision); прочие новые камеры
# получают инструкцию по марке (vendor_setup в движке, docs/vendor-activation.md).
AUTO_ACTIVATION_PROTOCOLS = ("v3", "legacy")
MANUAL_SETUP_BRANDS = ("dahua", "imou", "uniview", "tantos", "axis", "hanwha", "reolink",
                       "vigi", "ezviz", "milesight", "tvt", "xiongmai", "ajax", "hikvision")
BRAND_NAMES = {"dahua": "Dahua", "imou": "Imou", "uniview": "Uniview", "tantos": "Tantos",
               "axis": "Axis", "hanwha": "Hanwha Vision (Wisenet)", "reolink": "Reolink",
               "vigi": "TP-Link VIGI", "ezviz": "EZVIZ", "milesight": "Milesight",
               "tvt": "TVT", "xiongmai": "Xiongmai (XMEye)", "ajax": "Ajax",
               "hikvision": "Hikvision"}
ACTIVATION_PASSWORD_ERRORS = ("password_length", "password_charset", "password_weak",
                              "password_has_user")
# Постоянная клавиатура одна на весь чат, поэтому подписи не содержат камеры:
# камеру определяет тема, в которую пришло нажатие.
# Ключ подписи → действие. Подписи зависят от языка, а клавиатура у поля ввода
# остаётся старой после /lang, поэтому узнаём подписи всех языков каталога.
KEYBOARD_ACTIONS = {"action.snap": "snap", "action.clip": "clip",
                    "action.stat": "stat", "action.sub_on": "sub"}
KEYBOARD_ROWS = (("action.snap", "action.clip"), ("action.stat", "action.sub_on"))
MOTION_STATE_TEXT = {state: f"motion.{state}" for state in (
    "watching", "degraded", "behind", "blind", "disabled", "stalled", "unknown")}


def keyboard_action(text: str | None) -> str | None:
    """Действие по подписи постоянной кнопки на любом языке каталога."""
    label = (text or "").strip()
    for key, action in KEYBOARD_ACTIONS.items():
        if any(label == i18n.t(key, lang) for lang in i18n.available()):
            return action
    return None


def confirm_words(key: str) -> set[str]:
    """Слово подтверждения на всех языках каталога, в нижнем регистре."""
    return {i18n.t(key, lang).lower() for lang in i18n.available()}


async def _run_tool(command: tuple[str, ...], log) -> bytes | None:
    """Запустить ffprobe/ffmpeg и вернуть stdout; любой сбой — None, не исключение.

    Доставка клипа важнее его метаданных: без ffmpeg (или при его ошибке) видео
    всё равно должно уехать в тему, просто без подсказки о геометрии.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        log(f"{command[0]} не запустился: {type(exc).__name__}")
        return None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), VIDEO_PROBE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        process.kill()
        log(f"{command[0]} не уложился в {VIDEO_PROBE_TIMEOUT_SEC} с")
        return None
    return out if process.returncode == 0 else None


async def probe_video(path: str, log) -> dict:
    """width/height/duration для sendVideo; пустой словарь — если не выяснили."""
    out = await _run_tool(("ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=width,height",
                           "-show_entries", "format=duration",
                           "-of", "json", path), log)
    if not out:
        return {}
    try:
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = int(round(float(data.get("format", {}).get("duration") or 0)))
    except (ValueError, KeyError, IndexError, TypeError):
        return {}
    if width <= 0 or height <= 0:
        return {}
    meta = {"width": width, "height": height}
    if duration > 0:
        meta["duration"] = duration
    return meta


async def make_thumbnail(path: str, log) -> str | None:
    """Кадр-превью рядом с клипом: без него у большого видео нет и обложки."""
    target = f"{path}.thumb.jpg"
    out = await _run_tool(("ffmpeg", "-y", "-loglevel", "error", "-i", path,
                           "-frames:v", "1", "-vf", f"scale={VIDEO_THUMB_WIDTH}:-2",
                           target), log)
    if out is None or not os.path.isfile(target):
        return None
    return target


@contextlib.contextmanager
def open_thumbnail(path: str | None):
    """Файл миниатюры или None — вызывающему не нужно знать, получилась ли она."""
    if not path:
        yield None
        return
    try:
        handle = open(path, "rb")
    except OSError:
        yield None
        return
    try:
        yield handle
    finally:
        handle.close()


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_time(value: str | None, lang: str = "ru") -> str:
    """Показанное время — московское и читаемое.

    По проводу метки ходят в UTC (`...Z` или `+00:00`) — так их и оставляем: на
    них завязаны клипы и токены. Пересчёт только на границе с человеком, иначе
    время события в теме приходилось складывать с тремя часами в уме.
    """
    if not value:
        return i18n.t("time.unknown", lang)
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value  # незнакомый формат лучше показать как есть, чем потерять
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(MOSCOW).strftime(i18n.t("time.format", lang))


TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
            "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
            "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
            "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
            "ю": "yu", "я": "ya"}


def slugify(title: str) -> str:
    """`camera_id` из имени камеры: он живёт в путях и логах, поэтому латиница.

    Такая же функция есть у моста в `camera_discovery`. Дублирование намеренное:
    бот и мост — разные пользователи и разные каталоги деплоя, общей библиотеки
    между ними нет и заводить её ради двадцати строк дороже, чем повторить.
    """
    out = []
    for char in (title or "").strip().lower():
        if char in TRANSLIT:
            out.append(TRANSLIT[char])
        elif char.isascii() and char.isalnum():
            out.append(char)
        else:
            out.append("-")
    slug = "-".join(part for part in "".join(out).split("-") if part)[:48]
    return slug if slug and slug[0].isalnum() else ""


def unique_camera_id(base: str, taken) -> str:
    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    return f"{base}-{uuid.uuid4().hex[:6]}"


def topic_icon(*hints: str) -> str:
    """Иконка темы по площадке: дача, город или нейтральные «глаза»."""
    haystack = " ".join(h for h in hints if h).lower()
    for needle, icon in SITE_ICONS:
        if needle in haystack:
            return icon
    return TOPIC_ICON_CAMERA


def resolve_lang(code: str | None) -> str:
    """language_code Telegram (ru, pt-br, en-US…) → язык из каталога, иначе en."""
    available = {name.lower(): name for name in i18n.available()}
    raw = (code or "").strip().replace("_", "-").lower()
    if raw in available:
        return available[raw]
    base = raw.split("-")[0]
    if base in available:
        return available[base]
    # pt → pt-BR: другого португальского каталога нет
    family = next((name for key, name in sorted(available.items()) if key.split("-")[0] == base), None)
    return family or i18n.DEFAULT_LANG


def new_setup_code() -> str:
    import secrets

    return "".join(secrets.choice(SETUP_CODE_ALPHABET) for _ in range(SETUP_CODE_LEN))


@dataclass(frozen=True)
class Button:
    text: str
    data: str


class CctvBot:
    """Ядро без PTB: телеграм-объект внедряется, поэтому логика тестируется без сети."""

    def __init__(self, cfg, state: State, bridge: Bridge, tg, log=lambda _m: None) -> None:
        self.cfg = cfg
        self.state = state
        self.bridge = bridge
        self.tg = tg
        self.log = log
        self._topic_lock = asyncio.Lock()
        self._bridge_failures = 0
        self._code_attempts: dict[int, int] = {}
        self._activation_task: asyncio.Task | None = None

    # --- доступ -----------------------------------------------------------
    def allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in self.cfg.allowed_user_ids or user_id == self.owner_id

    @property
    def owner_id(self) -> int | None:
        """Владелец, назначенный мастером (`/start <код>`); из конфига — allow-list."""
        saved = self.state.get_service(OWNER_KEY)
        return int(saved) if saved else None

    @property
    def chat_id(self) -> int | None:
        """Группа: из конфига (CCTV_CHAT_ID) или привязанная мастером."""
        configured = getattr(self.cfg, "chat_id", None)
        if configured is not None:
            return configured
        saved = self.state.get_service(CHAT_KEY)
        return int(saved) if saved else None

    @property
    def lang(self) -> str:
        """Язык интерфейса: выбранный командой /lang → CCTV_LANG → en."""
        return resolve_lang(self.state.get_service(LANG_KEY) or getattr(self.cfg, "lang", ""))

    def _t(self, key: str, **params) -> str:
        return i18n.t(key, self.lang, **params)

    def _error(self, code: str | None) -> str:
        """Текст ошибки моста по коду; незнакомый код — общий текст сбоя."""
        return self._t(ERROR_TEXT.get(code or "", DEFAULT_ERROR_TEXT))

    def _time(self, value: str | None) -> str:
        return human_time(value, self.lang)

    # --- темы -------------------------------------------------------------
    async def ensure_topic(self, camera_id: str, title: str, site: str = "") -> int:
        """Одна тема на камеру. Повторная регистрация идемпотентна."""
        if self.chat_id is None:
            raise RuntimeError("группа не привязана — тему камеры завести негде")
        async with self._topic_lock:
            topic = self.state.topic_for(camera_id)
            if topic is not None:
                if title and title != topic.title:
                    self.state.rename_topic(camera_id, title)
                    await self.tg.edit_forum_topic(
                        chat_id=self.chat_id, message_thread_id=topic.thread_id, name=title,
                        icon_custom_emoji_id=topic_icon(title, site, camera_id),
                    )
                return topic.thread_id

            created = await self.tg.create_forum_topic(
                chat_id=self.chat_id, name=title or camera_id,
                icon_custom_emoji_id=topic_icon(title, site, camera_id),
            )
            thread_id = int(getattr(created, "message_thread_id", None) or created["message_thread_id"])
            self.state.bind_topic(camera_id, thread_id, title or camera_id)
            passport = await self.tg.send_message(
                chat_id=self.chat_id,
                message_thread_id=thread_id,
                text=self.passport_text(camera_id, title, site, "registered"),
                reply_markup=self.control_markup(camera_id),
            )
            message_id = int(getattr(passport, "message_id", None) or passport["message_id"])
            self.state.bind_panel(camera_id, message_id)
            await self.tg.pin_chat_message(
                chat_id=self.chat_id, message_id=message_id, disable_notification=True
            )
            self.log(f"камера {camera_id}: создана тема {thread_id}")
            await self.refresh_panel(camera_id)
            return thread_id

    async def retire_topic(self, camera_id: str) -> None:
        topic = self.state.topic_for(camera_id)
        if topic is None or topic.status == "retired":
            return
        self.state.retire_topic(camera_id)
        await self.tg.send_message(
            chat_id=self.chat_id, message_thread_id=topic.thread_id,
            text=self._t("topic.retired"),
        )
        await self.tg.close_forum_topic(
            chat_id=self.chat_id, message_thread_id=topic.thread_id
        )
        self.log(f"камера {camera_id}: тема {topic.thread_id} закрыта")

    def passport_text(self, camera_id: str, title: str, site: str, status: str) -> str:
        lines = [self._t("panel.camera", title=title or camera_id), f"camera_id: {camera_id}"]
        if site:
            lines.append(self._t("panel.site", site=site))
        lines.append(self._t("panel.status", status=status))
        return "\n".join(lines)

    def panel_text(self, camera_id: str, camera=None, *, user_id: int | None = None) -> str:
        """Живая панель камеры: состояние камеры, детектора и подписки одним экраном."""
        topic = self.state.topic_for(camera_id)
        title = camera.title if camera is not None else (topic.title if topic else camera_id)
        lines = [self._t("panel.camera", title=title), f"camera_id: {camera_id}"]
        if camera is not None:
            if camera.site:
                lines.append(self._t("panel.site", site=camera.site))
            lines.append(self._t("panel.stream", state=self._t(
                STATUS_TEXT.get(camera.status, STATUS_TEXT["unavailable"]))))
            lines.append(self._t("panel.detection", state=self._t(
                MOTION_STATE_TEXT.get(camera.motion_state, MOTION_STATE_TEXT["unknown"]))))
            if camera.last_motion_at:
                lines.append(self._t("panel.last_motion", time=self._time(camera.last_motion_at)))
        else:
            lines.append(self._t("panel.bridge_silent"))
            lines.append(self._t("panel.detection", state=self._t(MOTION_STATE_TEXT["unknown"])))
        subscribed = self.state.motion_subscribers(camera_id)
        lines.append(self._t("panel.notifications", state=self._t(
            "panel.notify_on" if subscribed else "panel.notify_off")))
        return "\n".join(lines)

    async def refresh_panel(self, camera_id: str, *, user_id: int | None = None,
                            force: bool = False) -> None:
        """Перерисовать закреплённую панель. Тихо: пользователь её не заказывал.

        `force` нужен после выката новой версии: набор кнопок сменился, а текст
        панели — нет, и без принуждения в темах остались бы старые кнопки.
        """
        panel = self.state.panel_for(camera_id)
        topic = self.state.topic_for(camera_id)
        if panel is None or topic is None or topic.status != "active":
            return
        message_id, rendered = panel
        try:
            cameras = await asyncio.to_thread(self.bridge.cameras)
            camera = next((c for c in cameras if c.camera_id == camera_id), None)
        except BridgeError:
            camera = None
        text = self.panel_text(camera_id, camera, user_id=user_id)
        # В панели не осталось тикающих меток («Последний кадр», «Обновлено»):
        # они делали правку темы ежеминутной, а тема от каждой правки всплывала
        # у владельца как новое событие. Перерисовка — только при смене сути.
        if text == rendered and not force:
            return
        try:
            await self.tg.edit_message_text(
                chat_id=self.chat_id, message_id=message_id, text=text,
                reply_markup=self.control_markup(camera_id, user_id=user_id, camera=camera),
            )
        except Exception as exc:  # чужая правка или удалённое сообщение не должны ронять ход
            self.log(f"панель {camera_id}: обновить не удалось ({type(exc).__name__})")
            return
        self.state.remember_panel_text(camera_id, text)

    async def refresh_all_panels(self, *, force: bool = False) -> None:
        for topic in self.state.active_topics():
            await self.refresh_panel(topic.camera_id, force=force)

    # --- кнопки -----------------------------------------------------------
    def buttons(self, camera_id: str, *, user_id: int | None = None,
                center_at: str | None = None, camera=None) -> list[Button]:
        """Кнопки паспорта. В data — только непрозрачный токен с TTL.

        Панель перерисовывается только при смене текста (иначе тема всплывает
        от каждой правки), поэтому часовой TTL здесь молча убивал кнопки:
        спокойная камера — панель не менялась сутки — и «Кадр» отвечал
        «Кнопка устарела». Токен панели живёт неделю; нажатие всё равно
        проходит allow-list и проверку темы, как и клавиатура без токенов.
        Короткий TTL остаётся у кнопки клипа под конкретный кадр: она обязана
        истечь вместе с окном клипа.
        """
        ttl = self.cfg.panel_callback_ttl_sec
        issue = self.state.issue_callback
        result = [Button(self._t(ACTION_TITLES[a]), f"cv:{a}:{issue(camera_id, a, ttl, center_at)}")
                  for a in ("snap", "clip", "stat")]
        on = user_id is not None and self.state.motion_enabled(user_id, camera_id)
        result.append(Button(self._t("action.sub_off" if on else "action.sub_on"),
                             f"cv:sub:{issue(camera_id, 'sub', ttl)}"))
        # Управление держим рядом с просмотром: иначе пауза требует ssh на сервер.
        paused = camera is not None and camera.status in ("paused", "retired")
        result.append(Button(self._t("button.resume" if paused else "button.pause"),
                             f"cv:{'resume' if paused else 'pause'}:"
                             f"{issue(camera_id, 'resume' if paused else 'pause', ttl)}"))
        result.append(Button(self._t("button.rename"), f"cv:rename:{issue(camera_id, 'rename', ttl)}"))
        result.append(Button(self._t("button.retire"), f"cv:retire:{issue(camera_id, 'retire', ttl)}"))
        result.append(Button(self._t("button.setup"), f"cv:setup:{issue(camera_id, 'setup', ttl)}"))
        return result

    async def setup_markup(self, camera_id: str, config: dict):
        """Кнопки карточки настройки: пароль, детекция людей, удаление из реестра."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        ttl = self.cfg.callback_ttl_sec
        person = config.get("person_detection") is True
        rows = [
            [InlineKeyboardButton(
                self._t("setup.creds_button"),
                callback_data=f"cv:cand:{self.state.issue_callback(camera_id, 'cand', ttl, config.get('host') or '')}")],
            [InlineKeyboardButton(
                self._t("setup.detect_off_button" if person else "setup.detect_on_button"),
                callback_data=f"cv:detect:{self.state.issue_callback(camera_id, 'detect', ttl, '0' if person else '1')}")],
            [InlineKeyboardButton(
                self._t("setup.drop_button"),
                callback_data=f"cv:drop:{self.state.issue_callback(camera_id, 'drop', ttl)}")],
        ]
        return InlineKeyboardMarkup(rows)

    def control_markup(self, camera_id: str, *, user_id: int | None = None, camera=None):
        return self._markup(self.buttons(camera_id, user_id=user_id, camera=camera))

    def frame_markup(self, camera_id: str, captured_at: str | None):
        """У каждого кадра — клип именно вокруг него, а не вокруг неявного «сейчас»."""
        token = self.state.issue_callback(camera_id, "clip", self.cfg.callback_ttl_sec, captured_at)
        return self._markup([Button(self._t("button.clip_around"), f"cv:clip:{token}")])

    def reply_keyboard(self):
        """Клавиатура держится у поля ввода и не уезжает вверх вместе с лентой.

        Reply-клавиатура в супергруппе одна на чат, поэтому подписи нейтральны:
        нажатие приходит текстом в конкретную тему, по ней и находится камера.
        """
        from telegram import KeyboardButton, ReplyKeyboardMarkup

        rows = [[self._t(key) for key in row] for row in KEYBOARD_ROWS]
        return ReplyKeyboardMarkup(
            [[KeyboardButton(text) for text in row] for row in rows],
            resize_keyboard=True, is_persistent=True, selective=True,
        )

    def _markup(self, buttons: list[Button]):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(b.text, callback_data=b.data) for b in row] for row in rows]
        )

    # --- обработка нажатия -------------------------------------------------
    async def on_callback(self, user_id: int | None, thread_id: int | None, data: str) -> str:
        """Вернуть короткий текст для answerCallbackQuery. Побочный эффект — публикация."""
        if not self.allowed(user_id):
            return self._t("no_access")
        parts = (data or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "cv":
            return self._t("callback.unknown")
        resolved = self.state.resolve_callback(parts[2])
        if resolved is None:
            return self._t("callback.expired")
        camera_id, action, center_at = resolved

        console = self.state.get_service(CONSOLE_KEY)
        on_console = console is not None and thread_id is not None and thread_id == int(console)
        if action == "panel":
            if not on_console:
                return self._t("callback.wrong_topic")
            await self.refresh_console()
            return self._t("console.refreshed")
        if camera_id == CONSOLE_CAMERA:
            # Заведение камеры идёт на пульте: темы у неё ещё нет, поэтому
            # проверка принадлежности к теме камеры здесь неприменима.
            if not on_console:
                return self._t("callback.wrong_topic")
            if action == "add":
                asyncio.create_task(self._discover())
                return self._t("add.searching_here")
            if action == "cand":
                return await self._ask_credentials(user_id, center_at or "", "", thread_id)
            if action in ("act", "actall"):
                return await self._ask_activation(user_id, thread_id, center_at or "")
            if action == "addr":
                self.state.expect_input(user_id, CONSOLE_CAMERA, "addr", INPUT_TTL_SEC)
                return await self._ask_for_text(thread_id, self._t("add.ask_address"))
            if action == "model":
                asyncio.create_task(self.model_menu())
                return self._t("model.menu_here")
            if action == "mfam":
                asyncio.create_task(self.model_files(center_at or ""))
                return self._t("model.files_here")
            if action == "mset":
                return await self._switch_model(center_at or "")
            if action == "thr":
                asyncio.create_task(self.threshold_menu())
                return self._t("thr.menu_here")
            if action == "thrcal":
                return await self._calibrate(center_at or "")
            if action == "thrset":
                return await self._ask_threshold(user_id, thread_id, center_at or "")
            if action == "thrauto":
                return await self._set_threshold(center_at or "", None)
            return self._t("callback.unknown")

        topic = self.state.topic_for(camera_id)
        if topic is None or topic.status != "active":
            return self._t("camera.retired")
        # Кнопка действует в теме своей камеры или на пульте: чужая тема — отказ.
        if thread_id is not None and thread_id != topic.thread_id and not on_console:
            return self._t("callback.wrong_topic")

        return await self._perform(user_id, camera_id, topic.thread_id, action, center_at)

    async def on_text(self, user_id: int | None, thread_id: int | None, text: str,
                      reply_to_message_id: int | None = None,
                      message_id: int | None = None) -> str:
        """Нажатие постоянной кнопки приходит обычным текстом — камеру даёт тема."""
        # Telegram присылает нажатие selective ReplyKeyboard как reply на то
        # сообщение бота, которое показало клавиатуру. Поэтому кнопку нужно
        # распознать ДО общего пути «ответ на кадр»: иначе «📷 Кадр» ошибочно
        # трактуется как запрос клипа вокруг сообщения с клавиатурой.
        action = keyboard_action(text)
        if action is not None:
            if not self.allowed(user_id):
                return self._t("no_access")
            camera_id = self.state.camera_for_thread(thread_id) if thread_id is not None else None
            if camera_id is None:
                return self._t("keyboard.camera_topic_only")
            topic = self.state.topic_for(camera_id)
            if topic is None or topic.status != "active":
                return self._t("camera.retired")
            return await self._perform(user_id, camera_id, topic.thread_id, action, None)
        if reply_to_message_id is not None:
            return await self.on_frame_reply(user_id, thread_id, reply_to_message_id)
        if self.allowed(user_id):
            pending = self.state.take_input(user_id)
            if pending is not None:
                if pending[1].startswith(SECRET_INPUTS):
                    # Пароль стирается из чата до любого сетевого вызова: чат
                    # индексируется в личный RAG, и лишней секунды ему хватит.
                    await self._forget_message(message_id)
                return await self._apply_input(user_id, pending[0], pending[1], text or "")
        return self._t(UNKNOWN_KEY)

    async def on_frame_reply(self, user_id: int | None, thread_id: int | None,
                             replied_message_id: int) -> str:
        """Текстовый reply на кадр запрашивает клип строго вокруг этого кадра."""
        if not self.allowed(user_id):
            return self._t("no_access")
        frame = self.state.resolve_frame_reply(replied_message_id)
        # Буфер записи короткий (минуты), связь «фото → момент» живёт час: оба
        # отказа обязаны говорить, что делать дальше, а не загадкой из кода.
        if frame is None:
            return self._t("frame.unknown")
        if frame.expired:
            return self._t("frame.expired")
        if thread_id != frame.thread_id:
            return self._t("frame.other_topic")
        topic = self.state.topic_for(frame.camera_id)
        if topic is None or topic.status != "active":
            return self._t("camera.retired")
        return await self._request_media(
            frame.camera_id, frame.thread_id, "clip", frame.center_at
        )

    async def _perform(self, user_id: int | None, camera_id: str, thread_id: int,
                       action: str, center_at: str | None) -> str:
        """Одно действие — один путь, независимо от того, откуда пришло нажатие."""
        if action == "sub":
            enabled = self.state.toggle_motion(user_id, camera_id)
            await self.refresh_panel(camera_id, user_id=user_id)
            return self._t("notify.on" if enabled else "notify.off")
        if action == "stat":
            return await self._status(camera_id, thread_id, user_id)
        if action in ("pause", "resume", "retire", "rename"):
            return await self._control(user_id, camera_id, thread_id, action)
        if action == "setup":
            return await self._setup(camera_id, thread_id)
        if action == "cand":
            return await self._ask_credentials(user_id, center_at or "", camera_id, thread_id)
        if action == "detect":
            return await self._toggle_detection(camera_id, thread_id, center_at == "1")
        if action == "drop":
            self.state.expect_input(user_id, camera_id, "drop", INPUT_TTL_SEC)
            return await self._ask_for_text(thread_id, self._t(INPUT_PROMPTS["drop"]))
        return await self._request_media(
            camera_id, thread_id, "clip" if action == "clip" else "snapshot", center_at
        )

    async def _ask_for_text(self, thread_id: int, text: str) -> str:
        """Просьба прислать текст обязана остаться в теме, а не мигнуть тостом.

        Ответ на нажатие кнопки Telegram показывает всплывающей подсказкой:
        однострочную — на пару секунд, многострочную — окном с «ОК». И то и
        другое исчезает без следа, и человек, отложивший телефон на минуту,
        видит молчащий чат и решает, что кнопка сломана. Поэтому сама просьба
        уходит обычным сообщением в тему и висит там до ввода; тем же текстом
        отвечаем и на нажатие — вторая строка делает подсказку окном.
        """
        try:
            await self.tg.send_message(chat_id=self.chat_id,
                                       message_thread_id=thread_id, text=text)
        except Exception as exc:  # тема могла закрыться между нажатием и ответом
            self.log(f"просьба о вводе {thread_id}: {type(exc).__name__}")
        return text

    async def _control(self, user_id: int | None, camera_id: str, thread_id: int,
                       action: str) -> str:
        """Управление камерой из чата. Адреса и пароли этим путём не меняются.

        Пауза и имя применяются сразу; снятие с эксплуатации закрывает тему,
        поэтому требует подтверждения словом — случайное нажатие не должно
        уносить камеру из пульта.
        """
        if action == "rename":
            self.state.expect_input(user_id, camera_id, "rename", INPUT_TTL_SEC)
            return await self._ask_for_text(
                thread_id, self._t(INPUT_PROMPTS["rename"], minutes=INPUT_TTL_SEC // 60))
        if action == "retire":
            self.state.expect_input(user_id, camera_id, "retire", INPUT_TTL_SEC)
            return await self._ask_for_text(thread_id, self._t(INPUT_PROMPTS["retire"]))
        try:
            await asyncio.to_thread(self.bridge.set_camera_state, camera_id, action)
        except BridgeError as exc:
            return self._error(exc.code)
        # Состояние сменилось по команде человека: сторож не должен об этом
        # рассказывать ещё раз как о новости.
        self.state.note_health(camera_id, "paused" if action == "pause" else "ok")
        await self.refresh_panel(camera_id, user_id=user_id)
        await self.refresh_console()
        if action == "pause":
            return self._t("control.paused")
        return self._t("control.resumed")

    async def _apply_input(self, user_id: int | None, camera_id: str, kind: str,
                           text: str) -> str:
        """Довести до конца действие, которому нужен был текст от человека."""
        head, _, payload = kind.partition("|")
        # Заведение камеры идёт до появления темы, поэтому эти ветки выше проверки темы.
        if head == "creds":
            return await self._apply_credentials(user_id, payload, text)
        if head == "name":
            return await self._apply_new_camera(user_id, payload, text)
        if head == "thr":
            return await self._apply_threshold(payload, text)
        if head == "actok":
            return await self._confirm_activation(user_id, payload, text)
        if head == "actpw":
            return await self._apply_activation_password(user_id, payload, text)
        if head == "addr":
            thread_id = await self.ensure_console()
            if thread_id is None:
                return self._t("wizard.group_needs")
            return await self._accept_address(user_id, text, thread_id)
        topic = self.state.topic_for(camera_id)
        if topic is None or topic.status != "active":
            return self._t("camera.retired")
        if kind == "drop":
            if text.strip().lower() not in confirm_words(CONFIRM_WORDS["drop"]):
                return self._t("drop.cancelled")
            return await self._delete_camera(camera_id)
        if kind == "rename":
            title = text.strip()[:64]
            if not title:
                return self._t("rename.empty")
            try:
                await asyncio.to_thread(self.bridge.set_camera_state, camera_id, "rename", title)
            except BridgeError as exc:
                return self._error(exc.code)
            self.state.rename_topic(camera_id, title)
            try:
                await self.tg.edit_forum_topic(chat_id=self.chat_id,
                                               message_thread_id=topic.thread_id, name=title,
                                               icon_custom_emoji_id=topic_icon(title, camera_id))
            except Exception as exc:
                self.log(f"переименование темы {camera_id}: {type(exc).__name__}")
            await self.refresh_panel(camera_id, user_id=user_id)
            await self.refresh_console()
            return self._t("rename.done", title=title)
        if text.strip().lower() not in confirm_words(CONFIRM_WORDS["retire"]):
            return self._t("retire.cancelled")
        try:
            await asyncio.to_thread(self.bridge.set_camera_state, camera_id, "retire")
        except BridgeError as exc:
            return self._error(exc.code)
        await self.retire_topic(camera_id)
        await self.refresh_console()
        return self._t("camera.retired")

    # --- заведение, правка и удаление камеры --------------------------------
    async def _forget_message(self, message_id: int | None) -> None:
        """Стереть сообщение с паролем. Ни его текст, ни причина сбоя не логируются."""
        if message_id is None:
            return
        try:
            await self.tg.delete_message(chat_id=self.chat_id, message_id=message_id)
        except Exception as exc:
            self.log(f"сообщение с секретом удалить не удалось ({type(exc).__name__})")

    async def _discover(self) -> None:
        """Опрос сетей и список кандидатов на пульте. Работает фоном: /24 не мгновенен."""
        thread_id = await self.ensure_console()
        if thread_id is None:
            return
        try:
            started = await asyncio.to_thread(self.bridge.scan_start)
        except BridgeError as exc:
            await self.notify_console(self._t("scan.not_started", reason=self._error(exc.code)))
            return
        if not started.get("ok"):
            await self.notify_console(self._t(
                "scan.not_started",
                reason=started.get("error") or self._t("scan.reason_unknown")))
            return
        scan_id = str(started.get("scan_id") or "")
        networks = ", ".join(started.get("networks") or []) or self._t("scan.default_networks")
        await self.notify_console(self._t("scan.searching", networks=networks))
        deadline = asyncio.get_running_loop().time() + SCAN_WAIT_SEC
        status: dict = {}
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(SCAN_POLL_SEC)
            try:
                status = await asyncio.to_thread(self.bridge.scan_status, scan_id)
            except BridgeError:
                continue
            if status.get("status") != "running":
                break
        if status.get("status") != "done":
            await self.notify_console(self._t(
                "scan.not_finished",
                reason=status.get("error") or self._t("scan.bridge_timeout")))
            return
        candidates = [c for c in status.get("candidates") or [] if c.get("host")]
        known = set()
        try:
            known = {c.camera_id for c in await asyncio.to_thread(self.bridge.cameras)}
        except BridgeError:
            pass
        # Уже заведённые мост помечает в кандидате: предлагать их кнопкой —
        # путь к дублю в реестре. Показываем списком, чтобы было видно,
        # что поиск их видел.
        registered = {str(c["host"]): str(c.get("registered_camera_id"))
                      for c in candidates if c.get("registered_camera_id")}
        listed_known = ", ".join(f"{camera_id} ({host})"
                                 for host, camera_id in sorted(registered.items()))
        fresh = [c for c in candidates if c["host"] not in registered]
        # Новая Hikvision без пароля: «логин и пароль» у неё спрашивать
        # бессмысленно — её сначала активируют. Отдельный список и кнопки.
        inactive = [c for c in fresh if c.get("activated") is False
                    and c.get("activation") in AUTO_ACTIVATION_PROTOCOLS]
        # Новая камера марки без автоактивации — инструкция, как задать пароль
        # вручную; мастер не обрывается: после настройки та же камера кнопкой.
        manual = [c for c in fresh if c not in inactive
                  and (c.get("activation") == "manual" or c.get("activated") is False)]
        fresh = [c for c in fresh if c not in inactive and c not in manual]
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        ttl = self.cfg.callback_ttl_sec
        # Камера вне поиска (другой порт, RTSP-сервер без ONVIF) — по адресу:
        # кнопка нужна и при пустом результате, иначе тупик.
        tail = [InlineKeyboardButton(
                    self._t("scan.retry_button"),
                    callback_data=f"cv:add:{self.state.issue_callback(CONSOLE_CAMERA, 'add', ttl)}"),
                InlineKeyboardButton(
                    self._t("add.manual_button"),
                    callback_data=f"cv:addr:{self.state.issue_callback(CONSOLE_CAMERA, 'addr', ttl)}")]
        act_rows, act_lines = [], []
        if inactive:
            act_lines = [self._t("scan.inactive", count=len(inactive))]
            act_rows = [[InlineKeyboardButton(
                self._t("act.button_one", label=str(c.get("label") or c["host"])[:48]),
                callback_data=f"cv:act:{self.state.issue_callback(CONSOLE_CAMERA, 'act', ttl, c['host'])}")]
                for c in inactive[:ACTIVATION_MAX_HOSTS]]
            hosts = ",".join(c["host"] for c in inactive[:ACTIVATION_MAX_HOSTS])
            act_rows.append([InlineKeyboardButton(
                self._t("act.button_all", count=min(len(inactive), ACTIVATION_MAX_HOSTS)),
                callback_data=f"cv:actall:{self.state.issue_callback(CONSOLE_CAMERA, 'actall', ttl, hosts)}")])
        if manual:
            await self._send_manual_setup(thread_id, manual,
                                          None if fresh or act_rows else tail)
            if not fresh and not act_rows:
                return
        if not fresh and act_rows:
            await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                       text="\n".join(act_lines),
                                       reply_markup=InlineKeyboardMarkup(act_rows + [tail]))
            return
        if not fresh:
            text = (self._t("scan.none_new", known=listed_known) if registered
                    else self._t("scan.none"))
            await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                       text=text + "\n" + self._t("add.manual_hint"),
                                       reply_markup=InlineKeyboardMarkup([tail]))
            return
        rows = [[InlineKeyboardButton(
            str(c.get("label") or c["host"])[:64],
            callback_data=f"cv:cand:{self.state.issue_callback(CONSOLE_CAMERA, 'cand', ttl, c['host'])}")]
            for c in fresh[:12]]
        rows += act_rows
        rows.append(tail)
        lines = [self._t("scan.found", fresh=len(fresh), known=len(known)),
                 self._t("scan.pick")]
        if listed_known:
            lines.insert(1, self._t("scan.skipped", known=listed_known))
        lines += act_lines
        await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                   text="\n".join(lines),
                                   reply_markup=InlineKeyboardMarkup(rows))

    def setup_instruction(self, brand: str) -> str:
        """Как задать первый пароль камере этой марки вручную."""
        key = f"setup.{brand}" if brand in MANUAL_SETUP_BRANDS else "setup.generic"
        return self._t(key)

    async def _send_manual_setup(self, thread_id: int, cameras: list[dict],
                                 tail: list | None) -> None:
        """Камеры, которые бот не активирует сам: инструкция по каждой марке
        один раз и кнопка «пароль уже задан» на камеру — в обычный путь /add."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        ttl = self.cfg.callback_ttl_sec
        lines = [self._t("scan.manual", count=len(cameras))]
        by_brand: dict[str, list[dict]] = {}
        for camera in cameras:
            brand = str(camera.get("brand") or camera.get("vendor") or "").lower()
            by_brand.setdefault(brand if brand in MANUAL_SETUP_BRANDS else "", []).append(camera)
        for brand, group in by_brand.items():
            hosts = ", ".join(str(c["host"]) for c in group)
            name = BRAND_NAMES.get(brand) or self._t("setup.unknown_brand")
            lines.append("")
            lines.append(self._t("scan.manual_brand", brand=name, hosts=hosts))
            lines.append(self.setup_instruction(brand))
        lines.append("")
        lines.append(self._t("scan.manual_next"))
        rows = [[InlineKeyboardButton(
            self._t("scan.manual_button", label=str(c.get("label") or c["host"])[:40]),
            callback_data=f"cv:cand:{self.state.issue_callback(CONSOLE_CAMERA, 'cand', ttl, c['host'])}")]
            for c in cameras[:ACTIVATION_MAX_HOSTS]]
        if tail:
            rows.append(tail)
        await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                   text="\n".join(lines)[:4000],
                                   reply_markup=InlineKeyboardMarkup(rows))

    async def _ask_credentials(self, user_id: int | None, host: str, camera_id: str,
                               thread_id: int, detect: str = "") -> str:
        """Спросить логин и пароль. Пароль не идёт ни в какую модель и не хранится."""
        if not host:
            return self._t("creds.no_host")
        self.state.expect_input(user_id, camera_id or CONSOLE_CAMERA,
                                f"creds|{host}|{camera_id}|{detect}", INPUT_TTL_SEC)
        return await self._ask_for_text(thread_id, self._t(INPUT_PROMPTS["creds"], host=host))

    async def _apply_credentials(self, user_id: int | None, payload: str, text: str) -> str:
        """Проверить логин и пароль на самой камере и определить её параметры."""
        host, _, rest = payload.partition("|")
        camera_id, _, detect = rest.partition("|")
        parts = (text or "").strip().split(None, 1)
        if len(parts) < 2:
            self.state.expect_input(user_id, camera_id or CONSOLE_CAMERA,
                                    f"creds|{host}|{camera_id}|{detect}", INPUT_TTL_SEC)
            return self._t("creds.need_both")
        try:
            result = await asyncio.to_thread(self.bridge.probe, host, parts[0], parts[1], detect)
        except BridgeError as exc:
            return self._error(exc.code)
        finally:
            parts = None  # пароль не остаётся в кадре обработчика дольше нужного
        if not result.get("ok"):
            self.state.expect_input(user_id, camera_id or CONSOLE_CAMERA,
                                    f"creds|{host}|{camera_id}|{detect}", INPUT_TTL_SEC)
            return self._t("creds.failed",
                           error=result.get("error") or self._t("creds.no_answer"))
        summary = result.get("summary") or {}
        token = str(result.get("probe_token") or "")
        if camera_id:
            try:
                applied = await asyncio.to_thread(self.bridge.update_camera, camera_id,
                                                  probe_token=token)
            except BridgeError as exc:
                return self._error(exc.code)
            if not applied.get("ok"):
                return self._t("registry.not_saved",
                               error=applied.get("error") or self._t("registry.refused"))
            asyncio.create_task(self._sync_after_restart())
            return self._t("creds.updated", detected=self.detected_text(summary))
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"name|{token}|{host}", INPUT_TTL_SEC)
        return self._t("creds.ask_name", detected=self.detected_text(summary))

    def detected_text(self, summary: dict) -> str:
        """Что определилось у камеры. URL — с затёртым паролем, так их даёт мост."""
        lines = [self._t("detected.camera", host=summary.get("host", ""), name=(
            " ".join(x for x in (summary.get("vendor"), summary.get("model")) if x)
            or self._t("detected.no_vendor")))]
        profiles = summary.get("profiles") or []
        for profile in profiles[:4]:
            size = (f"{profile.get('width')}×{profile.get('height')}"
                    if profile.get("width") else self._t("detected.size_unknown"))
            fps = self._t("detected.fps", fps=profile.get("fps")) if profile.get("fps") else ""
            lines.append(f"• {profile.get('name') or self._t('detected.profile')}: "
                         f"{profile.get('encoding') or '?'} {size}{fps}")
        lines.append(self._t("field.stream",
                             value=summary.get("main_url") or self._t("detected.not_determined")))
        if summary.get("sub_url"):
            lines.append(self._t("field.detect_stream", value=summary["sub_url"]))
        lines.append(self._t("field.snapshot",
                             value=summary.get("snapshot_url") or self._t("detected.no_snapshot")))
        if not summary.get("verified"):
            lines.append(self._t("detected.not_verified"))
        if summary.get("source") == "template":
            lines.append(self._t("detected.template"))
        return "\n".join(lines)

    async def _apply_new_camera(self, user_id: int | None, payload: str, text: str) -> str:
        token, _, host = payload.partition("|")
        title = (text or "").strip()[:64]
        if not title:
            self.state.expect_input(user_id, CONSOLE_CAMERA, f"name|{payload}", INPUT_TTL_SEC)
            return self._t("rename.empty_retry")
        try:
            existing = {c.camera_id for c in await asyncio.to_thread(self.bridge.cameras)}
        except BridgeError:
            existing = set()
        camera_id = unique_camera_id(slugify(title) or f"cam-{host.replace('.', '-')}", existing)
        try:
            added = await asyncio.to_thread(self.bridge.add_camera, camera_id, title, title, token)
        except BridgeError as exc:
            return self._error(exc.code)
        if not added.get("ok"):
            return self._t("add.not_added",
                           error=added.get("error") or self._t("registry.refused"))
        asyncio.create_task(self._sync_after_restart(first_frame=camera_id))
        self.log(f"камера {camera_id}: добавлена в реестр из чата")
        return self._t("add.added", title=title, camera_id=camera_id)

    # --- активация новых Hikvision -------------------------------------------
    # Порядок шагов выбран ради пароля: сперва подтверждение словом (активация
    # необратима), и только потом сам пароль — так он живёт в одном обработчике
    # и не ждёт в памяти бота следующего сообщения. Сообщение с паролем
    # стирается до любого сетевого вызова (SECRET_INPUTS в on_text).
    async def _ask_activation(self, user_id: int | None, thread_id: int, payload: str) -> str:
        hosts = [h for h in payload.split(",") if h][:ACTIVATION_MAX_HOSTS]
        if not hosts:
            return self._t("creds.no_host")
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"actok|{','.join(hosts)}", INPUT_TTL_SEC)
        return await self._ask_for_text(thread_id, self._t(
            "act.confirm", count=len(hosts), hosts=", ".join(hosts),
            word=self._t(CONFIRM_WORDS["activate"])))

    async def _confirm_activation(self, user_id: int | None, hosts: str, text: str) -> str:
        if text.strip().lower() not in confirm_words(CONFIRM_WORDS["activate"]):
            return self._t("act.cancelled")
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"actpw|{hosts}", INPUT_TTL_SEC)
        thread_id = await self.ensure_console()
        prompt = self._t("act.ask_password", word=self._t(CONFIRM_WORDS["generate"]))
        if thread_id is None:
            return prompt
        return await self._ask_for_text(thread_id, prompt)

    async def _apply_activation_password(self, user_id: int | None, hosts: str, text: str) -> str:
        """Пароль admin для пакета: свой или «сгенерировать» — тогда его создаёт мост."""
        typed = (text or "").strip()
        password = None if typed.lower() in confirm_words(CONFIRM_WORDS["generate"]) else typed
        try:
            started = await asyncio.to_thread(self.bridge.activation_start,
                                              hosts.split(","), password)
        except BridgeError as exc:
            return self._error(exc.code)
        finally:
            password = typed = text = None  # пароль не живёт в кадре дольше нужного
        if not started.get("ok"):
            code = str(started.get("error_code") or "")
            if code in ACTIVATION_PASSWORD_ERRORS:
                # Камеры не тронуты: правила проверены до сети. Пробуем снова.
                self.state.expect_input(user_id, CONSOLE_CAMERA, f"actpw|{hosts}", INPUT_TTL_SEC)
                return self._t(f"act.{code}") + "\n" + self._t("act.password_rules")
            if code == "busy":
                return self._t("act.busy")
            return self._t("act.not_started")
        self._activation_task = asyncio.create_task(
            self._await_activation(str(started.get("activation_id") or "")))
        return self._t("act.started", count=len(started.get("hosts") or []))

    async def _await_activation(self, activation_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + ACTIVATION_WAIT_SEC
        status: dict = {}
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(ACTIVATION_POLL_SEC)
            try:
                status = await asyncio.to_thread(self.bridge.activation_status, activation_id)
            except BridgeError:
                continue
            if status.get("status") == "done":
                break
        if status.get("status") != "done":
            # Пароль уже лежит в хранилище моста — не потерян, просто не показан.
            await self.notify_console(self._t("act.timeout"))
            return
        await self._finish_activation(status)

    def _activation_line(self, result: dict, added: dict[str, str]) -> str:
        host = str(result.get("host") or "")
        outcome = str(result.get("outcome") or "failed")
        line = self._t(ACTIVATION_OUTCOME_TEXT.get(outcome, "act.outcome.failed"), host=host)
        stage = str(result.get("stage") or "")
        if host in added:
            line += " " + self._t("act.registered", camera_id=added[host])
        elif outcome == "activated" and result.get("probe_token"):
            line += " " + self._t("act.not_registered")
        if stage:
            line += " " + self._t(ACTIVATION_STAGE_TEXT.get(stage, "act.stage.internal"))
        return line

    async def _finish_activation(self, status: dict) -> None:
        """Итог по каждой камере, запись удачных в реестр, пароль — владельцу в личку.

        Пароль приходит из моста один раз. В группу он не идёт: чат —
        архив и может индексироваться. Не дошёл в личку — он в хранилище моста.
        """
        results = [r for r in status.get("results") or [] if isinstance(r, dict)]
        try:
            existing = {c.camera_id for c in await asyncio.to_thread(self.bridge.cameras)}
        except BridgeError:
            existing = set()
        added: dict[str, str] = {}
        for result in results:
            token = str(result.get("probe_token") or "")
            if result.get("outcome") != "activated" or not token:
                continue
            host = str(result.get("host") or "")
            summary = result.get("summary") or {}
            title = f"{summary.get('model') or result.get('model') or 'Hikvision'} {host}"[:64]
            camera_id = unique_camera_id(f"hik-{host.replace('.', '-')}", existing)
            try:
                reply = await asyncio.to_thread(self.bridge.add_camera, camera_id, title, title, token)
            except BridgeError:
                continue
            if reply.get("ok"):
                existing.add(camera_id)
                added[host] = camera_id
        lines = [self._t("act.summary", done=sum(r.get("outcome") == "activated" for r in results),
                         total=len(results))]
        lines += [self._activation_line(r, added) for r in results]
        password = status.get("password")
        if password:
            text = self._t("act.password_dm", password=password,
                           hosts=", ".join(str(r.get("host")) for r in results
                                           if r.get("outcome") in ("activated", "unverified")))
            delivered = await self.notify_owner(text)
            password = text = None
            lines.append(self._t("act.password_sent" if delivered else "act.password_not_sent"))
        if added:
            lines.append(self._t("act.topics_soon"))
            asyncio.create_task(self._sync_after_restart(first_frame=list(added.values())))
        await self.notify_console("\n".join(lines))
        self.log(f"активация: {len(added)} камер в реестре из {len(results)}")

    async def _setup(self, camera_id: str, thread_id: int) -> str:
        """Карточка настройки в теме камеры: что записано в реестре и что можно сменить."""
        try:
            config = await asyncio.to_thread(self.bridge.camera_config, camera_id)
        except BridgeError as exc:
            return self._error(exc.code)
        if not config:
            return self._t("setup.no_record")
        lines = [self._t("setup.title", title=config.get("title") or camera_id),
                 f"camera_id: {camera_id}",
                 self._t("setup.address", value=config.get("host") or "—"),
                 self._t("setup.login", value=config.get("username") or "—"),
                 self._t("field.stream", value=config.get("rtsp_url") or "—"),
                 self._t("field.detect_stream", value=config.get("detect_rtsp_url") or "—"),
                 self._t("field.snapshot",
                         value=config.get("snapshot_url") or self._t("setup.no_snapshot")),
                 self._t("setup.person", state=self._t(
                     "setup.person_on" if config.get("person_detection") else "setup.person_off"))]
        await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                   text="\n".join(lines),
                                   reply_markup=await self.setup_markup(camera_id, config))
        return self._t("setup.card_sent")

    async def _toggle_detection(self, camera_id: str, thread_id: int, enable: bool) -> str:
        try:
            applied = await asyncio.to_thread(self.bridge.update_camera, camera_id,
                                              person_detection=enable)
        except BridgeError as exc:
            return self._error(exc.code)
        if not applied.get("ok"):
            return self._t("registry.not_saved",
                           error=applied.get("error") or self._t("registry.refused"))
        asyncio.create_task(self._sync_after_restart())
        return self._t("detect.enabled" if enable else "detect.disabled")

    async def _delete_camera(self, camera_id: str) -> str:
        try:
            removed = await asyncio.to_thread(self.bridge.delete_camera, camera_id)
        except BridgeError as exc:
            return self._error(exc.code)
        if not removed.get("ok"):
            return self._t("registry.not_deleted",
                           error=removed.get("error") or self._t("registry.refused"))
        await self.retire_topic(camera_id)
        await self.refresh_console()
        asyncio.create_task(self._sync_after_restart())
        self.log(f"камера {camera_id}: удалена из реестра из чата")
        return self._t("drop.done")

    async def _sync_after_restart(self, first_frame: str | list[str] | None = None) -> None:
        """Реестр меняется вместе с перезапуском цепочки — темы сводим уже после него.

        `first_frame` — новая камера (или несколько после пакетной активации):
        после появления темы шлём в неё первый кадр.
        """
        frames = [first_frame] if isinstance(first_frame, str) else list(first_frame or [])
        await asyncio.sleep(RESTART_WAIT_SEC)
        for attempt in range(3):
            try:
                await self.sync_registry()
                if frames:
                    await asyncio.gather(*(self.first_frame(camera_id) for camera_id in frames))
                return
            except Exception as exc:  # мост может ещё подниматься
                self.log(f"сверка реестра после перезапуска отложена ({type(exc).__name__})")
                await asyncio.sleep(5 * (attempt + 1))

    async def _status(self, camera_id: str, thread_id: int,
                      user_id: int | None = None) -> str:
        """Статус отвечает содержимым панели: «обновлена» ничего не сообщало о камере."""
        try:
            cameras = await asyncio.to_thread(self.bridge.cameras)
        except BridgeError as exc:
            return self._error(exc.code)
        camera = next((c for c in cameras if c.camera_id == camera_id), None)
        if camera is None:
            return self._t(ERROR_TEXT["not_found"])
        await self.refresh_panel(camera_id, user_id=user_id)
        return self.panel_text(camera_id, camera, user_id=user_id)

    async def _request_media(self, camera_id: str, thread_id: int, kind: str,
                             center_at: str | None) -> str:
        request_id = str(uuid.uuid4())
        self.state.remember_request(request_id, camera_id, kind, thread_id)
        try:
            await asyncio.to_thread(
                self.bridge.request_media, request_id, camera_id, kind, utcnow(), center_at
            )
        except BridgeError as exc:
            return self._error(exc.code)
        return self._t("media.snap_requested" if kind == "snapshot" else "media.clip_requested")

    # --- события Bridge -----------------------------------------------------
    async def on_event(self, event: Event) -> None:
        """Единая точка входа для событий; повтор `event_id` не создаёт второй пост."""
        if not self.state.is_new_event(event.event_id):
            self.log(f"событие {event.type}: повтор event_id, пропуск")
            return
        if event.type == "camera.registered":
            if self.chat_id is None:
                return  # тему заведёт сверка реестра после привязки группы
            await self.ensure_topic(event.camera_id, event.title or event.camera_id, event.site or "")
        elif event.type == "camera.retired":
            await self.retire_topic(event.camera_id)
        elif event.type == "motion.detected":
            await self._on_motion(event)
        elif event.type == "media.ready":
            await self._on_media_ready(event)
        elif event.type == "media.failed":
            await self._on_media_failed(event)

    async def _on_motion(self, event: Event) -> None:
        topic = self.state.topic_for(event.camera_id)
        if topic is None or topic.status != "active":
            self.log(f"движение по неизвестной камере {event.camera_id}: пропуск")
            return
        # Подписка владельца управляет звуком, а не самим фактом записи в тему.
        silent = not self.state.motion_subscribers(event.camera_id)
        # cctv_pipeline шлёт конкретное имя источника ("recorded_main_person_detector"),
        # а не голое "person_detector" — точное сравнение молчало и подписывало
        # подтверждённого человека как обычное "Движение".
        is_person = "person_detector" in (event.source or "")
        caption = (f"{self._t('event.person' if is_person else 'event.motion')}: "
                   f"{self._time(event.occurred_at or utcnow())}")
        if event.media is None:
            await self.tg.send_message(
                chat_id=self.chat_id, message_thread_id=topic.thread_id,
                text=caption + "\n" + self._t("event.no_frame"), disable_notification=silent,
            )
            return
        await self._publish(event, topic.thread_id, "snapshot", caption, silent=silent)

    async def _on_media_ready(self, event: Event) -> None:
        if event.request_id and not event.source_event_id:
            # Дедупликации по event_id мало: Bridge может повторить готовность
            # того же запроса под новым event_id. Ключ выдачи — request_id.
            pending = self.state.take_request(event.request_id)
            if pending is None:
                self.log(f"media.ready {event.request_id}: запрос неизвестен или уже отдан")
                return
            thread_id = pending.thread_id
        else:
            # Клип движения бот не заказывал: его request_id — это event_id самого
            # движения, и строгая проверка отправляла каждый такой клип в мусор.
            topic = self.state.topic_for(event.camera_id)
            if topic is None or topic.status != "active":
                self.log(f"media.ready по неизвестной камере {event.camera_id}: пропуск")
                return
            thread_id = topic.thread_id
        kind = event.kind or "snapshot"
        stamp = event.captured_at or event.occurred_at or utcnow()
        caption = f"{self._t('media.frame' if kind == 'snapshot' else 'media.clip')}: {self._time(stamp)}"
        await self._publish(event, thread_id, kind, caption, silent=False)

    async def _on_media_failed(self, event: Event) -> None:
        """Отказ Bridge обязан вернуться в ту же тему: иначе кнопка выглядит сломанной."""
        thread_id = None
        if event.request_id:
            pending = self.state.take_request(event.request_id)
            if pending is not None:
                thread_id = pending.thread_id
        if thread_id is None:
            topic = self.state.topic_for(event.camera_id)
            if topic is None or topic.status != "active":
                self.log(f"media.failed по неизвестной камере {event.camera_id}: пропуск")
                return
            thread_id = topic.thread_id
        what = self._t("media.clip" if event.kind == "clip" else "media.frame")
        reason = self._error(event.error or "")
        await self.tg.send_message(
            chat_id=self.chat_id, message_thread_id=thread_id,
            text=self._t("media.failed", what=what, reason=reason),
        )

    async def _publish(self, event: Event, thread_id: int, kind: str, caption: str,
                       *, silent: bool) -> None:
        assert event.media is not None
        try:
            media = await asyncio.to_thread(
                self.bridge.download, event.media.url, kind=kind,
                expected_sha256=event.media.sha256, declared_bytes=event.media.bytes,
            )
        except BridgeError as exc:
            await self.tg.send_message(
                chat_id=self.chat_id, message_thread_id=thread_id,
                text=f"{caption}\n{self._error(exc.code)}", disable_notification=silent,
            )
            return
        try:
            await self._send_media_with_retry(event, thread_id, kind, caption,
                                              media.path, silent=silent)
        finally:
            # Временный файл не переживает публикацию ни при успехе, ни при ошибке.
            try:
                os.unlink(media.path)
            except OSError:
                pass

    async def _send_media_with_retry(self, event: Event, thread_id: int, kind: str,
                                     caption: str, path: str, *, silent: bool) -> None:
        """Довезти медиа до темы или честно сказать, что событие потеряно.

        Тема Telegram — единственное место, где событие хранится: диск моста
        транзитный. Обрыв сети или лимит Telegram молча вырезал бы кусок архива,
        поэтому отправка повторяется, а окончательный отказ снимает отметку
        дедупликации — повтор того же события от Bridge должен пройти.
        """
        last: Exception | None = None
        # Считается один раз на событие, а не на попытку: ffprobe/ffmpeg тут
        # дешевле повторной заливки, но повторять их на каждом ретрае незачем.
        meta: dict | None = None
        thumb_path: str | None = None
        try:
            for attempt in range(DELIVERY_ATTEMPTS):
                try:
                    with open(path, "rb") as handle:
                        if kind == "clip":
                            if meta is None:
                                meta = await probe_video(path, self.log)
                                thumb_path = await make_thumbnail(path, self.log)
                            with open_thumbnail(thumb_path) as thumb:
                                await self.tg.send_video(
                                    chat_id=self.chat_id, message_thread_id=thread_id,
                                    video=handle, caption=caption, disable_notification=silent,
                                    supports_streaming=True, thumbnail=thumb, **meta,
                                    # Имя задаёт mime загрузки: без .mp4 Bot API кладёт
                                    # документ, и Android рисует файл вместо плеера.
                                    filename=CLIP_FILENAME,
                                    read_timeout=self.cfg.tg_media_timeout_sec,
                                    write_timeout=self.cfg.tg_media_timeout_sec,
                                )
                        else:
                            published = await self.tg.send_photo(
                                chat_id=self.chat_id, message_thread_id=thread_id,
                                photo=handle, caption=caption, disable_notification=silent,
                                read_timeout=self.cfg.tg_media_timeout_sec,
                                write_timeout=self.cfg.tg_media_timeout_sec,
                                reply_markup=self.frame_markup(
                                    event.camera_id, event.captured_at or event.occurred_at
                                ),
                            )
                            message_id = int(getattr(published, "message_id", None)
                                             or published["message_id"])
                            self.state.remember_frame(
                                message_id, event.camera_id, thread_id,
                                event.captured_at or event.occurred_at or utcnow(),
                                self.cfg.callback_ttl_sec,
                            )
                    return
                except Exception as exc:  # сеть, лимит Telegram, временная ошибка API
                    last = exc
                    self.log(f"доставка {kind} {event.camera_id}: попытка "
                             f"{attempt + 1}/{DELIVERY_ATTEMPTS} не удалась ({type(exc).__name__})")
                    if attempt + 1 < DELIVERY_ATTEMPTS:
                        await asyncio.sleep(DELIVERY_BACKOFF_SEC[min(attempt, len(DELIVERY_BACKOFF_SEC) - 1)])
            # Все попытки исчерпаны: событие не должно тихо исчезнуть из архива.
            self.state.forget_event(event.event_id)
            try:
                await self.tg.send_message(
                    chat_id=self.chat_id, message_thread_id=thread_id,
                    text=self._t("delivery.failed", caption=caption, attempts=DELIVERY_ATTEMPTS,
                                 error=type(last).__name__ if last else self._t("delivery.error")),
                )
            except Exception as exc:
                self.log(f"предупреждение о потере не ушло: {type(exc).__name__}")
        finally:
            # Миниатюра — наш временный файл; она не должна пережить доставку.
            if thumb_path:
                try:
                    os.unlink(thumb_path)
                except OSError:
                    pass

    # --- сторож -------------------------------------------------------------
    @staticmethod
    def health_state(camera) -> str:
        """Одно слово о камере. Пауза и снятие — не поломка, о них не тревожим."""
        if camera.status in ("paused", "retired"):
            return camera.status
        if camera.status != "online":
            return "no_frames"
        if camera.motion_state in ("stalled", "blind", "disabled", "behind"):
            return f"detector_{camera.motion_state}"
        return "ok"

    async def watch_health(self) -> None:
        """Сообщить о переходах состояния. Тишина не должна означать поломку.

        Панель показывает «нет кадров» только тому, кто сам открыл тему, — а
        сломанная камера выглядит ровно как спокойная ночь. Поэтому о смене
        состояния бот говорит сам, и только о смене: повтор каждую минуту
        отучил бы читать тему.
        """
        try:
            cameras, storage = await asyncio.to_thread(self.bridge.registry)
        except BridgeError as exc:
            self.log(f"сторож: реестр недоступен ({exc.code})")
            self._bridge_failures += 1
            if (self._bridge_failures >= BRIDGE_DOWN_AFTER
                    and self.state.note_health(BRIDGE_HEALTH_KEY, "down")):
                await self.notify_owner(f"{self._t(BRIDGE_DOWN_KEY)}\n{self._time(utcnow())}")
            return
        self._bridge_failures = 0
        was = self.state.health_of(BRIDGE_HEALTH_KEY)
        if self.state.note_health(BRIDGE_HEALTH_KEY, "ok") and was == "down":
            await self.notify_owner(f"{self._t(BRIDGE_UP_KEY)}\n{self._time(utcnow())}")
        for camera in cameras:
            topic = self.state.topic_for(camera.camera_id)
            if topic is None or topic.status != "active":
                continue
            state = self.health_state(camera)
            known = self.state.health_of(camera.camera_id)
            if not self.state.note_health(camera.camera_id, state):
                continue
            if known is None and state == "ok":
                continue  # исправная камера при старте бота — не новость
            key = HEALTH_TEXT.get(state)
            if key is None:
                continue  # пауза и снятие объявляются в момент нажатия кнопки
            text = self._t(key)
            try:
                await self.tg.send_message(
                    chat_id=self.chat_id, message_thread_id=topic.thread_id,
                    text=f"{text}\n{self._time(utcnow())}",
                )
            except Exception as exc:
                self.log(f"сторож {camera.camera_id}: сообщение не ушло ({type(exc).__name__})")
        await self.watch_storage(storage)

    async def watch_storage(self, storage) -> None:
        """Диск моста — транзит: переполнение рвёт запись клипов, а не архив.

        Порог «мало места» — порог движка (CCTV_MIN_FREE_BYTES), который мост
        отдаёт вместе с реестром: в контейнере транзит — tmpfs на сотни МиБ, и
        прежние жёсткие 2 ГиБ давали тревогу сразу после старта. Старый мост
        без поля — прежние 2 ГиБ.
        """
        if storage is None:
            return
        threshold = getattr(storage, "min_free_bytes", 0) or 2 * 1024 ** 3
        low_space = 0 <= storage.free_bytes < threshold
        state = "full" if storage.over_budget else ("low" if low_space else "ok")
        # Про диск говорим и при первом наблюдении: бот мог стартовать уже на
        # переполненном хранилище, и тогда «сменится» ему уже не с чего.
        if not self.state.note_health("_storage", state) or state == "ok":
            return
        gib = lambda value: self._t("storage.gib", value=value / 1024 ** 3)
        text = self._t("storage.warning", used=gib(storage.used_bytes),
                       budget=gib(storage.budget_bytes), free=gib(storage.free_bytes))
        await self.notify_console(text)

    async def notify_owner(self, text: str) -> int:
        """Личка владельцу от этого же бота. Возвращает число доставленных.

        Telegram не даёт боту писать первым: владелец, ни разу не нажавший
        «Старт», личку не получит — это видно в журнале, а не молча.
        """
        delivered = 0
        recipients = set(getattr(self.cfg, "owner_ids", None) or self.cfg.allowed_user_ids)
        if self.owner_id is not None:
            recipients.add(self.owner_id)
        for user_id in sorted(recipients):
            try:
                await self.tg.send_message(chat_id=user_id, text=text)
                delivered += 1
            except Exception as exc:
                self.log(f"личка владельцу {user_id} не ушла ({type(exc).__name__})")
        return delivered

    # --- пульт --------------------------------------------------------------
    async def notify_console(self, text: str) -> None:
        thread_id = await self.ensure_console()
        if thread_id is None:
            return
        try:
            await self.tg.send_message(chat_id=self.chat_id,
                                       message_thread_id=thread_id, text=text)
        except Exception as exc:
            self.log(f"пульт: сообщение не ушло ({type(exc).__name__})")

    async def ensure_console(self) -> int | None:
        """Служебная тема одна на весь пульт; её id переживает перезапуск."""
        if self.chat_id is None:
            return None  # группа ещё не привязана мастером
        saved = self.state.get_service(CONSOLE_KEY)
        if saved is not None:
            return int(saved)
        async with self._topic_lock:
            saved = self.state.get_service(CONSOLE_KEY)
            if saved is not None:
                return int(saved)
            try:
                created = await self.tg.create_forum_topic(
                    chat_id=self.chat_id, name=self._t(CONSOLE_TITLE),
                    icon_custom_emoji_id=TOPIC_ICON_CONSOLE)
            except Exception as exc:
                self.log(f"пульт: тему создать не удалось ({type(exc).__name__})")
                return None
            thread_id = int(getattr(created, "message_thread_id", None)
                            or created["message_thread_id"])
            self.state.set_service(CONSOLE_KEY, str(thread_id))
            self.log(f"пульт: создана тема {thread_id}")
            return thread_id

    async def console_text(self) -> str:
        try:
            cameras, storage = await asyncio.to_thread(self.bridge.registry)
        except BridgeError as exc:
            return self._t("console.unavailable", error=self._error(exc.code))
        lines = [self._t("console.header"), ""]
        for camera in cameras:
            state = self.health_state(camera)
            lines.append(f"{CONSOLE_STATE_MARK.get(state, '⚪️')} {camera.title} — "
                         f"{self._t(CONSOLE_STATE_TEXT[state]) if state in CONSOLE_STATE_TEXT else state}")
            if camera.last_motion_at:
                lines.append(self._t("console.last_motion", time=self._time(camera.last_motion_at)))
        if storage is not None:
            lines.append("")
            lines.append(self._t("console.storage", used=storage.used_bytes / 1024 ** 3,
                                 budget=storage.budget_bytes / 1024 ** 3,
                                 free=storage.free_bytes / 1024 ** 3))
        lines.append("")
        lines.append(self._t("console.updated", time=self._time(utcnow())))
        return "\n".join(lines)

    async def console_markup(self):
        """По кнопке на камеру: одно нажатие — пауза или возврат в работу."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        try:
            cameras = await asyncio.to_thread(self.bridge.cameras)
        except BridgeError:
            cameras = []
        # Панель пульта закреплена и перерисовывается раз в минуту, но если
        # перерисовка молча сбоит дольше часа (сеть, лимиты), часовые токены
        # убивали кнопки — тот же класс регрессии, что и с панелями камер.
        ttl = self.cfg.panel_callback_ttl_sec
        rows = []
        for camera in cameras:
            paused = camera.status in ("paused", "retired")
            action = "resume" if paused else "pause"
            label = f"{'▶️' if paused else '⏸'} {camera.title}"
            token = self.state.issue_callback(camera.camera_id, action, ttl)
            rows.append([InlineKeyboardButton(label, callback_data=f"cv:{action}:{token}")])
        refresh = self.state.issue_callback(CONSOLE_CAMERA, "panel", ttl)
        add = self.state.issue_callback(CONSOLE_CAMERA, "add", ttl)
        model = self.state.issue_callback(CONSOLE_CAMERA, "model", ttl)
        rows.append([InlineKeyboardButton(self._t("console.refresh"), callback_data=f"cv:panel:{refresh}"),
                     InlineKeyboardButton(self._t("console.add"), callback_data=f"cv:add:{add}")])
        thresholds = self.state.issue_callback(CONSOLE_CAMERA, "thr", ttl)
        rows.append([InlineKeyboardButton(self._t("console.model"), callback_data=f"cv:model:{model}"),
                     InlineKeyboardButton(self._t("console.thresholds"), callback_data=f"cv:thr:{thresholds}")])
        return InlineKeyboardMarkup(rows)

    async def ensure_topic_icons(self) -> None:
        """Один раз проставить иконки темам, заведённым до их появления.

        Отметка в `service` держит это разовым: правка темы всплывает у
        владельца как событие, и делать её на каждом старте нельзя.
        """
        if self.state.get_service(ICONS_KEY) is not None or self.chat_id is None:
            return
        targets = [(topic.thread_id, topic.title,
                    topic_icon(topic.title, topic.camera_id))
                   for topic in self.state.active_topics()]
        console = self.state.get_service(CONSOLE_KEY)
        if console is not None:
            targets.append((int(console), self._t(CONSOLE_TITLE), TOPIC_ICON_CONSOLE))
        done = True
        for index, (thread_id, title, icon) in enumerate(targets):
            if index:
                # Правки тем подряд Telegram отдаёт медленно и упирается в
                # лимит: без паузы последняя тема стабильно ловила таймаут.
                await asyncio.sleep(1)
            try:
                await self.tg.edit_forum_topic(chat_id=self.chat_id,
                                               message_thread_id=thread_id, name=title,
                                               icon_custom_emoji_id=icon)
            except Exception as exc:
                # «not modified» значит, что иконка уже на месте: это успех, а не
                # отказ. Остальные ошибки не должны мешать соседним темам, но и
                # отметку ставить нельзя — иначе тема останется без иконки навсегда.
                if "not modified" in str(exc).lower().replace("_", " "):
                    continue
                done = False
                self.log(f"иконка темы {thread_id}: не поставилась ({type(exc).__name__})")
        if done:
            self.state.set_service(ICONS_KEY, "done")

    async def refresh_console(self) -> None:
        """Одно закреплённое сообщение на пульт: лента не растёт от обновлений."""
        thread_id = await self.ensure_console()
        if thread_id is None:
            return
        text, markup = await self.console_text(), await self.console_markup()
        saved = self.state.get_service(CONSOLE_PANEL_KEY)
        if saved is not None:
            try:
                await self.tg.edit_message_text(
                    chat_id=self.chat_id, message_id=int(saved), text=text,
                    reply_markup=markup)
                return
            except Exception as exc:
                # Сообщение могли удалить руками — тогда заводим новое.
                self.log(f"пульт: правка не прошла ({type(exc).__name__}), пересоздаю")
        posted = await self.tg.send_message(
            chat_id=self.chat_id, message_thread_id=thread_id, text=text,
            reply_markup=markup)
        message_id = int(getattr(posted, "message_id", None) or posted["message_id"])
        self.state.set_service(CONSOLE_PANEL_KEY, str(message_id))
        try:
            await self.tg.pin_chat_message(chat_id=self.chat_id, message_id=message_id,
                                           disable_notification=True)
        except Exception as exc:
            self.log(f"пульт: закрепить не удалось ({type(exc).__name__})")

    # --- мастер первого запуска ------------------------------------------------
    def needs_owner(self) -> bool:
        return not self.cfg.allowed_user_ids and self.owner_id is None

    def setup_code(self) -> str | None:
        """Одноразовый код владельца; None — владелец уже есть. Код переживает
        перезапуск (тот же в журнале), исчезает после первого успешного /start."""
        if not self.needs_owner():
            self.state.delete_service(SETUP_CODE_KEY)
            return None
        code = self.state.get_service(SETUP_CODE_KEY)
        if not code:
            code = new_setup_code()
            self.state.set_service(SETUP_CODE_KEY, code)
        return code

    async def on_start(self, user_id: int | None, chat_id: int | None, chat_type: str,
                       args: list[str], language_code: str | None = None) -> str | None:
        """/start: в личке — назначение владельца по коду, в группе — привязка."""
        if user_id is None:
            return None
        if chat_type != "private":
            if not self.allowed(user_id):
                return None
            return await self.bind_group(chat_id, user_id)
        if self.needs_owner():
            code = self.state.get_service(SETUP_CODE_KEY) or ""
            given = (args[0] if args else "").strip().upper().replace("-", "")
            if self._code_attempts.get(user_id, 0) >= SETUP_CODE_ATTEMPTS:
                return None
            if not code or not given:
                return i18n.t("wizard.need_code", resolve_lang(language_code))
            import hmac

            if not hmac.compare_digest(given, code):
                self._code_attempts[user_id] = self._code_attempts.get(user_id, 0) + 1
                self.log(f"мастер: неверный код от {user_id}")
                return i18n.t("wizard.bad_code", resolve_lang(language_code))
            self.state.set_service(OWNER_KEY, str(user_id))
            self.state.delete_service(SETUP_CODE_KEY)
            if not self.state.get_service(LANG_KEY) and not getattr(self.cfg, "lang", ""):
                self.state.set_service(LANG_KEY, resolve_lang(language_code))
            self.log(f"мастер: владелец назначен ({user_id}), язык {self.lang}")
            return self._t("wizard.owner_set") + "\n\n" + self._t("wizard.add_to_group")
        if not self.allowed(user_id):
            return None
        if self.chat_id is None:
            return self._t("wizard.add_to_group")
        return self._t("wizard.private_help")

    async def on_bot_membership(self, chat_id: int, chat_type: str, actor_id: int | None,
                                status: str) -> None:
        """Бота добавили в группу или повысили до админа — довести настройку."""
        if chat_type not in ("group", "supergroup") or status in ("left", "kicked"):
            return
        if not self.allowed(actor_id):
            self.log(f"мастер: бота добавил в {chat_id} посторонний ({actor_id}) — без привязки")
            return
        answer = await self.bind_group(chat_id, actor_id)
        if answer:
            await self._say(chat_id, None, answer)

    async def on_chat_migrated(self, old_chat_id: int, new_chat_id: int) -> None:
        """Группа стала супергруппой (включили темы) — у неё новый id."""
        if self.state.get_service(CHAT_KEY) == str(old_chat_id):
            self.state.set_service(CHAT_KEY, str(new_chat_id))
            self.log(f"мастер: группа {old_chat_id} → {new_chat_id}")

    async def bind_group(self, chat_id: int | None, user_id: int | None) -> str | None:
        """Привязать группу (если ещё нет) и создать в ней пульт и темы камер.

        Без тем (форум выключен) или без прав администратора тему создать нельзя —
        тогда отвечаем, что включить; повтор — /setup в группе или само повышение
        бота до админа (Telegram пришлёт об этом обновление).
        """
        if chat_id is None:
            return None
        current = self.chat_id
        if current is not None and current != chat_id:
            return self._t("wizard.other_group")
        if current is None:
            self.state.set_service(CHAT_KEY, str(chat_id))
            self.log(f"мастер: группа {chat_id} привязана")
        problems = await self._group_problems(chat_id)
        if problems:
            return "\n".join([self._t("wizard.group_needs")] + problems
                             + [self._t("wizard.group_retry")])
        fresh = self.state.get_service(CONSOLE_KEY) is None
        thread_id = await self.ensure_console()
        if thread_id is None:
            return "\n".join([self._t("wizard.group_needs"), self._t("wizard.need_forum"),
                              self._t("wizard.need_admin"), self._t("wizard.group_retry")])
        await self.refresh_console()
        await self.sync_registry()
        if fresh:
            await self.notify_console(self._t("wizard.console_ready"))
            return self._t("wizard.group_ready")
        return self._t("wizard.group_already")

    async def _group_problems(self, chat_id: int) -> list[str]:
        """Чего не хватает группе: темы и права бота. Неизвестное — не помеха."""
        problems = []
        try:
            chat = await self.tg.get_chat(chat_id=chat_id)
            if getattr(chat, "is_forum", None) is False:
                problems.append(self._t("wizard.need_forum"))
        except Exception as exc:
            self.log(f"мастер: getChat {type(exc).__name__}")
        try:
            me = await self.tg.get_me()
            member = await self.tg.get_chat_member(chat_id=chat_id, user_id=me.id)
            if getattr(member, "status", "") != "administrator":
                problems.append(self._t("wizard.need_admin"))
            else:
                if getattr(member, "can_manage_topics", True) is False:
                    problems.append(self._t("wizard.need_topics_right"))
                if getattr(member, "can_delete_messages", True) is False:
                    problems.append(self._t("wizard.need_delete_right"))
        except Exception as exc:
            self.log(f"мастер: getChatMember {type(exc).__name__}")
        return problems

    async def _say(self, chat_id: int, thread_id: int | None, text: str) -> None:
        try:
            await self.tg.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text)
        except Exception as exc:
            self.log(f"сообщение в {chat_id} не ушло ({type(exc).__name__})")

    async def on_add(self, user_id: int | None, args: list[str]) -> str:
        """/add — поиск камер; /add <адрес> — камера вне поиска (IP или адрес потока)."""
        if not self.allowed(user_id):
            return self._t("no_access")
        if self.chat_id is None:
            return self._t("wizard.add_to_group")
        thread_id = await self.ensure_console()
        if thread_id is None:
            return self._t("wizard.group_needs")
        if args:
            return await self._accept_address(user_id, " ".join(args), thread_id)
        asyncio.create_task(self._discover())
        return self._t("add.searching")

    async def _accept_address(self, user_id: int | None, text: str, thread_id: int) -> str:
        """Адрес камеры от человека: IP или полный адрес потока; второй — поток детектора.

        Разбирает и проверяет адрес мост (бот к камерам не ходит и протоколов не
        знает); здесь — только форма: одно-два слова и без пароля внутри.
        """
        parts = (text or "").split()
        if not parts or len(parts) > 2 or any("|" in part for part in parts):
            return self._t("add.bad_address")
        if any("@" in part for part in parts):
            # Пароль — только отдельным сообщением, которое удаляется.
            return self._t("add.no_password_in_url")
        target, detect = parts[0], (parts[1] if len(parts) > 1 else "")
        return await self._ask_credentials(user_id, target, "", thread_id, detect)

    # --- модель детектора людей ------------------------------------------
    # Бот только показывает и заказывает: перезагрузку детекторов, проверку файла
    # и откат делает движок (engine/model_switch.py), бот ждёт исход и сообщает.
    async def on_model(self, user_id: int | None) -> str:
        """/model — меню модели детектора на пульте."""
        if not self.allowed(user_id):
            return self._t("no_access")
        if self.chat_id is None:
            return self._t("wizard.add_to_group")
        if await self.ensure_console() is None:
            return self._t("wizard.group_needs")
        asyncio.create_task(self.model_menu())
        return self._t("model.menu_sent")

    @staticmethod
    def _family_title(catalog: dict, family: str | None) -> str:
        for item in catalog.get("families") or []:
            if item.get("family") == family:
                return str(item.get("title") or family)
        return str(family or "?")

    def _model_reason(self, code: str | None) -> str:
        key = f"model.error.{code}"
        text = self._t(key)
        return self._t("model.error.person_model_load_failed") if text == key else text

    def _active_line(self, catalog: dict) -> str:
        active = catalog.get("active")
        if not isinstance(active, dict) or not active.get("family"):
            return self._t("model.active_none")
        return self._t("model.active", family=self._family_title(catalog, active.get("family")),
                       file=active.get("model") or "?", confidence=float(active.get("confidence") or 0))

    def model_text(self, catalog: dict) -> str:
        lines = [self._t("model.header"), self._active_line(catalog), "", self._t("model.families")]
        for item in catalog.get("families") or []:
            lines.append(self._t("model.family_line", title=item.get("title") or item.get("family"),
                                 file=item.get("default_file") or "?",
                                 confidence=float(item.get("confidence") or 0),
                                 license=item.get("license") or "?")
                         + (" " + self._t("model.bundled") if item.get("bundled") else ""))
        files = [str(m.get("model")) for m in catalog.get("models") or [] if m.get("model")]
        lines.append("")
        lines.append(self._t("model.files", files=", ".join(files)) if files else self._t("model.files_none"))
        switch = catalog.get("switch") if isinstance(catalog.get("switch"), dict) else {}
        target = switch.get("target") if isinstance(switch.get("target"), dict) else {}
        if switch.get("state") == "pending":
            lines.append(self._t("model.pending", family=self._family_title(catalog, target.get("family")),
                                 file=target.get("model") or "?"))
        elif switch.get("state") == "rolled_back":
            lines.append(self._t("model.last_rollback", family=self._family_title(catalog, target.get("family")),
                                 file=target.get("model") or "?", reason=self._model_reason(switch.get("error"))))
        lines.append("")
        lines.append(self._t("model.pick_family"))
        return "\n".join(lines)

    async def model_menu(self) -> None:
        """Меню на пульте: активная модель, семейства (кнопки), найденные файлы."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        thread_id = await self.ensure_console()
        if thread_id is None:
            return
        try:
            catalog = await asyncio.to_thread(self.bridge.detector_models)
        except BridgeError as exc:
            await self.notify_console(self._t("model.unavailable", error=self._error(exc.code)))
            return
        ttl = self.cfg.callback_ttl_sec
        active = (catalog.get("active") or {}).get("family")
        rows = [[InlineKeyboardButton(
            ("✅ " if item.get("family") == active else "") + str(item.get("title") or item.get("family")),
            callback_data=f"cv:mfam:{self.state.issue_callback(CONSOLE_CAMERA, 'mfam', ttl, str(item.get('family')))}")]
            for item in catalog.get("families") or [] if item.get("family")]
        rows.append([InlineKeyboardButton(
            self._t("console.thresholds"),
            callback_data=f"cv:thr:{self.state.issue_callback(CONSOLE_CAMERA, 'thr', ttl)}")])
        await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                   text=self.model_text(catalog), reply_markup=InlineKeyboardMarkup(rows))

    async def model_files(self, family: str) -> None:
        """Файлы для выбранного семейства: подходящие по имени — первыми."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        thread_id = await self.ensure_console()
        if thread_id is None:
            return
        try:
            catalog = await asyncio.to_thread(self.bridge.detector_models)
        except BridgeError as exc:
            await self.notify_console(self._t("model.unavailable", error=self._error(exc.code)))
            return
        info = next((f for f in catalog.get("families") or [] if f.get("family") == family), None)
        if info is None:
            await self.notify_console(self._t("model.error.person_model_family_unknown"))
            return
        title = self._family_title(catalog, family)
        models = [m for m in catalog.get("models") or [] if m.get("model")]
        # Имя файла — только подсказка порядка: чужой формат всё равно поймает загрузка.
        models.sort(key=lambda m: (m.get("hint") != family, str(m.get("model"))))
        ttl = self.cfg.callback_ttl_sec
        back = InlineKeyboardButton(
            self._t("model.back"),
            callback_data=f"cv:model:{self.state.issue_callback(CONSOLE_CAMERA, 'model', ttl)}")
        if not models:
            await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                       text=self._t("model.no_files", family=title),
                                       reply_markup=InlineKeyboardMarkup([[back]]))
            return
        active = catalog.get("active") if isinstance(catalog.get("active"), dict) else {}
        rows = []
        for item in models[:MODEL_FILES_SHOWN]:
            name = str(item["model"])
            current = active.get("family") == family and active.get("model") == name
            mark = "✅ " if current else ("" if item.get("hint") in (family, None) else "⚠️ ")
            token = self.state.issue_callback(CONSOLE_CAMERA, "mset", ttl, f"{family}/{name}")
            rows.append([InlineKeyboardButton((mark + name)[:64], callback_data=f"cv:mset:{token}")])
        rows.append([back])
        await self.tg.send_message(
            chat_id=self.chat_id, message_thread_id=thread_id,
            text=self._t("model.pick_file", family=title, confidence=float(info.get("confidence") or 0)),
            reply_markup=InlineKeyboardMarkup(rows))

    async def _switch_model(self, payload: str) -> str:
        family, _, name = payload.partition("/")
        if not family or not name:
            return self._t("callback.unknown")
        try:
            catalog = await asyncio.to_thread(self.bridge.detector_models)
            active = catalog.get("active") if isinstance(catalog.get("active"), dict) else {}
            if active.get("family") == family and active.get("model") == name:
                return self._t("model.already")
            request_id = await asyncio.to_thread(self.bridge.switch_detector_model, family, name)
        except BridgeError as exc:
            return self._t("model.not_started", reason=self._model_reason("person_model_missing")
                           if exc.code == "not_found" else self._error(exc.code))
        asyncio.create_task(self._await_model_switch(request_id, family, name))
        return self._t("model.switching", family=self._family_title(catalog, family), file=name)

    async def _await_model_switch(self, request_id: str, family: str, name: str) -> None:
        """Дождаться исхода у движка и сказать пользователю — и про успех, и про откат."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MODEL_WAIT_SEC
        catalog: dict = {}
        while loop.time() < deadline:
            await asyncio.sleep(MODEL_POLL_SEC)
            try:
                catalog = await asyncio.to_thread(self.bridge.detector_models)
            except BridgeError:
                continue
            switch = catalog.get("switch") if isinstance(catalog.get("switch"), dict) else {}
            if switch.get("request_id") != request_id:
                # Заявку перезаписала более новая (второе нажатие) — исход сообщит её ожидание.
                await self.notify_console(self._t("model.superseded", file=name))
                return
            title = self._family_title(catalog, family)
            if switch.get("state") == "ok":
                await self.notify_console(self._t("model.switched", family=title, file=name,
                                                  confidence=float((catalog.get("active") or {})
                                                                   .get("confidence") or 0)))
                return
            if switch.get("state") == "rolled_back":
                await self.notify_console(self._t("model.rolled_back", family=title, file=name,
                                                  reason=self._model_reason(switch.get("error")),
                                                  active=self._active_line(catalog)))
                return
        await self.notify_console(self._t("model.timeout", seconds=MODEL_WAIT_SEC,
                                          active=self._active_line(catalog) if catalog
                                          else self._t("model.active_none")))

    # --- порог детектора по камерам -----------------------------------------
    # Автокалибровку (шум сцены + подтверждённые проходы) считает движок
    # (engine/threshold_calibration.py); бот показывает итог, заказывает
    # калибровку и передаёт ручной порог.
    def threshold_text(self, view: dict) -> str:
        cameras = view.get("cameras") or []
        if not view.get("model"):
            return "\n".join([self._t("thr.header_none"), self._t("model.active_none")])
        lines = [self._t("thr.header", model=view["model"])]
        if not cameras:
            lines.append(self._t("thr.no_cameras"))
        for item in cameras:
            title, value = str(item.get("title") or item.get("camera_id")), float(item.get("threshold") or 0)
            auto = item.get("auto") if isinstance(item.get("auto"), dict) else None
            if item.get("source") == "manual":
                line = self._t("thr.line_manual", title=title, threshold=value)
            elif item.get("source") == "auto" and auto:
                line = self._t("thr.line_auto", title=title, threshold=value,
                               noise=float(auto.get("noise_level") or 0), frames=int(auto.get("noise_n") or 0),
                               passes=int(auto.get("passes_used") or 0))
            else:
                line = self._t("thr.line_start", title=title, threshold=value)
            lines.append(line)
            if item.get("state") == "collecting":
                lines.append(self._t("thr.collecting", collected=int(item.get("collected") or 0),
                                     needed=int(item.get("needed") or 0)))
        if view.get("pending"):
            lines.append(self._t("thr.pending"))
        lines += ["", self._t("thr.explain")]
        return "\n".join(lines)

    async def threshold_menu(self) -> None:
        """Пороги на пульте: итог по каждой камере, «Откалибровать», ручной ввод."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        thread_id = await self.ensure_console()
        if thread_id is None:
            return
        try:
            view = await asyncio.to_thread(self.bridge.detector_thresholds)
        except BridgeError as exc:
            await self.notify_console(self._t("thr.unavailable", error=self._error(exc.code)))
            return
        ttl = self.cfg.callback_ttl_sec
        issue = lambda action, payload="": self.state.issue_callback(CONSOLE_CAMERA, action, ttl, payload)
        rows = []
        cameras = view.get("cameras") or [] if view.get("model") else []
        if cameras:
            rows.append([InlineKeyboardButton(self._t("thr.button_calibrate_all"),
                                              callback_data=f"cv:thrcal:{issue('thrcal')}")])
        for item in cameras:
            camera_id = str(item.get("camera_id"))
            title = str(item.get("title") or camera_id)
            row = [InlineKeyboardButton(self._t("thr.button_set", title=title)[:64],
                                        callback_data=f"cv:thrset:{issue('thrset', camera_id)}")]
            if item.get("source") == "manual":
                row.append(InlineKeyboardButton(self._t("thr.button_auto"),
                                                callback_data=f"cv:thrauto:{issue('thrauto', camera_id)}"))
            rows.append(row)
        await self.tg.send_message(chat_id=self.chat_id, message_thread_id=thread_id,
                                   text=self.threshold_text(view),
                                   reply_markup=InlineKeyboardMarkup(rows) if rows else None)

    async def _threshold_camera(self, camera_id: str) -> tuple[dict, dict | None]:
        view = await asyncio.to_thread(self.bridge.detector_thresholds)
        item = next((c for c in view.get("cameras") or [] if c.get("camera_id") == camera_id), None)
        return view, item

    async def _calibrate(self, camera_id: str) -> str:
        try:
            started = await asyncio.to_thread(self.bridge.calibrate_detector, camera_id or None)
        except BridgeError as exc:
            return self._t("thr.unavailable", error=self._error(exc.code))
        return self._t("thr.calibrating", frames=int(started.get("frames") or 0))

    async def _ask_threshold(self, user_id: int | None, thread_id: int | None, camera_id: str) -> str:
        try:
            view, item = await self._threshold_camera(camera_id)
        except BridgeError as exc:
            return self._t("thr.unavailable", error=self._error(exc.code))
        if item is None or user_id is None or thread_id is None:
            return self._t("camera.retired")
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"thr|{camera_id}", INPUT_TTL_SEC)
        return await self._ask_for_text(thread_id, self._t(
            "thr.ask", title=item.get("title") or camera_id, threshold=float(item.get("threshold") or 0),
            min=float(view.get("min") or 0.05), max=float(view.get("max") or 0.95)))

    async def _apply_threshold(self, camera_id: str, text: str) -> str:
        raw = (text or "").strip().lower()
        if raw in THRESHOLD_AUTO_WORDS:
            return await self._set_threshold(camera_id, None)
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            value = -1.0
        if not 0.05 <= value <= 0.95:
            return self._t("thr.bad_value", min=0.05, max=0.95)
        return await self._set_threshold(camera_id, value)

    async def _set_threshold(self, camera_id: str, value: float | None) -> str:
        try:
            await asyncio.to_thread(self.bridge.set_detector_threshold, camera_id, value)
            view, item = await self._threshold_camera(camera_id)
        except BridgeError as exc:
            return self._t("thr.unavailable", error=self._error(exc.code))
        title = (item or {}).get("title") or camera_id
        if value is None:
            return self._t("thr.auto_done", title=title, threshold=float((item or {}).get("threshold") or 0))
        return self._t("thr.set_done", title=title, value=value, model=view.get("model") or "?")

    async def first_frame(self, camera_id: str) -> None:
        """Первый кадр новой камеры: дождаться потока и прислать снимок в её тему."""
        deadline = asyncio.get_running_loop().time() + FIRST_FRAME_WAIT_SEC
        while asyncio.get_running_loop().time() < deadline:
            try:
                camera = next((c for c in await asyncio.to_thread(self.bridge.cameras)
                               if c.camera_id == camera_id), None)
            except BridgeError:
                camera = None
            topic = self.state.topic_for(camera_id)
            if camera is not None and camera.status == "online" and topic is not None:
                await self._request_media(camera_id, topic.thread_id, "snapshot", None)
                await self.notify_console(self._t("add.first_frame", title=camera.title))
                return
            await asyncio.sleep(FIRST_FRAME_POLL_SEC)
        await self.notify_console(self._t("add.no_first_frame", camera_id=camera_id))

    async def set_language(self, user_id: int | None, args: list[str]) -> str:
        """/lang — показать язык и список; /lang <код> — сменить для всего бота."""
        if not self.allowed(user_id):
            return self._t("no_access")
        if not args:
            return self._t("lang.current", lang=self.lang, available=", ".join(i18n.available()))
        wanted = args[0].strip()
        if wanted.replace("_", "-").lower() not in {n.lower() for n in i18n.available()}:
            return self._t("lang.unknown", available=", ".join(i18n.available()))
        self.state.set_service(LANG_KEY, resolve_lang(wanted))
        if self.chat_id is not None:
            await self.refresh_console()
            await self.refresh_all_panels(force=True)
        return self._t("lang.changed", lang=self.lang)

    # --- команды ------------------------------------------------------------
    async def show_keyboard(self, thread_id: int | None) -> str:
        """Поставить постоянную клавиатуру: в теме камеры — с её именем в подсказке."""
        camera_id = self.state.camera_for_thread(thread_id) if thread_id is not None else None
        if camera_id is None:
            return self._t("keyboard.open_topic")
        topic = self.state.topic_for(camera_id)
        if topic is None or topic.status != "active":
            return self._t("camera.retired")
        await self.refresh_panel(camera_id)
        return self._t("keyboard.pinned", title=topic.title)

    async def menu_text(self) -> str:
        topics = self.state.active_topics()
        if not topics:
            return self._t("menu.empty")
        lines = [self._t("menu.header")]
        lines += [f"• {t.title} ({t.camera_id})" for t in topics]
        lines.append(self._t("menu.footer"))
        return "\n".join(lines)

    async def sync_registry(self) -> None:
        """Свести темы с реестром Bridge: новые заводим, снятые закрываем."""
        if self.chat_id is None:
            return  # темам негде жить, пока мастер не привязал группу
        try:
            cameras = await asyncio.to_thread(self.bridge.cameras)
        except BridgeError as exc:
            self.log(f"реестр недоступен: {exc.code}")
            return
        for camera in cameras:
            if camera.status == "retired":
                await self.retire_topic(camera.camera_id)
            else:
                await self.ensure_topic(camera.camera_id, camera.title, camera.site)
                await self.ensure_panel(camera.camera_id)
        await self.refresh_console()

    async def ensure_panel(self, camera_id: str) -> None:
        """Тема, заведённая до появления панели, получает её при первом же старте."""
        if self.state.panel_for(camera_id) is not None:
            await self.refresh_panel(camera_id)
            return
        topic = self.state.topic_for(camera_id)
        if topic is None or topic.status != "active":
            return
        posted = await self.tg.send_message(
            chat_id=self.chat_id, message_thread_id=topic.thread_id,
            text=self.panel_text(camera_id), reply_markup=self.control_markup(camera_id),
        )
        message_id = int(getattr(posted, "message_id", None) or posted["message_id"])
        self.state.bind_panel(camera_id, message_id)
        try:
            await self.tg.pin_chat_message(
                chat_id=self.chat_id, message_id=message_id, disable_notification=True
            )
        except Exception as exc:
            self.log(f"панель {camera_id}: закрепить не удалось ({type(exc).__name__})")
        await self.refresh_panel(camera_id)
