#!/usr/bin/env python3
"""Логика cctv-tg-bot: маршруты, кнопки, публикация кадров и событий движения.

Куда писать о камере, решает слой маршрутов (routes.py): тема камеры, тема
локации или плоский чат. Ядро работает с местом доставки `Dest` и тем Telegram
не касается.

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
import re
import uuid
from dataclasses import dataclass

from .. import __version__, i18n, settings
from .bridge import Bridge, BridgeError
from .events import Event
from .routes import CONSOLE_KEY, PRESETS, Dest, Router, hashtag  # noqa: F401 — CONSOLE_KEY: ключ пульта для тестов и стенда
from .state import State, valid_camera_id as valid_id
from .updates import UpdateChecker

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
# Склейка событий: событие камеры в течение стольких секунд после её поста в
# том же месте правит этот пост («+N за минуту», свежий кадр), а не шлёт новый.
# Отсчёт от первого поста — непрерывно активная камера даёт пост в минуту.
EVENT_MERGE_SEC = 60
CONSOLE_PANEL_KEY = "console_panel"
CONSOLE_CAMERA = "console"
CONSOLE_TITLE = "console.title"
# Карта камер (замена «Пульта»): до стольких камер кнопки всех камер на одном
# экране, больше — кнопки локаций, внутри локации — её камеры. Кнопок в ряду —
# столько, чтобы имя камеры читалось на телефоне.
MAP_PAGE_LIMIT = 12
MAP_ROW = 3
MAP_BUTTONS_MAX = 90  # лимит Telegram — 100 кнопок на сообщение, запас на служебные
# Карта места, открытая на карточке и забытая, сама возвращается к карте:
# закреп чата должен показывать все камеры, а не ту, что смотрели вчера.
HOME_VIEW_TTL_SEC = 600
# Карточки по /cam живут в ленте; перерисовываются последние столько на чат.
CARD_SCREENS_KEPT = 5
# Приглашение без группы (/invite): одноразовая ссылка на сутки.
INVITE_TTL_SEC = 86400
INVITE_PREFIX = "inv"
# Событие камеры, которой бот не знает, запускает сверку реестра не чаще раза в столько секунд.
UNKNOWN_SYNC_SEC = 60
# Шаг мастера «куда присылать»: от стольких камер советуем группу с темами (проект 0.3, 2.5).
WHERE_TOPICS_FROM = 4
LOCATION_MAX = 48
# Ответ «-» (или слово снятия на любом языке) на просьбу о локации снимает тег.
LOCATION_CLEAR_WORDS = "input.clear_words"
# Иконки тем берутся из набора getForumTopicIconStickers — он доступен ботам без
# premium. Камеры своей иконки в наборе нет, поэтому наблюдение — «глаза».
TOPIC_ICON_CAMERA = "5357121491508928442"    # 👀 — площадка неизвестна
TOPIC_ICON_CONSOLE = "5350554349074391003"  # 💻
# Площадка узнаётся по имени темы и названию площадки, а не по camera_id: на
# даче камер будет несколько, и каждая новая должна получать иконку дачи
# сама, без правки кода. Порядок важен — совпадает первое вхождение. Слова
# площадок — в каталоге (на всех языках сразу: имя камеры пишут как удобно).
SITE_ICONS = (
    ("topic.site_house_words", "5312486108309757006"),  # 🏠 дача
    ("topic.site_city_words", "5350548830041415279"),   # 🏛 город
)
ICONS_KEY = "topic_icons_v2"
# Мастер первого запуска: владелец, группа, язык и одноразовый код — в state.
OWNER_KEY = "owner_id"
CHAT_KEY = "chat_id"
LANG_KEY = "lang"
# Язык Telegram владельца (language_code) — отдельно от явного выбора /lang:
# явная настройка (/lang, CCTV_LANG) всегда сильнее подсказки клиента.
# Значение — «<user_id>:<язык>»: подсказку меняет только тот же владелец,
# иначе у двух владельцев с разными клиентами язык прыгал бы с каждым сообщением.
OWNER_LANG_KEY = "owner_lang"
SETUP_CODE_KEY = "setup_code"
# Команды меню «/» Telegram, подписи — command.<имя> в каталогах.
MENU_COMMANDS = ("menu", "cam", "add", "mode", "invite", "model", "lang", "setup", "help", "version")
# Закреплённое сообщение-прогресс мастера в личке владельца: «Шаг 1/3 … 3/3».
# Значение — «<chat_id>:<message_id>»; после третьего шага правится в «готово»
# и открепляется (закреп чата остаётся за картой камер).
PROGRESS_KEY = "wizard_progress"
PROGRESS_TEXT_KEY = "wizard_progress_text"
# Группа, которой бот сказал «не хватает тем/прав»: «<chat_id>:<с какого времени>:<что сказал>».
# Таймер сам перепроверяет её (темы включают без апдейта для бота), my_chat_member —
# сразу; одинаковый список недостач второй раз в группу не пишется.
GROUP_PENDING_KEY = "group_pending"
GROUP_RECHECK_FOR_SEC = 24 * 3600
# Инструкция «как включить темы и права» под клиент — тексты client.<клиент>.* в каталогах.
CLIENTS = ("android", "ios", "desktop")
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
# Ввод ручного порога: «auto» (на любом языке каталога) снимает его и
# возвращает автокалибровку.
THRESHOLD_AUTO_WORDS = "input.auto_words"
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
                 "drop": "input.drop", "creds": "input.creds", "loc": "input.location"}
# Пароль в пути адреса (XMEye /user=…&password=…): от человека такой адрес не принимаем.
PATH_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|pass)=")
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


def catalog_words(key: str) -> set[str]:
    """Список слов через запятую (ключ каталога) на всех языках, в нижнем регистре."""
    return {word.strip().lower() for lang in i18n.available()
            for word in i18n.t(key, lang).split(",") if word.strip()}


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


def _transient(exc: Exception) -> bool:
    """Сбой связи с Telegram (Bad Gateway, таймаут, flood wait), а не отказ по сути.

    BadRequest в PTB — тоже NetworkError, но это ответ Telegram «так нельзя»
    (сообщения нет, его не правят): его повтор не вылечит.
    """
    from telegram.error import BadRequest, NetworkError, RetryAfter

    return isinstance(exc, RetryAfter) or (isinstance(exc, NetworkError)
                                           and not isinstance(exc, BadRequest))


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_time(value: str | None, lang: str = i18n.DEFAULT_LANG, zone: dt.tzinfo | None = None) -> str:
    """Показанное время — в поясе установки (CCTV_TZ, без него UTC) и читаемое.

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
    moment = moment.astimezone(zone or dt.timezone.utc)
    abbr = moment.strftime("%Z")
    # Сокращение пояса — как у tzdata (CEST, MSK, +04), кроме переведённых в каталоге (МСК).
    label = i18n.t(f"time.zone.{abbr}", lang) if i18n.has(f"time.zone.{abbr}") else abbr
    return moment.strftime(i18n.t("time.format", lang, zone=label.replace("%", "%%")))


def human_clock(value: str | None, zone: dt.tzinfo | None = None) -> str:
    """Только часы (ЧЧ:ММ:СС) в поясе установки — для «последнее …» склеенного поста:
    дата и пояс уже есть в первой строке подписи."""
    try:
        moment = dt.datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return value or ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(zone or dt.timezone.utc).strftime("%H:%M:%S")


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
    for key, icon in SITE_ICONS:
        if any(needle in haystack for needle in catalog_words(key)):
            return icon
    return TOPIC_ICON_CAMERA


def resolve_lang(code: str | None) -> str:
    """language_code Telegram (ru, pt-br, en-US…) → язык из каталога, иначе en."""
    return i18n.resolve(code)


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
        # Пояс подписей; None — CCTV_TZ не задан (или неизвестен): UTC и строка в «Пульте».
        self.zone = settings.time_zone(getattr(cfg, "tz", ""))
        self.state = state
        self.bridge = bridge
        self.tg = tg
        self.log = log
        self._topic_lock = asyncio.Lock()
        # Сверка реестра идёт и по таймеру, и после /add, и по событию новой камеры:
        # параллельные сверки завели бы панель камеры дважды.
        self._sync_lock = asyncio.Lock()
        self._unknown_synced: dict[str, float] = {}
        self.routes = Router(state, tg, group=lambda: self.chat_id, recipients=self._recipients,
                             t=self._t, icon=topic_icon, log=log)
        self.merge_sec = getattr(cfg, "event_merge_sec", EVENT_MERGE_SEC)
        self._bridge_failures = 0
        self._code_attempts: dict[int, int] = {}
        self._activation_task: asyncio.Task | None = None
        self.updates = UpdateChecker(state, enabled=getattr(cfg, "update_check", True),
                                     url=getattr(cfg, "update_url", ""))
        self._username: str | None = None

    # --- доступ -----------------------------------------------------------
    def allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return (user_id in self.cfg.allowed_user_ids or user_id == self.owner_id
                or self.state.is_member(user_id))

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

    def _recipients(self) -> list[int]:
        """Люди установки: владелец первым, затем allow-list и приглашённые (/invite) —
        личные ленты плоского режима."""
        people = [self.owner_id] if self.owner_id is not None else []
        people += sorted(set(self.cfg.allowed_user_ids) - set(people))
        people += [user_id for user_id, _ in self.state.members() if user_id not in people]
        return people

    @property
    def lang(self) -> str:
        """Язык интерфейса: /lang → CCTV_LANG → language_code владельца → en."""
        owner_lang = (self.state.get_service(OWNER_LANG_KEY) or "").partition(":")[2]
        return i18n.choose(self.state.get_service(LANG_KEY), getattr(self.cfg, "lang", ""), owner_lang)

    def is_owner(self, user_id: int | None) -> bool:
        """Владелец — назначенный мастером или из CCTV_OWNER_IDS (по умолчанию allow-list)."""
        return user_id is not None and (user_id == self.owner_id
                                        or user_id in getattr(self.cfg, "owner_ids", ()))

    def note_language(self, user_id: int | None, language_code: str | None) -> None:
        """Запомнить язык Telegram владельца — подсказка, когда явного выбора нет."""
        found = i18n.match(language_code)
        if not found or not self.is_owner(user_id):
            return
        saved = self.state.get_service(OWNER_LANG_KEY) or ""
        saved_user = saved.partition(":")[0]
        if saved == f"{user_id}:{found}" or (saved_user.lstrip("-").isdigit() and saved_user != str(user_id)
                                              and self.is_owner(int(saved_user))):
            return
        self.state.set_service(OWNER_LANG_KEY, f"{user_id}:{found}")

    def _t(self, key: str, **params) -> str:
        return i18n.t(key, self.lang, **params)

    def _error(self, code: str | None) -> str:
        """Текст ошибки моста по коду; незнакомый код — общий текст сбоя."""
        return self._t(ERROR_TEXT.get(code or "", DEFAULT_ERROR_TEXT))

    def _refusal(self, reply: dict, fallback: str) -> str:
        """Отказ моста человеку: ключ каталога (error_key) — на языке бота, код
        ошибки — по ERROR_TEXT; мост без ключа — его текст как есть."""
        key = str(reply.get("error_key") or "")
        if key and i18n.has(key):
            params = reply.get("error_params")
            try:
                return self._t(key, **(params if isinstance(params, dict) else {}))
            except (KeyError, IndexError, ValueError):
                pass  # параметры не сошлись с каталогом — лучше текст моста, чем ничего
        error = str(reply.get("error") or "")
        if error in ERROR_TEXT:
            return self._error(error)
        return error or self._t(fallback)

    def _time(self, value: str | None) -> str:
        return human_time(value, self.lang, self.zone)

    # --- маршруты и темы ------------------------------------------------------
    async def _send(self, dest: Dest, text: str, **kwargs):
        """Сообщение в место доставки: тема, группа или личка — решил маршрут."""
        return await self.tg.send_message(**dest.kw(), text=text, **kwargs)

    async def ensure_topic(self, camera_id: str, title: str, site: str = "") -> list[Dest]:
        """Маршрут камеры. В режиме «тема на камеру» — одна тема на камеру с
        паспортом-панелью; повторная регистрация идемпотентна."""
        if not self.routes.ready():
            raise RuntimeError("no group is bound — nowhere to deliver camera events")
        async with self._topic_lock:
            dests, fresh = await self.routes.ensure(camera_id, title, site)
            if fresh is None:
                return dests
            passport = await self._send(
                fresh, self.passport_text(camera_id, title, site, self._t("status.registered")),
                reply_markup=self.control_markup(camera_id),
            )
            message_id = int(getattr(passport, "message_id", None) or passport["message_id"])
            self.state.bind_panel(camera_id, message_id)
            try:
                await self.tg.pin_chat_message(
                    chat_id=fresh.chat_id, message_id=message_id, disable_notification=True
                )
            except Exception as exc:  # нет права «Закрепление» — тема и панель всё равно есть
                self.log(f"панель {camera_id}: закрепить не удалось ({type(exc).__name__})")
            await self.refresh_panel(camera_id)
            return dests

    async def retire_topic(self, camera_id: str) -> None:
        """Камера снята: сказать об этом в её маршрут, своя тема закрывается архивом."""
        title = self.routes.title(camera_id)
        dests, own = self.routes.retire(camera_id)
        if not dests and own is None:
            return  # уже снята: сверка реестра повторяет это на каждом проходе
        for dest in dests:
            text = (self._t("topic.retired") if dest == own
                    else self._t("topic.retired_shared", title=title))
            try:
                await self._send(dest, text)
            except Exception as exc:
                self.log(f"камера {camera_id}: о снятии не сказано ({type(exc).__name__})")
        if own is not None:
            await self.routes.close(own)
        self.log(f"камера {camera_id}: снята")

    def passport_text(self, camera_id: str, title: str, site: str, status: str) -> str:
        lines = [self._t("panel.camera", title=title or camera_id), f"camera_id: {camera_id}"]
        if site:
            lines.append(self._t("panel.site", site=site))
        lines.append(self._t("panel.status", status=status))
        return "\n".join(lines)

    def panel_text(self, camera_id: str, camera=None, *, user_id: int | None = None,
                   viewer: Dest | None = None) -> str:
        """Карточка камеры: состояние камеры, детектора и подписки одним экраном.

        Одна и та же карточка — закреплённая панель своей темы камеры, экран карты
        (пульт, плоский чат, личка) и ответ на /cam. `viewer` — где её смотрят:
        в личке строка уведомлений — про этого человека, в группе — про всех.
        """
        title = camera.title if camera is not None else self.routes.title(camera_id)
        lines = [self._t("panel.camera", title=title), f"camera_id: {camera_id}"]
        location = self.routes.location(camera_id)
        if location:
            lines.append(self._t("panel.location", location=location))
        tags = self.routes.hashtags(camera_id)
        if tags:
            lines.append(tags)
        if camera is not None:
            if camera.site and camera.site != location:
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
        if viewer is not None and viewer.private:
            subscribed = viewer.chat_id in self.state.motion_subscribers(camera_id)
        else:
            subscribed = bool(self.state.motion_subscribers(camera_id))
        lines.append(self._t("panel.notifications", state=self._t(
            "panel.notify_on" if subscribed else "panel.notify_off")))
        return "\n".join(lines)

    async def refresh_panel(self, camera_id: str, *, user_id: int | None = None,
                            force: bool = False, cards: bool = True) -> None:
        """Перерисовать закреплённую панель. Тихо: пользователь её не заказывал.

        `force` нужен после выката новой версии: набор кнопок сменился, а текст
        панели — нет, и без принуждения в темах остались бы старые кнопки.
        """
        if cards and any(s.view == f"card:{camera_id}" for s in self.state.screens()):
            await self.refresh_cards(camera_id, force=force)
        panel = self.state.panel_for(camera_id)
        home = self.routes.camera_topic(camera_id)
        if panel is None or home is None:
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
                chat_id=home.chat_id, message_id=message_id, text=text,
                reply_markup=self.control_markup(camera_id, user_id=user_id, camera=camera),
            )
        except Exception as exc:  # чужая правка или удалённое сообщение не должны ронять ход
            self.log(f"панель {camera_id}: обновить не удалось ({type(exc).__name__})")
            return
        self.state.remember_panel_text(camera_id, text)

    async def refresh_cards(self, camera_id: str | None = None, *, force: bool = False) -> None:
        """Перерисовать открытые карточки (на картах и по /cam) — после действия с
        камерой или раз в минуту. Правка — только при смене текста, как у панели."""
        screens = [s for s in self.state.screens() if s.view.startswith("card:")
                   and (camera_id is None or s.view == f"card:{camera_id}")]
        if not screens:
            return
        registry = await self._registry()
        for screen in screens:
            await self._redraw(screen, registry, force=force)

    async def refresh_all_panels(self, *, force: bool = False) -> None:
        for camera in self.state.active_cameras():
            await self.refresh_panel(camera.camera_id, force=force, cards=False)
        await self.refresh_cards(force=force)

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
        result.append(Button(self._t("button.location"), f"cv:loc:{issue(camera_id, 'loc', ttl)}"))
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

    def control_markup(self, camera_id: str, *, user_id: int | None = None, camera=None,
                       back: bool = False):
        """Кнопки карточки (панели темы) по две в ряд; «◀ К карте» — на экране карты."""
        buttons = self.buttons(camera_id, user_id=user_id, camera=camera)
        markup = self._markup(buttons)
        if not back:
            return markup
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        token = self.state.issue_callback(CONSOLE_CAMERA, "map", self.cfg.panel_callback_ttl_sec, "")
        return InlineKeyboardMarkup(list(markup.inline_keyboard)
                                    + [[InlineKeyboardButton(self._t("map.back"), callback_data=f"cv:map:{token}")]])

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
    async def on_callback(self, user_id: int | None, thread: int | None, data: str,
                          *, chat_id: int | None = None, message_id: int | None = None) -> str:
        """Вернуть короткий текст для answerCallbackQuery. Побочный эффект — публикация.

        `thread` — тема Telegram, где нажали (как пришла в апдейте), `chat_id` —
        чат (None — группа установки); место нажатия разбирает слой маршрутов.
        `message_id` — нажатое сообщение: карта и карточка правят его на месте.
        """
        if not self.allowed(user_id):
            return self._t("no_access")
        parts = (data or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "cv":
            return self._t("callback.unknown")
        resolved = self.state.resolve_callback(parts[2])
        if resolved is None:
            return self._t("callback.expired")
        camera_id, action, center_at = resolved

        origin = self.routes.origin(chat_id, thread)
        on_console = self.routes.is_console(origin)
        if action == "panel":
            if not on_console:
                return self._t("callback.wrong_topic")
            await self.refresh_console()
            return self._t("console.refreshed")
        # Пресет и люди — дело владельца, а не места: /mode и /invite работают и в личке.
        if action == "mode":
            if not self.is_owner(user_id):
                return self._t("access.owner_only")
            if not center_at:
                await self.mode_menu(origin)
                return self._t("mode.menu_here")
            return await self.apply_mode(center_at)
        if action == "where":
            if not self.is_owner(user_id):
                return self._t("access.owner_only")
            return await self.choose_where(user_id, center_at or "", origin, message_id)
        if action == "client":  # инструкция под клиент — в любом месте, где показаны кнопки
            return await self.client_help(center_at or "", origin)
        if action == "kick":
            if not self.is_owner(user_id):
                return self._t("access.owner_only")
            return await self._kick(int(center_at or 0))
        if action == "map":
            if not on_console or origin is None:
                return self._t("callback.wrong_topic")
            await self._show(origin, message_id, f"map:{center_at}" if center_at else "map")
            return ""
        if camera_id == CONSOLE_CAMERA:
            # Заведение камеры идёт на пульте: маршрута у неё ещё нет, поэтому
            # проверка принадлежности к маршруту камеры здесь неприменима.
            if not on_console:
                return self._t("callback.wrong_topic")
            if action == "add":
                asyncio.create_task(self._discover())
                return self._t("add.searching_here")
            if action == "cand":
                return await self._ask_credentials(user_id, center_at or "", "", origin)
            if action in ("act", "actall"):
                return await self._ask_activation(user_id, origin, center_at or "")
            if action == "addr":
                self.state.expect_input(user_id, CONSOLE_CAMERA, "addr", INPUT_TTL_SEC)
                return await self._ask_for_text(origin, self._t("add.ask_address"))
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
                return await self._ask_threshold(user_id, origin, center_at or "")
            if action == "thrauto":
                return await self._set_threshold(center_at or "", None)
            return self._t("callback.unknown")

        if not self.routes.active(camera_id):
            return self._t("camera.retired")
        # Кнопка действует в месте своей камеры, на карте (пульт) и в личке
        # допущенного: чужое место — отказ. Нажатие без места (старый клиент) —
        # как раньше, в основной маршрут камеры.
        if (origin is not None and not on_console and not self.routes.unplaced(origin)
                and not self.routes.belongs(camera_id, origin) and not self.routes.personal(origin)):
            return self._t("callback.wrong_topic")
        if action == "card":
            # Карточка — в том же сообщении, где нажали камеру на карте.
            if origin is None:
                return self._t("callback.wrong_topic")
            await self._show(origin, message_id, f"card:{camera_id}")
            return ""
        # С карты (карточка на пульте, в плоском чате, в личке) ответ — туда же, где
        # нажали: кадр и просьба о вводе ложатся рядом с карточкой, а не в чужой теме.
        reply = origin if on_console else self.routes.reply_to(camera_id, origin)
        if reply is None:
            return self._t("camera.retired")
        return await self._perform(user_id, camera_id, reply, action, center_at)

    async def on_text(self, user_id: int | None, thread: int | None, text: str,
                      reply_to_message_id: int | None = None,
                      message_id: int | None = None, *, chat_id: int | None = None) -> str:
        """Нажатие постоянной кнопки приходит обычным текстом — камеру даёт тема камеры."""
        origin = self.routes.origin(chat_id, thread)
        # Telegram присылает нажатие selective ReplyKeyboard как reply на то
        # сообщение бота, которое показало клавиатуру. Поэтому кнопку нужно
        # распознать ДО общего пути «ответ на кадр»: иначе «📷 Кадр» ошибочно
        # трактуется как запрос клипа вокруг сообщения с клавиатурой.
        action = keyboard_action(text)
        if action is not None:
            if not self.allowed(user_id):
                return self._t("no_access")
            camera_id = self.routes.camera_at(origin)
            if camera_id is None:
                return self._t("keyboard.camera_topic_only")
            if not self.routes.active(camera_id):
                return self._t("camera.retired")
            return await self._perform(user_id, camera_id, origin, action, None)
        pending = None
        if reply_to_message_id is not None:
            # Reply на просьбу бота («пришлите логин и пароль», имя) — это ввод, а не
            # ответ на кадр: в личке и в группе без тем так отвечают чаще, чем в теме.
            if (self.routes.resolve_frame(reply_to_message_id, origin) is not None
                    or not self.allowed(user_id)):
                return await self.on_frame_reply(user_id, thread, reply_to_message_id, chat_id=chat_id)
            pending = self.state.take_input(user_id)
            if pending is None:
                return await self.on_frame_reply(user_id, thread, reply_to_message_id, chat_id=chat_id)
        if self.allowed(user_id):
            if pending is None:
                pending = self.state.take_input(user_id)
            if pending is not None:
                if pending[1].startswith(SECRET_INPUTS):
                    # Пароль стирается из чата до любого сетевого вызова: чат
                    # индексируется в личный RAG, и лишней секунды ему хватит.
                    await self._forget_message(message_id, origin)
                return await self._apply_input(user_id, pending[0], pending[1], text or "")
        return self._t(UNKNOWN_KEY if self.routes.forum else "hint.unknown_flat")

    async def on_frame_reply(self, user_id: int | None, thread: int | None,
                             replied_message_id: int, *, chat_id: int | None = None) -> str:
        """Текстовый reply на кадр запрашивает клип строго вокруг этого кадра."""
        if not self.allowed(user_id):
            return self._t("no_access")
        origin = self.routes.origin(chat_id, thread)
        found = self.routes.resolve_frame(replied_message_id, origin)
        # Буфер записи короткий (минуты), связь «фото → момент» живёт час: оба
        # отказа обязаны говорить, что делать дальше, а не загадкой из кода.
        if found is None:
            return self._t("frame.unknown" if self.routes.forum else "frame.unknown_flat")
        frame, where = found
        if frame.expired:
            return self._t("frame.expired")
        if origin != where:
            return self._t("frame.other_topic")
        if not self.routes.active(frame.camera_id):
            return self._t("camera.retired")
        return await self._request_media(frame.camera_id, where, "clip", frame.center_at)

    async def _perform(self, user_id: int | None, camera_id: str, dest: Dest,
                       action: str, center_at: str | None) -> str:
        """Одно действие — один путь, независимо от того, откуда пришло нажатие.

        `dest` — куда вернуть результат (кадр, клип, просьбу о вводе).
        """
        if action == "sub":
            enabled = self.state.toggle_motion(user_id, camera_id)
            await self.refresh_panel(camera_id, user_id=user_id)
            return self._t("notify.on" if enabled else "notify.off")
        if action == "stat":
            return await self._status(camera_id, user_id)
        if action in ("pause", "resume", "retire", "rename"):
            return await self._control(user_id, camera_id, dest, action)
        if action == "setup":
            return await self._setup(camera_id, dest)
        if action == "cand":
            return await self._ask_credentials(user_id, center_at or "", camera_id, dest)
        if action == "detect":
            return await self._toggle_detection(camera_id, center_at == "1")
        if action == "drop":
            self.state.expect_input(user_id, camera_id, "drop", INPUT_TTL_SEC)
            return await self._ask_for_text(dest, self._t(INPUT_PROMPTS["drop"]))
        if action == "loc":
            self.state.expect_input(user_id, camera_id, "loc", INPUT_TTL_SEC)
            current = self.routes.location(camera_id) or self._t("location.none")
            return await self._ask_for_text(dest, self._t(INPUT_PROMPTS["loc"], location=current))
        return await self._request_media(
            camera_id, dest, "clip" if action == "clip" else "snapshot", center_at
        )

    async def _ask_for_text(self, dest: Dest | None, text: str, *, post: bool = True) -> str:
        """Просьба прислать текст обязана остаться в чате, а не мигнуть тостом.

        Ответ на нажатие кнопки Telegram показывает всплывающей подсказкой:
        однострочную — на пару секунд, многострочную — окном с «ОК». И то и
        другое исчезает без следа, и человек, отложивший телефон на минуту,
        видит молчащий чат и решает, что кнопка сломана. Поэтому сама просьба
        уходит обычным сообщением туда, где нажали, и висит там до ввода; тем же
        текстом отвечаем и на нажатие — вторая строка делает подсказку окном.

        `post=False` — просьба в ответ на сообщение человека (/add <адрес>, адрес
        текстом): ответ и так ляжет в чат ответом на него, отдельное сообщение
        было бы второй копией той же просьбы (прогон на чистой VM, 09.10.2026).
        """
        if not post or dest is None:
            return text
        try:
            await self._send(dest, text)
        except Exception as exc:  # тема могла закрыться между нажатием и ответом
            self.log(f"просьба о вводе: {type(exc).__name__}")
        return text

    async def _control(self, user_id: int | None, camera_id: str, dest: Dest,
                       action: str) -> str:
        """Управление камерой из чата. Адреса и пароли этим путём не меняются.

        Пауза и имя применяются сразу; снятие с эксплуатации закрывает тему,
        поэтому требует подтверждения словом — случайное нажатие не должно
        уносить камеру из пульта.
        """
        if action == "rename":
            self.state.expect_input(user_id, camera_id, "rename", INPUT_TTL_SEC)
            return await self._ask_for_text(
                dest, self._t(INPUT_PROMPTS["rename"] if dest.in_topic else "input.rename_here",
                              minutes=INPUT_TTL_SEC // 60))
        if action == "retire":
            self.state.expect_input(user_id, camera_id, "retire", INPUT_TTL_SEC)
            # Своей темы нет (плоско, тема локации) — закрывать нечего, камера уходит с карты.
            own = self.routes.camera_topic(camera_id) is not None
            return await self._ask_for_text(dest, self._t(INPUT_PROMPTS["retire"] if own else "input.retire_map"))
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
            console = await self.ensure_console()
            if console is None:
                return self._t("wizard.group_needs")
            return await self._accept_address(user_id, text, console)
        if not self.routes.active(camera_id):
            return self._t("camera.retired")
        if kind == "drop":
            if text.strip().lower() not in confirm_words(CONFIRM_WORDS["drop"]):
                return self._t("drop.cancelled")
            return await self._delete_camera(camera_id)
        if kind == "loc":
            return await self._apply_location(camera_id, text)
        if kind == "rename":
            title = text.strip()[:64]
            if not title:
                return self._t("rename.empty")
            try:
                await asyncio.to_thread(self.bridge.set_camera_state, camera_id, "rename", title)
            except BridgeError as exc:
                return self._error(exc.code)
            await self.routes.rename(camera_id, title)
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
    async def _forget_message(self, message_id: int | None, where: Dest | None = None) -> None:
        """Стереть сообщение с паролем. Ни его текст, ни причина сбоя не логируются."""
        chat = where.chat_id if where is not None else self.chat_id
        if message_id is None or chat is None:
            return
        try:
            await self.tg.delete_message(chat_id=chat, message_id=message_id)
        except Exception as exc:
            self.log(f"сообщение с секретом удалить не удалось ({type(exc).__name__})")

    async def _discover(self) -> None:
        """Опрос сетей и список кандидатов на пульте. Работает фоном: /24 не мгновенен."""
        console = await self.ensure_console()
        if console is None:
            return
        try:
            started = await asyncio.to_thread(self.bridge.scan_start)
        except BridgeError as exc:
            await self.notify_console(self._t("scan.not_started", reason=self._error(exc.code)))
            return
        if not started.get("ok"):
            await self.notify_console(self._t(
                "scan.not_started",
                reason=self._refusal(started, "scan.reason_unknown")))
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
                reason=self._refusal(status, "scan.bridge_timeout")))
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
            await self._send_manual_setup(console, manual,
                                          None if fresh or act_rows else tail)
            if not fresh and not act_rows:
                return
        if not fresh and act_rows:
            await self._send(console, "\n".join(act_lines),
                             reply_markup=InlineKeyboardMarkup(act_rows + [tail]))
            return
        if not fresh:
            # Пусто — не тупик: чаще всего камеры в другой подсети (VLAN, второй
            # роутер), и подсказка называет причину и оба выхода.
            text = (self._t("scan.none_new", known=listed_known) if registered
                    else self._t("scan.none"))
            hint = self._t("scan.none_hint", networks=networks) if not registered else self._t("add.manual_hint")
            await self._send(console, text + "\n" + hint,
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
        await self._send(console, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    def setup_instruction(self, brand: str) -> str:
        """Как задать первый пароль камере этой марки вручную."""
        key = f"setup.{brand}" if brand in MANUAL_SETUP_BRANDS else "setup.generic"
        return self._t(key)

    async def _send_manual_setup(self, console: Dest, cameras: list[dict],
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
        await self._send(console, "\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(rows))

    async def _ask_credentials(self, user_id: int | None, host: str, camera_id: str,
                               dest: Dest | None, detect: str = "", *, post: bool = True) -> str:
        """Спросить логин и пароль. Пароль не идёт ни в какую модель и не хранится."""
        if not host:
            return self._t("creds.no_host")
        self.state.expect_input(user_id, camera_id or CONSOLE_CAMERA,
                                f"creds|{host}|{camera_id}|{detect}", INPUT_TTL_SEC)
        return await self._ask_for_text(dest, self._t(INPUT_PROMPTS["creds"], host=host), post=post)

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
                           error=self._refusal(result, "creds.no_answer"))
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
                               error=self._refusal(applied, "registry.refused"))
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
            if summary.get("path"):
                lines.append(self._t("detected.path", path=summary["path"],
                                     template=summary.get("template") or "generic"))
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
                           error=self._refusal(added, "registry.refused"))
        asyncio.create_task(self._sync_after_restart(first_frame=camera_id))
        self.log(f"камера {camera_id}: добавлена в реестр из чата")
        # Новая камера получает пресет установки: своя тема с панелью — только у «темы на камеру».
        key = "add.added" if self.routes.preset == "camera" else "add.added_card"
        return self._t(key, title=title, camera_id=camera_id)

    # --- активация новых Hikvision -------------------------------------------
    # Порядок шагов выбран ради пароля: сперва подтверждение словом (активация
    # необратима), и только потом сам пароль — так он живёт в одном обработчике
    # и не ждёт в памяти бота следующего сообщения. Сообщение с паролем
    # стирается до любого сетевого вызова (SECRET_INPUTS в on_text).
    async def _ask_activation(self, user_id: int | None, dest: Dest | None, payload: str) -> str:
        hosts = [h for h in payload.split(",") if h][:ACTIVATION_MAX_HOSTS]
        if not hosts:
            return self._t("creds.no_host")
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"actok|{','.join(hosts)}", INPUT_TTL_SEC)
        return await self._ask_for_text(dest, self._t(
            "act.confirm", count=len(hosts), hosts=", ".join(hosts),
            word=self._t(CONFIRM_WORDS["activate"])))

    async def _confirm_activation(self, user_id: int | None, hosts: str, text: str) -> str:
        if text.strip().lower() not in confirm_words(CONFIRM_WORDS["activate"]):
            return self._t("act.cancelled")
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"actpw|{hosts}", INPUT_TTL_SEC)
        # Слово подтверждения пришло сообщением: просьба о пароле — ответом на него.
        return self._t("act.ask_password", word=self._t(CONFIRM_WORDS["generate"]))

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
            lines.append(self._t("act.topics_soon" if self.routes.preset == "camera" else "act.cameras_soon"))
            asyncio.create_task(self._sync_after_restart(first_frame=list(added.values())))
        await self.notify_console("\n".join(lines))
        self.log(f"активация: {len(added)} камер в реестре из {len(results)}")

    async def _setup(self, camera_id: str, dest: Dest) -> str:
        """Карточка настройки там, где нажали: что записано в реестре и что можно сменить."""
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
        await self._send(dest, "\n".join(lines), reply_markup=await self.setup_markup(camera_id, config))
        # Тема камеры — «в теме камеры»; карта, личка, общая тема — карточка ниже, здесь же.
        return self._t("setup.card_sent" if dest == self.routes.camera_topic(camera_id) else "setup.card_here")

    async def _toggle_detection(self, camera_id: str, enable: bool) -> str:
        try:
            applied = await asyncio.to_thread(self.bridge.update_camera, camera_id,
                                              person_detection=enable)
        except BridgeError as exc:
            return self._error(exc.code)
        if not applied.get("ok"):
            return self._t("registry.not_saved",
                           error=self._refusal(applied, "registry.refused"))
        asyncio.create_task(self._sync_after_restart())
        return self._t("detect.enabled" if enable else "detect.disabled")

    async def _delete_camera(self, camera_id: str) -> str:
        try:
            removed = await asyncio.to_thread(self.bridge.delete_camera, camera_id)
        except BridgeError as exc:
            return self._error(exc.code)
        if not removed.get("ok"):
            return self._t("registry.not_deleted",
                           error=self._refusal(removed, "registry.refused"))
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
                self.log(f"сверка реестра после перезапуска отложена ({type(exc).__name__}: {exc})")
                await asyncio.sleep(5 * (attempt + 1))

    async def _status(self, camera_id: str, user_id: int | None = None) -> str:
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

    async def _request_media(self, camera_id: str, dest: Dest, kind: str,
                             center_at: str | None) -> str:
        """Заказать кадр или клип; результат вернётся в `dest` — тому, кто попросил."""
        request_id = str(uuid.uuid4())
        self.routes.remember_request(request_id, camera_id, kind, dest)
        try:
            await asyncio.to_thread(
                self.bridge.request_media, request_id, camera_id, kind, utcnow(), center_at
            )
        except BridgeError as exc:
            return self._error(exc.code)
        if kind != "snapshot":
            return self._t("media.clip_requested")
        # Тема — в форуме; в плоском чате и в личке кадр придёт туда же, где просили.
        return self._t("media.snap_requested" if dest.in_topic else "media.snap_requested_here")

    # --- события Bridge -----------------------------------------------------
    async def on_event(self, event: Event) -> None:
        """Единая точка входа для событий; повтор `event_id` не создаёт второй пост."""
        if not self.state.is_new_event(event.event_id):
            self.log(f"событие {event.type}: повтор event_id, пропуск")
            return
        if event.type == "camera.registered":
            if not self.routes.ready():
                return  # маршрут заведёт сверка реестра после привязки группы
            await self.ensure_topic(event.camera_id, event.title or event.camera_id, event.site or "")
        elif event.type == "camera.retired":
            await self.retire_topic(event.camera_id)
        elif event.type == "motion.detected":
            await self._on_motion(event)
        elif event.type == "media.ready":
            await self._on_media_ready(event)
        elif event.type == "media.failed":
            await self._on_media_failed(event)

    def _caption(self, camera_id: str, what: str, stamp: str | None, *, merged: int = 0,
                 last_at: str | None = None, extra: str = "") -> str:
        """Подпись поста о камере: что и когда, «+N за минуту», хэштеги камеры и локации.

        В своей теме камеры имя не нужно — его даёт тема; в общей теме локации и
        в плоском чате подпись начинается с имени камеры, иначе ленту не различить.
        """
        head = f"{what}: {self._time(stamp)}"
        if not self.routes.dedicated(camera_id):
            head = self._t("event.shared", title=self.routes.title(camera_id), event=head)
        lines = [head]
        if merged:
            lines.append(self._t("event.merged", count=merged, time=human_clock(last_at, self.zone)))
        if extra:
            lines.append(extra)
        tags = self.routes.hashtags(camera_id)
        if tags:
            lines.append(tags)
        return "\n".join(lines)

    async def _route_event(self, camera_id: str) -> list[Dest]:
        """Маршрут камеры события. Камера, которой бот ещё не знает, — новая: её
        первое движение обгоняет сверку после перезапуска цепочки (стенд 09.10.2026,
        «движение по неизвестной камере: пропуск» сразу после /add) — сверить и
        доставить, а не выбросить."""
        dests = self.routes.route(camera_id)
        if dests or self.routes.camera(camera_id) is not None or not self.routes.ready():
            return dests
        # Чужая камера (не из реестра) шлёт события подряд — сверка не чаще раза в минуту.
        now = asyncio.get_running_loop().time()
        if now - self._unknown_synced.get(camera_id, -UNKNOWN_SYNC_SEC) < UNKNOWN_SYNC_SEC:
            return dests
        self._unknown_synced[camera_id] = now
        await self.sync_registry()
        return self.routes.route(camera_id)

    async def _on_motion(self, event: Event) -> None:
        dests = await self._route_event(event.camera_id)
        if not dests:
            self.log(f"движение по неизвестной камере {event.camera_id}: пропуск")
            return
        self.state.count_event(self._today())  # «сегодня N событий» на карте
        # cctv_pipeline шлёт конкретное имя источника ("recorded_main_person_detector"),
        # а не голое "person_detector" — точное сравнение молчало и подписывало
        # подтверждённого человека как обычное «Движение».
        is_person = "person_detector" in (event.source or "")
        what = self._t("event.person" if is_person else "event.motion")
        stamp = event.occurred_at or utcnow()
        # Подписка управляет звуком, а не самим фактом записи в чат.
        silent = lambda dest: self.routes.silent_for(event.camera_id, dest)
        if event.media is None:
            for dest in dests:
                await self._send(dest, self._caption(event.camera_id, what, stamp,
                                                     extra=self._t("event.no_frame")),
                                 disable_notification=silent(dest))
            return
        await self._publish(event, dests, "snapshot",
                            lambda _dest: self._caption(event.camera_id, what, stamp),
                            silent=silent, person=is_person)

    async def _on_media_ready(self, event: Event) -> None:
        if event.request_id and not event.source_event_id:
            # Дедупликации по event_id мало: Bridge может повторить готовность
            # того же запроса под новым event_id. Ключ выдачи — request_id.
            pending = self.routes.take_request(event.request_id)
            if pending is None:
                self.log(f"media.ready {event.request_id}: запрос неизвестен или уже отдан")
                return
            dests = [pending[2]]
        else:
            # Клип движения бот не заказывал: его request_id — это event_id самого
            # движения, и строгая проверка отправляла каждый такой клип в мусор.
            dests = await self._route_event(event.camera_id)
            if not dests:
                self.log(f"media.ready по неизвестной камере {event.camera_id}: пропуск")
                return
        kind = event.kind or "snapshot"
        stamp = event.captured_at or event.occurred_at or utcnow()
        what = self._t("media.frame" if kind == "snapshot" else "media.clip")
        await self._publish(event, dests, kind, lambda _dest: self._caption(event.camera_id, what, stamp),
                            silent=lambda _dest: False)

    async def _on_media_failed(self, event: Event) -> None:
        """Отказ Bridge обязан вернуться туда, откуда просили: иначе кнопка выглядит сломанной."""
        dests: list[Dest] = []
        if event.request_id:
            pending = self.routes.take_request(event.request_id)
            if pending is not None:
                dests = [pending[2]]
        if not dests:
            dests = self.routes.route(event.camera_id)
            if not dests:
                self.log(f"media.failed по неизвестной камере {event.camera_id}: пропуск")
                return
        what = self._t("media.clip" if event.kind == "clip" else "media.frame")
        reason = self._error(event.error or "")
        for dest in dests:
            await self._send(dest, self._t("media.failed", what=what, reason=reason))

    async def _publish(self, event: Event, dests: list[Dest], kind: str, caption,
                       *, silent, person: bool | None = None) -> None:
        """Скачать медиа один раз и довезти во все места маршрута.

        `caption` и `silent` — функции места (звук в личке — свой у каждого).
        `person` задан у событий движения: они склеиваются за EVENT_MERGE_SEC.
        Отметка дедупликации снимается, только если не доехало никуда: повтор
        моста иначе продублировал бы событие у тех, кому оно уже пришло.
        """
        assert event.media is not None
        try:
            media = await asyncio.to_thread(
                self.bridge.download, event.media.url, kind=kind,
                expected_sha256=event.media.sha256, declared_bytes=event.media.bytes,
            )
        except BridgeError as exc:
            for dest in dests:
                await self._send(dest, f"{caption(dest)}\n{self._error(exc.code)}",
                                 disable_notification=silent(dest))
            return
        try:
            delivered = 0
            for dest in dests:
                if person is not None and await self._merge_motion(event, dest, media.path, person):
                    delivered += 1
                    continue
                if await self._send_media_with_retry(event, dest, kind, caption(dest), media.path,
                                                     silent=silent(dest), person=person):
                    delivered += 1
            if not delivered:
                self.state.forget_event(event.event_id)
        finally:
            # Временный файл не переживает публикацию ни при успехе, ни при ошибке.
            try:
                os.unlink(media.path)
            except OSError:
                pass

    async def _merge_motion(self, event: Event, dest: Dest, path: str, person: bool) -> bool:
        """Вклеить событие в недавний пост этой камеры здесь же: свежий кадр и «+N».

        True — вклеено. Пост удалили руками или Telegram отказал — False, и
        событие уходит новым постом: склейка не вправе терять события.
        """
        post = self.routes.recent_post(event.camera_id, dest, self.merge_sec)
        if post is None:
            return False
        captured = event.captured_at or event.occurred_at or utcnow()
        what = self._t("event.person" if person or post.person else "event.motion")
        caption = self._caption(event.camera_id, what, post.first_at, merged=post.merged + 1,
                                last_at=event.occurred_at or captured)
        from telegram import InputMediaPhoto

        try:
            with open(path, "rb") as handle:
                await self.tg.edit_message_media(
                    chat_id=dest.chat_id, message_id=post.message_id,
                    media=InputMediaPhoto(handle, caption=caption),
                    reply_markup=self.frame_markup(event.camera_id, captured),
                    read_timeout=self.cfg.tg_media_timeout_sec,
                    write_timeout=self.cfg.tg_media_timeout_sec,
                )
        except Exception as exc:
            self.log(f"склейка {event.camera_id}: правка поста не прошла ({type(exc).__name__}), новый пост")
            self.routes.forget_post(event.camera_id, dest)
            return False
        self.routes.merge_post(event.camera_id, dest, person)
        # Ответ на склеенный пост — клип вокруг его свежего кадра.
        self.routes.remember_frame(post.message_id, event.camera_id, dest, captured,
                                   self.cfg.callback_ttl_sec)
        return True

    async def _send_media_with_retry(self, event: Event, dest: Dest, kind: str,
                                     caption: str, path: str, *, silent: bool,
                                     person: bool | None = None) -> bool:
        """Довезти медиа до места или честно сказать, что событие потеряно. True — доехало.

        Чат Telegram — единственное место, где событие хранится: диск моста
        транзитный. Обрыв сети или лимит Telegram молча вырезал бы кусок архива,
        поэтому отправка повторяется, а окончательный отказ видно в самом чате.
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
                                    **dest.kw(),
                                    video=handle, caption=caption, disable_notification=silent,
                                    supports_streaming=True, thumbnail=thumb, **meta,
                                    # Имя задаёт mime загрузки: без .mp4 Bot API кладёт
                                    # документ, и Android рисует файл вместо плеера.
                                    filename=CLIP_FILENAME,
                                    read_timeout=self.cfg.tg_media_timeout_sec,
                                    write_timeout=self.cfg.tg_media_timeout_sec,
                                )
                        else:
                            captured = event.captured_at or event.occurred_at
                            published = await self.tg.send_photo(
                                **dest.kw(),
                                photo=handle, caption=caption, disable_notification=silent,
                                read_timeout=self.cfg.tg_media_timeout_sec,
                                write_timeout=self.cfg.tg_media_timeout_sec,
                                reply_markup=self.frame_markup(event.camera_id, captured),
                            )
                            message_id = int(getattr(published, "message_id", None)
                                             or published["message_id"])
                            self.routes.remember_frame(
                                message_id, event.camera_id, dest, captured or utcnow(),
                                self.cfg.callback_ttl_sec,
                            )
                            if person is not None:
                                self.routes.remember_post(event.camera_id, dest, message_id,
                                                          event.occurred_at or utcnow(), person)
                    return True
                except Exception as exc:  # сеть, лимит Telegram, временная ошибка API
                    last = exc
                    self.log(f"доставка {kind} {event.camera_id}: попытка "
                             f"{attempt + 1}/{DELIVERY_ATTEMPTS} не удалась ({type(exc).__name__})")
                    if attempt + 1 < DELIVERY_ATTEMPTS:
                        await asyncio.sleep(DELIVERY_BACKOFF_SEC[min(attempt, len(DELIVERY_BACKOFF_SEC) - 1)])
            # Все попытки исчерпаны: событие не должно тихо исчезнуть из архива.
            try:
                await self._send(dest, self._t(
                    "delivery.failed", caption=caption, attempts=DELIVERY_ATTEMPTS,
                    error=type(last).__name__ if last else self._t("delivery.error")))
            except Exception as exc:
                self.log(f"предупреждение о потере не ушло: {type(exc).__name__}")
            return False
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
            dests = self.routes.route(camera.camera_id)
            if not dests:
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
            if not self.routes.dedicated(camera.camera_id):
                text = self._t("event.shared", title=camera.title, event=text)
            for dest in dests:
                try:
                    await self._send(dest, f"{text}\n{self._time(utcnow())}")
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
        console = await self.ensure_console()
        if console is None:
            return
        try:
            await self._send(console, text)
        except Exception as exc:
            self.log(f"пульт: сообщение не ушло ({type(exc).__name__})")

    async def ensure_console(self) -> Dest | None:
        """Пульт: служебная тема в форуме (одна, переживает перезапуск), в плоском
        режиме — сам чат. None — группа ещё не привязана мастером."""
        if not self.routes.ready():
            return None
        existing = self.routes.console()
        if existing is not None:
            return existing
        async with self._topic_lock:
            return await self.routes.ensure_console(self._t(CONSOLE_TITLE), TOPIC_ICON_CONSOLE)

    # --- карта камер (замена «Пульта») ------------------------------------------
    async def _registry(self):
        """(камеры, хранилище) или BridgeError — один запрос моста на всю перерисовку."""
        try:
            return await asyncio.to_thread(self.bridge.registry)
        except BridgeError as exc:
            return exc

    def _today(self) -> str:
        return dt.datetime.now(self.zone or dt.timezone.utc).date().isoformat()

    def _sections(self, cameras) -> list[tuple[str, list]]:
        """Камеры по локациям (тег камеры, иначе площадка): локации по имени,
        камеры без локации — последней секцией."""
        groups: dict[str, list] = {}
        for camera in cameras:
            groups.setdefault(self.routes.location(camera.camera_id), []).append(camera)
        return sorted(groups.items(), key=lambda item: (item[0] == "", item[0].lower()))

    def _camera_mark(self, camera) -> str:
        return CONSOLE_STATE_MARK.get(self.health_state(camera), "⚪️")

    def map_text(self, cameras, storage, viewer: Dest | None = None) -> str:
        """Карта: сводка, камеры секциями по локациям, хранилище и время правки."""
        live = [c for c in cameras if c.status != "retired"]
        online = sum(1 for c in live if c.status == "online")
        lines = [self._t("map.header", cameras=len(live), online=online,
                         events=self.state.events_on(self._today()))]
        sections = self._sections(cameras)
        titled = len(sections) > 1 or (sections and sections[0][0] != "")
        viewer = viewer or (Dest(self.chat_id) if self.chat_id is not None else None)
        for location, group in sections:
            lines.append("")
            if titled:
                lines.append(self._t("map.section", location=location or self._t("route.no_location")))
            for camera in group:
                state = self.health_state(camera)
                quiet = (" 🔕" if viewer is not None and camera.status != "retired"
                         and self.routes.silent_for(camera.camera_id, viewer) else "")
                lines.append(f"{CONSOLE_STATE_MARK.get(state, '⚪️')} {camera.title}{quiet} — "
                             f"{self._t(CONSOLE_STATE_TEXT[state]) if state in CONSOLE_STATE_TEXT else state}")
                if camera.last_motion_at:
                    lines.append(self._t("console.last_motion", time=self._time(camera.last_motion_at)))
        if storage is not None:
            lines.append("")
            lines.append(self._t("console.storage", used=storage.used_bytes / 1024 ** 3,
                                 budget=storage.budget_bytes / 1024 ** 3,
                                 free=storage.free_bytes / 1024 ** 3))
        lines.append("")
        if self.zone is None:
            lines.append(self._t("console.tz_unset"))
        available = self.updates.available()
        if available:
            lines.append(self._t("update.available", version=available))
        lines.append(self._t("console.updated", time=self._time(utcnow())))
        return "\n".join(lines)[:4000]

    def map_markup(self, cameras, page: str | None = None):
        """Кнопки карты: камеры (нажатие — карточка в этом же сообщении), а при
        многих камерах — локации страницами; внизу — пульт: камера, модель, пороги, режим.

        Токены живут неделю: карта закреплена и перерисовывается раз в минуту, но
        если перерисовка молча сбоит дольше часа (сеть, лимиты), часовые токены
        убивали кнопки — тот же класс регрессии, что и с панелями камер.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        ttl = self.cfg.panel_callback_ttl_sec
        issue = self.state.issue_callback
        live = [c for c in cameras if c.status != "retired" and valid_id(c.camera_id)]
        sections = self._sections(live)
        no_location = self._t("route.no_location")
        if page is None and len(live) > MAP_PAGE_LIMIT and len(sections) > 1:
            buttons = [Button(self._t("map.page", location=location or no_location, count=len(group))[:40],
                              f"cv:map:{issue(CONSOLE_CAMERA, 'map', ttl, '@' + location)}")
                       for location, group in sections]
        else:
            chosen = live if page is None else next((g for loc, g in sections if loc == page), [])
            buttons = [Button(f"{self._camera_mark(c)} {c.title}"[:40],
                              f"cv:card:{issue(c.camera_id, 'card', ttl)}")
                       for c in chosen[:MAP_BUTTONS_MAX]]
        rows = [buttons[i:i + MAP_ROW] for i in range(0, len(buttons), MAP_ROW)]
        if page is not None:
            rows.append([Button(self._t("map.back"), f"cv:map:{issue(CONSOLE_CAMERA, 'map', ttl, '')}")])
        rows.append([Button(self._t("console.refresh"), f"cv:panel:{issue(CONSOLE_CAMERA, 'panel', ttl)}"),
                     Button(self._t("console.add"), f"cv:add:{issue(CONSOLE_CAMERA, 'add', ttl)}")])
        rows.append([Button(self._t("console.model"), f"cv:model:{issue(CONSOLE_CAMERA, 'model', ttl)}"),
                     Button(self._t("console.thresholds"), f"cv:thr:{issue(CONSOLE_CAMERA, 'thr', ttl)}")])
        rows.append([Button(self._t("map.mode"), f"cv:mode:{issue(CONSOLE_CAMERA, 'mode', ttl, '')}")])
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(b.text, callback_data=b.data) for b in row] for row in rows])

    async def console_text(self, viewer: Dest | None = None) -> str:
        registry = await self._registry()
        if isinstance(registry, BridgeError):
            return self._t("console.unavailable", error=self._error(registry.code))
        return self.map_text(*registry, viewer=viewer)

    async def console_markup(self, page: str | None = None):
        registry = await self._registry()
        return self.map_markup([] if isinstance(registry, BridgeError) else registry[0], page)

    async def _render(self, view: str, dest: Dest, registry) -> tuple[str, object]:
        """Текст и кнопки экрана: карта (её страница) или карточка камеры."""
        failed = isinstance(registry, BridgeError)
        cameras, storage = ([], None) if failed else registry
        if view.startswith("card:"):
            camera_id = view[len("card:"):]
            camera = next((c for c in cameras if c.camera_id == camera_id), None)
            if self.routes.active(camera_id):
                user_id = dest.chat_id if dest.private else None
                return (self.panel_text(camera_id, camera, viewer=dest),
                        self.control_markup(camera_id, user_id=user_id, camera=camera,
                                            back=self.routes.is_console(dest)))
            view = "map"
        page = view[len("map:@"):] if view.startswith("map:@") else None
        if failed:
            text = self._t("console.unavailable", error=self._error(registry.code))
        else:
            text = self.map_text(cameras, storage, viewer=dest)
        return text, self.map_markup(cameras, page)

    async def _redraw(self, screen, registry, *, force: bool = False, view: str | None = None) -> bool:
        """Правка экрана на месте. False — сообщение не правится (удалили руками)."""
        dest = self.routes.screen_place(screen)
        view = view or screen.view
        text, markup = await self._render(view, dest, registry)
        if text == screen.rendered and view == screen.view and not force:
            return True
        try:
            await self.tg.edit_message_text(chat_id=dest.chat_id, message_id=screen.message_id,
                                            text=text, reply_markup=markup)
        except Exception as exc:
            if "not modified" in str(exc).lower().replace("_", " "):
                return True
            if _transient(exc):
                # Сбой связи с Telegram — не «сообщение удалили»: экран остаётся,
                # правка повторится в следующем цикле. Иначе каждый Bad Gateway
                # заводил новую закреплённую карту рядом со старой.
                self.log(f"экран {dest.chat_id}/{screen.message_id}: правка отложена ({type(exc).__name__})")
                return True
            self.log(f"экран {dest.chat_id}/{screen.message_id}: правка не прошла ({type(exc).__name__})")
            return False
        if view != screen.view:
            self.routes.remember_screen(dest, screen.message_id, view,
                                       home=screen.home)
        self.state.remember_screen_text(dest.chat_id, screen.message_id, text)
        return True

    async def _show(self, dest: Dest, message_id: int | None, view: str) -> None:
        """Показать экран: правкой нажатого сообщения («один экран»), иначе новым
        сообщением в `dest` — оно и станет экраном (карточка по /cam)."""
        registry = await self._registry()
        if message_id is not None:
            screen = self.state.screen(dest.chat_id, message_id)
            if screen is None:
                # Экран до 0.3.0 (закреплённый пульт) или потерянная запись — заводим.
                self.routes.remember_screen(dest, message_id, "map",
                                           home=self._is_home_message(dest, message_id))
                screen = self.state.screen(dest.chat_id, message_id)
            if await self._redraw(screen, registry, force=True, view=view):
                return
            self.state.forget_screen(dest.chat_id, message_id)
        text, markup = await self._render(view, dest, registry)
        posted = await self._send(dest, text, reply_markup=markup)
        message_id = int(getattr(posted, "message_id", None) or posted["message_id"])
        self.routes.remember_screen(dest, message_id, view)
        self.state.remember_screen_text(dest.chat_id, message_id, text)
        self.state.prune_screens(dest.chat_id, CARD_SCREENS_KEPT)

    def _is_home_message(self, dest: Dest, message_id: int) -> bool:
        return (dest == self.routes.console() and self.routes.forum
                and self.state.get_service(CONSOLE_PANEL_KEY) == str(message_id))

    async def ensure_topic_icons(self) -> None:
        """Один раз проставить иконки темам, заведённым до их появления.

        Отметка в `service` держит это разовым: правка темы всплывает у
        владельца как событие, и делать её на каждом старте нельзя.
        """
        if self.state.get_service(ICONS_KEY) is not None or self.chat_id is None:
            return
        targets = [(dest, title, topic_icon(title, camera_id) if camera_id else topic_icon(title))
                   for dest, title, camera_id in self.routes.forum_topics()]
        console = self.routes.console() if self.routes.forum else None
        if console is not None:
            targets.append((console, self._t(CONSOLE_TITLE), TOPIC_ICON_CONSOLE))
        done = True
        for index, (dest, title, icon) in enumerate(targets):
            if index:
                # Правки тем подряд Telegram отдаёт медленно и упирается в
                # лимит: без паузы последняя тема стабильно ловила таймаут.
                await asyncio.sleep(1)
            try:
                await self.routes.edit_topic(dest, title, icon)
            except Exception as exc:
                # «not modified» значит, что иконка уже на месте: это успех, а не
                # отказ. Остальные ошибки не должны мешать соседним темам, но и
                # отметку ставить нельзя — иначе тема останется без иконки навсегда.
                if "not modified" in str(exc).lower().replace("_", " "):
                    continue
                done = False
                self.log(f"иконка темы {title}: не поставилась ({type(exc).__name__})")
        if done:
            self.state.set_service(ICONS_KEY, "done")

    async def refresh_console(self, *, force: bool = False) -> None:
        """Карта в каждом своём месте — одно закреплённое сообщение, лента не растёт.

        Место карты — тема пульта (форум), сама группа (плоско) или личка каждого
        допущенного (плоско без группы). Карта, открытая на карточке, остаётся
        карточкой, пока её смотрят; забытая — через HOME_VIEW_TTL_SEC снова карта.
        """
        console = await self.ensure_console()
        if console is None:
            return
        registry = await self._registry()
        for place in self.routes.map_places():
            await self._refresh_home(place, registry, force=force)

    async def _refresh_home(self, place: Dest, registry, *, force: bool = False) -> None:
        screen = self.routes.home_screen(place)
        if screen is None and place == self.routes.console() and self.routes.forum:
            saved = self.state.get_service(CONSOLE_PANEL_KEY)
            if saved is not None:
                # Пульт до 0.3.0: его закреплённое сообщение становится картой.
                self.routes.remember_screen(place, int(saved), "map", home=True)
                screen = self.routes.home_screen(place)
        if screen is not None:
            view = screen.view
            if view != "map" and self.state.now() - screen.shown_at > HOME_VIEW_TTL_SEC:
                view = "map"
            if await self._redraw(screen, registry, force=force or view.startswith("map"), view=view):
                return
            # Сообщение могли удалить руками — тогда заводим новое.
            self.log(f"карта {place.chat_id}: правка не прошла, пересоздаю")
            self.state.forget_screen(place.chat_id, screen.message_id)
        text, markup = await self._render("map", place, registry)
        posted = await self._send(place, text, reply_markup=markup)
        message_id = int(getattr(posted, "message_id", None) or posted["message_id"])
        self.routes.remember_screen(place, message_id, "map", home=True)
        self.state.remember_screen_text(place.chat_id, message_id, text)
        if self.routes.forum and place == self.routes.console():
            self.state.set_service(CONSOLE_PANEL_KEY, str(message_id))
        try:
            await self.tg.pin_chat_message(chat_id=place.chat_id, message_id=message_id,
                                           disable_notification=True)
        except Exception as exc:
            self.log(f"карта: закрепить не удалось ({type(exc).__name__})")

    async def _retire_screens(self) -> None:
        """После смены пресета карта переезжает: прежние карты вне новых мест
        честно говорят об этом и больше не перерисовываются."""
        places = set(self.routes.map_places())
        for screen in self.state.screens():
            dest = self.routes.screen_place(screen)
            if not screen.home or dest in places:
                continue
            self.state.forget_screen(screen.chat_id, screen.message_id)
            try:
                await self.tg.edit_message_text(chat_id=dest.chat_id, message_id=screen.message_id,
                                                text=self._t("map.moved"), reply_markup=None)
            except Exception as exc:
                self.log(f"старая карта {dest.chat_id}: правка не прошла ({type(exc).__name__})")

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
                       args: list[str], language_code: str | None = None,
                       name: str = "") -> str | None:
        """/start: в личке — назначение владельца по коду или вход по приглашению
        (/invite), в группе — привязка."""
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
            # Владельца ещё нет: явный язык установки (/lang, CCTV_LANG), иначе язык клиента.
            lang = i18n.choose(self.state.get_service(LANG_KEY), getattr(self.cfg, "lang", ""), language_code)
            if not code or not given:
                return i18n.t("wizard.need_code", lang)
            import hmac

            if not hmac.compare_digest(given, code):
                self._code_attempts[user_id] = self._code_attempts.get(user_id, 0) + 1
                self.log(f"мастер: неверный код от {user_id}")
                return i18n.t("wizard.bad_code", lang)
            self.state.set_service(OWNER_KEY, str(user_id))
            self.state.delete_service(SETUP_CODE_KEY)
            self.state.delete_service(OWNER_LANG_KEY)
            self.note_language(user_id, language_code)
            self.log(f"мастер: владелец назначен ({user_id}), язык {self.lang}")
            if self.chat_id is not None:  # группа задана конфигом — выбирать нечего
                return self._t("wizard.owner_set")
            await self.start_progress(user_id)
            return await self.where_menu(Dest(user_id), self._t("wizard.owner_set"))
        given = (args[0] if args else "").strip()
        if given.startswith(INVITE_PREFIX) and not self.allowed(user_id):
            return await self._join(user_id, given[len(INVITE_PREFIX):], name)
        if not self.allowed(user_id):
            return None
        flat = self.routes.preset == "flat"
        if self.chat_id is None:
            if flat:
                return self._t("wizard.private_help_here")
            if self.is_owner(user_id) and not self.routes.chosen:
                return await self.where_menu(Dest(user_id))  # шаг мастера пропущен — ещё раз
            return self._t("wizard.add_to_group")
        return self._t("wizard.private_help_flat" if flat else "wizard.private_help")

    async def _camera_count(self) -> int:
        """Сколько камер уже в реестре — для совета «темы от 4 камер». Мост молчит — 0."""
        try:
            cameras = await asyncio.to_thread(self.bridge.cameras)
        except Exception:  # совет, а не условие: без моста считаем по своему состоянию
            return len(self.state.active_cameras())
        return sum(1 for camera in cameras if camera.status != "retired")

    async def where_menu(self, dest: Dest, head: str = "") -> str | None:
        """Шаг 2 мастера: «Сюда, в этот чат» или «В группу с темами».

        Сюда — плоский режим в личке, установка заканчивается одним нажатием;
        группу можно подключить позже (/mode). От WHERE_TOPICS_FROM камер
        советуем темы: плоская лента при многих камерах — каша.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        count = await self._camera_count()
        many = count >= WHERE_TOPICS_FROM
        lines = ([head, ""] if head else []) + [self._t("wizard.where")]
        if many:
            lines += ["", self._t("wizard.where_many", count=count)]
        text = "\n".join(lines)
        try:
            rows = []
            for choice in ("here", "group"):
                label = self._t(f"wizard.where_{choice}")
                if (choice == "group") == many:
                    label += " ⭐"
                token = self.state.issue_callback(CONSOLE_CAMERA, "where", self.cfg.callback_ttl_sec, choice)
                rows.append([InlineKeyboardButton(label, callback_data=f"cv:where:{token}")])
            await self._send(dest, text, reply_markup=InlineKeyboardMarkup(rows))
        except Exception as exc:
            self.log(f"мастер: выбор места не ушёл ({type(exc).__name__})")
            return text + "\n\n" + self._t("wizard.add_to_group")
        return None

    async def choose_where(self, user_id: int, choice: str, origin: Dest | None,
                           message_id: int | None) -> str:
        """Ответ на шаг мастера. Сюда — пресет «плоско» в личке и карта здесь же;
        в группу — пресет «тема на камеру» и инструкция про группу."""
        if self.chat_id is not None:
            return self._t("wizard.where_done")
        if origin is None or not origin.private:
            return self._t("callback.wrong_topic")
        if choice == "here":
            self.routes.set_preset("flat")
            self.log("мастер: события — в личку (плоский режим)")
            await self.sync_registry()
            await self.refresh_console()
            text = self._t("wizard.here_ready")
        else:
            self.routes.set_preset("camera")
            self.log("мастер: события — в группу с темами")
            text = self._t("wizard.add_to_group") + "\n\n" + self._t("client.pick")
        # Кнопки выбора больше не нужны: шаг сделан. Под «в группу» — кнопки
        # «Android / iPhone / Desktop» с инструкцией, где в этом клиенте темы и права.
        markup = self.client_markup(forum=True) if choice != "here" else None
        await self.refresh_progress()
        if message_id is not None:
            try:
                await self.tg.edit_message_text(chat_id=origin.chat_id, message_id=message_id,
                                                text=text, reply_markup=markup)
                return "✅"
            except Exception as exc:
                self.log(f"мастер: правка выбора не прошла ({type(exc).__name__})")
        try:
            await self._send(origin, text, reply_markup=markup)
        except Exception as exc:
            self.log(f"сообщение в {origin.chat_id} не ушло ({type(exc).__name__})")
        return "✅"

    async def on_bot_membership(self, chat_id: int, chat_type: str, actor_id: int | None,
                                status: str) -> None:
        """Бота добавили в группу или повысили до админа — довести настройку."""
        if chat_type not in ("group", "supergroup"):
            return
        if status in ("left", "kicked"):
            pending = self._pending_group()
            if pending is not None and pending[0] == chat_id:  # группу бросили — не ждём её
                self.state.delete_service(GROUP_PENDING_KEY)
            return
        if not self.allowed(actor_id):
            self.log(f"мастер: бота добавил в {chat_id} посторонний ({actor_id}) — без привязки")
            return
        reported = self._pending_group()
        answer = await self.bind_group(chat_id, actor_id)
        if not answer:
            return
        if reported is not None and reported[0] == chat_id and reported[2] == self._digest(answer):
            # Права поменяли, но не те: тот же список недостач второй раз не пишем.
            self.log(f"мастер: группа {chat_id} — недостачи прежние, молчу")
            return
        await self.say_group(chat_id, answer)

    async def say_group(self, chat_id: int, answer: str) -> None:
        """Ответ мастера в группу; список недостач — с кнопками инструкции по клиентам."""
        try:
            await self._send(Dest(chat_id), answer, reply_markup=self.group_help_markup(answer))
        except Exception as exc:
            self.log(f"сообщение в {chat_id} не ушло ({type(exc).__name__})")

    def group_help_markup(self, answer: str | None):
        """Кнопки «Android / iPhone / Desktop» под списком недостач группы, иначе None."""
        if not answer or not answer.startswith(self._t("wizard.group_needs")):
            return None
        return self.client_markup(forum=self._t("wizard.need_forum") in answer
                                  or self._t("wizard.need_topics_right") in answer)

    async def on_chat_migrated(self, old_chat_id: int, new_chat_id: int) -> None:
        """Группа стала супергруппой (включили темы) — у неё новый id."""
        if self.state.get_service(CHAT_KEY) == str(old_chat_id):
            self.state.set_service(CHAT_KEY, str(new_chat_id))
            self.log(f"мастер: группа {old_chat_id} → {new_chat_id}")
            # Карта старого чата там и осталась: на новом id сверка заведёт свою.
            for screen in self.state.screens():
                if screen.chat_id == old_chat_id:
                    self.state.forget_screen(screen.chat_id, screen.message_id)
        pending = self._pending_group()
        if pending is not None and pending[0] == old_chat_id:
            # Темы обычно и включают переводом в супергруппу: перепроверить сразу.
            self.state.set_service(GROUP_PENDING_KEY, f"{new_chat_id}:{pending[1]}:{pending[2]}")
            await self.recheck_pending_group()

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
        flat = self.routes.preset == "flat"
        # В плоском режиме привязанная группа сразу забирает ленту у личек — поэтому
        # она привязывается, только когда готова. Группа под темы ждёт тем и прав
        # привязанной: доставки туда до темы всё равно нет.
        if current is None and not flat:
            self.state.set_service(CHAT_KEY, str(chat_id))
            self.log(f"мастер: группа {chat_id} привязана")
        problems = await self._group_problems(chat_id, forum=not flat)
        if problems:
            if self._t("wizard.need_forum") in problems:  # группа без тем — тоже путь
                problems.append(self._t("wizard.or_flat"))
            return self._remember_problems(chat_id, "\n".join(
                [self._t("wizard.group_needs")] + problems + [self._t("wizard.group_retry")]))
        if flat:
            if current is None:
                self.state.set_service(CHAT_KEY, str(chat_id))
                self.log(f"мастер: группа {chat_id} привязана")
            answer = await self._bind_flat_group(chat_id)
            self.state.delete_service(GROUP_PENDING_KEY)
            await self.refresh_progress()
            return answer
        fresh = not self.routes.has_console()
        console = await self.ensure_console()
        if console is None:
            return self._remember_problems(chat_id, "\n".join(
                [self._t("wizard.group_needs"), self._t("wizard.need_forum"),
                 self._t("wizard.need_admin"), self._t("wizard.group_retry")]))
        self.state.delete_service(GROUP_PENDING_KEY)
        await self.refresh_console()
        await self.sync_registry()
        await self.refresh_progress()
        if fresh:
            await self.notify_console(self._t("wizard.console_ready"))
            return self._t("wizard.group_ready")
        return self._t("wizard.group_already")

    async def _bind_flat_group(self, chat_id: int) -> str:
        """Плоский режим в группе: тем нет, лента и карта — в самой группе. Карты
        в личках (плоско без группы) говорят, что переехали сюда."""
        fresh = self.routes.home_screen(Dest(chat_id)) is None
        await self.sync_registry()
        await self.refresh_console()
        await self._retire_screens()
        if fresh:
            self.log(f"мастер: группа {chat_id} — плоский режим")
            return self._t("wizard.group_ready_flat")
        return self._t("wizard.group_already")

    async def _group_problems(self, chat_id: int, *, forum: bool = True) -> list[str]:
        """Чего не хватает группе: темы (если нужны) и права бота. Неизвестное — не помеха.

        Плоскому режиму темы и право «Управление темами» не нужны, а админ с
        «Удалением» (пароль камеры) и «Закреплением» (карта) — нужен.
        """
        problems = []
        try:
            chat = await self.tg.get_chat(chat_id=chat_id)
            if forum and getattr(chat, "is_forum", None) is False:
                problems.append(self._t("wizard.need_forum"))
        except Exception as exc:
            self.log(f"мастер: getChat {type(exc).__name__}")
        try:
            me = await self.tg.get_me()
            member = await self.tg.get_chat_member(chat_id=chat_id, user_id=me.id)
            if getattr(member, "status", "") != "administrator":
                problems.append(self._t("wizard.need_admin"))
            else:
                if forum and getattr(member, "can_manage_topics", True) is False:
                    problems.append(self._t("wizard.need_topics_right"))
                if getattr(member, "can_delete_messages", True) is False:
                    problems.append(self._t("wizard.need_delete_right"))
                if getattr(member, "can_pin_messages", True) is False:
                    problems.append(self._t("wizard.need_pin_right"))
        except Exception as exc:
            self.log(f"мастер: getChatMember {type(exc).__name__}")
        return problems

    async def _say(self, dest: Dest, text: str) -> None:
        try:
            await self._send(dest, text)
        except Exception as exc:
            self.log(f"сообщение в {dest.chat_id} не ушло ({type(exc).__name__})")

    # --- прогресс мастера и помощь с группой ------------------------------------
    @staticmethod
    def _digest(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _pending_group(self) -> tuple[int, float, str] | None:
        """Группа, которой сказано «не хватает тем/прав»: (chat_id, с какого времени, что сказано)."""
        raw = self.state.get_service(GROUP_PENDING_KEY) or ""
        try:
            chat, since, digest = raw.split(":", 2)
            return int(chat), float(since), digest
        except ValueError:
            return None

    def _remember_problems(self, chat_id: int, answer: str) -> str:
        """Запомнить недостачи группы: таймер перепроверит её сам. Время — с первого раза."""
        pending = self._pending_group()
        since = pending[1] if pending is not None and pending[0] == chat_id else self.state.now()
        self.state.set_service(GROUP_PENDING_KEY, f"{chat_id}:{since}:{self._digest(answer)}")
        return answer

    async def recheck_pending_group(self) -> bool:
        """Группа ждала тем или прав — проверить снова без /setup.

        Повышение бота приходит апдейтом my_chat_member, а включение тем — нет
        (у группы просто меняется is_forum, или она переезжает в супергруппу):
        поэтому таймер раз в минуту спрашивает getChat/getChatMember сам, сутки
        с первой недостачи. Готова — привязка и сообщение в группу, как после /setup.
        """
        pending = self._pending_group()
        if pending is None:
            return False
        chat_id, since, _digest = pending
        if self.state.now() - since > GROUP_RECHECK_FOR_SEC:
            self.state.delete_service(GROUP_PENDING_KEY)
            self.log(f"мастер: группа {chat_id} — сутки без тем/прав, перепроверка снята")
            return False
        if self.chat_id is not None and self.chat_id != chat_id:
            self.state.delete_service(GROUP_PENDING_KEY)
            return False
        flat = self.routes.preset == "flat"
        try:
            # Группа недоступна (бота убрали, сеть) — не «готова»: _group_problems
            # неизвестное помехой не считает, а здесь без ответа привязывать нельзя.
            await self.tg.get_chat(chat_id=chat_id)
        except Exception as exc:
            self.log(f"мастер: группа {chat_id} недоступна ({type(exc).__name__})")
            return False
        if await self._group_problems(chat_id, forum=not flat):
            return False
        answer = await self.bind_group(chat_id, None)
        if answer == self._t("wizard.group_already"):
            # Пульт успела завести сверка по таймеру, а человек «готово» ещё не видел.
            if not flat:
                await self.notify_console(self._t("wizard.console_ready"))
            answer = self._t("wizard.group_ready_flat" if flat else "wizard.group_ready")
        if answer and self._pending_group() is None:
            self.log(f"мастер: группа {chat_id} готова — перепроверка без /setup")
            await self.say_group(chat_id, answer)
            return True
        return False

    async def bot_mention(self) -> str:
        """@имя бота для инструкций; не узнали — «этого бота» на языке установки."""
        if self._username is None:
            try:
                me = await self.tg.get_me()
                self._username = str(getattr(me, "username", "") or "")
            except Exception as exc:
                self.log(f"getMe: {type(exc).__name__}")
                return self._t("client.this_bot")
        return f"@{self._username}" if self._username else self._t("client.this_bot")

    def client_markup(self, *, forum: bool):
        """Три кнопки «Android / iPhone / Desktop»: инструкция по темам и правам в этом клиенте."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        ttl = self.cfg.panel_callback_ttl_sec  # инструкцию открывают и через день
        kind = "forum" if forum else "flat"
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            self._t(f"client.{client}.button"),
            callback_data=f"cv:client:{self.state.issue_callback(CONSOLE_CAMERA, 'client', ttl, f'{client}|{kind}')}")
            for client in CLIENTS]])

    async def client_help(self, payload: str, origin: Dest | None) -> str:
        """Инструкция под клиент — отдельным сообщением в тот же чат (3–4 строки, без скриншотов)."""
        client, _, kind = payload.partition("|")
        if client not in CLIENTS or origin is None:
            return self._t("callback.unknown")
        forum = kind != "flat"
        bot = await self.bot_mention()
        rights = self._t("client.rights_forum" if forum else "client.rights_flat")
        lines = [self._t(f"client.{client}.title"), self._t(f"client.{client}.group")]
        if forum:
            lines.append(self._t(f"client.{client}.topics"))
        lines += [self._t(f"client.{client}.bot", bot=bot),
                  self._t(f"client.{client}.admin", bot=bot, rights=rights),
                  self._t("client.after")]
        await self._say(Dest(origin.chat_id, origin.thread_id), "\n".join(lines))
        return self._t("client.sent")

    def _progress_steps(self) -> tuple[list[tuple[bool, str]], bool]:
        """Шаги мастера: (сделан, что) ×3 и «всё готово»."""
        place_done, place = False, self._t("progress.where")
        if self.routes.chosen or self.chat_id is not None:
            if self.routes.preset == "flat" and self.chat_id is None:
                place_done, place = True, self._t("progress.here")
            elif self.routes.preset == "flat":
                place_done, place = True, self._t("progress.group")
            elif self.chat_id is not None and self.routes.has_console():
                place_done, place = True, self._t("progress.group")
            else:
                place = self._t("progress.group_wait")
        cameras = self.state.active_cameras()
        camera = (self._t("progress.camera", title=cameras[0].title) if cameras
                  else self._t("progress.camera_wait"))
        steps = [(True, self._t("progress.owner")), (place_done, place), (bool(cameras), camera)]
        return steps, all(done for done, _ in steps)

    def progress_text(self) -> tuple[str, bool]:
        steps, done = self._progress_steps()
        lines = [self._t("progress.header")]
        lines += [self._t("progress.step", n=n, mark="✅" if ok else "⬜", what=what)
                  for n, (ok, what) in enumerate(steps, 1)]
        if done:
            lines += ["", self._t("progress.done")]
        return "\n".join(lines), done

    async def start_progress(self, user_id: int) -> None:
        """Закреплённое сообщение-прогресс в личке владельца — человек всегда видит, где он."""
        text, _done = self.progress_text()
        try:
            posted = await self._send(Dest(user_id), text)
            message_id = int(getattr(posted, "message_id", None) or posted["message_id"])
        except Exception as exc:
            self.log(f"мастер: прогресс не ушёл ({type(exc).__name__})")
            return
        self.state.set_service(PROGRESS_KEY, f"{user_id}:{message_id}")
        self.state.set_service(PROGRESS_TEXT_KEY, text)
        try:
            await self.tg.pin_chat_message(chat_id=user_id, message_id=message_id,
                                           disable_notification=True)
        except Exception as exc:
            self.log(f"мастер: прогресс не закреплён ({type(exc).__name__})")

    async def refresh_progress(self) -> None:
        """Перерисовать прогресс, если что-то сдвинулось; все шаги — «готово» и открепить."""
        raw = self.state.get_service(PROGRESS_KEY)
        if not raw:
            return
        try:
            chat_id, message_id = (int(part) for part in raw.split(":", 1))
        except ValueError:
            self.state.delete_service(PROGRESS_KEY)
            return
        text, done = self.progress_text()
        if text != self.state.get_service(PROGRESS_TEXT_KEY):
            try:
                await self.tg.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            except Exception as exc:
                if "not modified" not in str(exc).lower():
                    self.log(f"мастер: прогресс не обновлён ({type(exc).__name__})")
                    return
            self.state.set_service(PROGRESS_TEXT_KEY, text)
        if done:
            self.state.delete_service(PROGRESS_KEY)
            self.state.delete_service(PROGRESS_TEXT_KEY)
            self.log("мастер: все три шага пройдены")
            try:  # закреп чата — за картой камер, прогресс своё отработал
                await self.tg.unpin_chat_message(chat_id=chat_id, message_id=message_id)
            except Exception as exc:
                self.log(f"мастер: прогресс не откреплён ({type(exc).__name__})")

    # --- /help и /version -----------------------------------------------------------
    def help_text(self, user_id: int | None) -> str:
        """Короткая карта команд и кнопок для текущего режима."""
        flat = not self.routes.forum
        lines = [self._t("help.header"), "",
                 self._t("help.map_flat" if flat else "help.map_topics"),
                 self._t("help.card_flat" if flat else "help.card_topics"),
                 self._t("help.reply"), "",
                 self._t("help.commands")]
        lines += [f"/{name} — {self._t(f'command.{name}')}" for name in MENU_COMMANDS
                  if name not in ("help", "setup") or (name == "setup" and not flat)]
        if not self.is_owner(user_id):
            lines += ["", self._t("help.owner_only")]
        lines += ["", self._t("help.version", version=__version__)]
        return "\n".join(lines)

    async def version_text(self) -> str:
        """Версия установки и — если проверка включена — последняя в GitHub Releases."""
        lines = [self._t("version.current", version=__version__)]
        if not self.updates.enabled:
            lines.append(self._t("version.check_off"))
            return "\n".join(lines)
        if await self.check_updates():
            await self.refresh_console()
        available, latest = self.updates.available(), self.updates.latest()
        if available:
            lines.append(self._t("update.available", version=available))
        elif latest:
            lines.append(self._t("version.latest"))
        else:
            lines.append(self._t("version.unknown"))
        checked = self.updates.checked_at()
        if checked:
            stamp = dt.datetime.fromtimestamp(checked, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(self._t("version.checked", time=self._time(stamp)))
        lines.append(self._t("version.how_off"))
        return "\n".join(lines)

    async def check_updates(self) -> bool:
        """Раз в сутки спросить GitHub; True — узнали о новой версии (карту перерисовать)."""
        if not self.updates.due():
            return False
        found = await asyncio.to_thread(self.updates.check)
        if found:
            self.log(f"доступна версия {self.updates.available()} (установлена {__version__})")
        return found

    def _not_ready(self) -> str:
        """Доставлять некуда: шаг мастера «куда присылать» ещё не пройден — или
        выбрана группа, а её нет."""
        if not self.routes.chosen and self.chat_id is None:
            return self._t("wizard.where_first")
        return self._t("wizard.add_to_group")

    def _console_here(self, chat_id: int | None) -> bool:
        """Ответ пульта ляжет в этот же чат: плоский режим, и /add прислали на карте."""
        return (not self.routes.forum and chat_id is not None
                and self.routes.origin(chat_id, None) == self.routes.console())

    async def on_add(self, user_id: int | None, args: list[str], *,
                     chat_id: int | None = None) -> str:
        """/add — поиск камер; /add <адрес> — камера вне поиска (IP или адрес потока)."""
        if not self.allowed(user_id):
            return self._t("no_access")
        if not self.routes.ready():
            return self._not_ready()
        console = await self.ensure_console()
        if console is None:
            return self._t("wizard.group_needs")
        if args:
            return await self._accept_address(user_id, " ".join(args), console)
        asyncio.create_task(self._discover())
        return self._t("add.searching_here" if self._console_here(chat_id) else "add.searching")

    async def _accept_address(self, user_id: int | None, text: str, console: Dest) -> str:
        """Адрес камеры от человека: IP или полный адрес потока; второй — поток детектора.

        Разбирает и проверяет адрес мост (бот к камерам не ходит и протоколов не
        знает); здесь — только форма: одно-два слова и без пароля внутри.
        """
        parts = (text or "").split()
        if not parts or len(parts) > 2 or any("|" in part for part in parts):
            return self._t("add.bad_address")
        if any("@" in part or PATH_SECRET.search(part) for part in parts):
            # Пароль — только отдельным сообщением, которое удаляется: ни в userinfo,
            # ни в пути (XMEye /user=…&password=…). Путь XMEye мост соберёт сам.
            return self._t("add.no_password_in_url")
        target, detect = parts[0], (parts[1] if len(parts) > 1 else "")
        # Адрес всегда приходит сообщением — просьба уходит ответом на него.
        return await self._ask_credentials(user_id, target, "", console, detect, post=False)

    # --- модель детектора людей ------------------------------------------
    # Бот только показывает и заказывает: перезагрузку детекторов, проверку файла
    # и откат делает движок (engine/model_switch.py), бот ждёт исход и сообщает.
    async def on_model(self, user_id: int | None, *, chat_id: int | None = None) -> str:
        """/model — меню модели детектора на пульте."""
        if not self.allowed(user_id):
            return self._t("no_access")
        if not self.routes.ready():
            return self._not_ready()
        if await self.ensure_console() is None:
            return self._t("wizard.group_needs")
        asyncio.create_task(self.model_menu())
        return self._t("model.menu_here" if self._console_here(chat_id) else "model.menu_sent")

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

        console = await self.ensure_console()
        if console is None:
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
        await self._send(console, self.model_text(catalog), reply_markup=InlineKeyboardMarkup(rows))

    async def model_files(self, family: str) -> None:
        """Файлы для выбранного семейства: подходящие по имени — первыми."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        console = await self.ensure_console()
        if console is None:
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
            await self._send(console, self._t("model.no_files", family=title),
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
        await self._send(
            console, self._t("model.pick_file", family=title, confidence=float(info.get("confidence") or 0)),
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

        console = await self.ensure_console()
        if console is None:
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
        await self._send(console, self.threshold_text(view),
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

    async def _ask_threshold(self, user_id: int | None, dest: Dest | None, camera_id: str) -> str:
        try:
            view, item = await self._threshold_camera(camera_id)
        except BridgeError as exc:
            return self._t("thr.unavailable", error=self._error(exc.code))
        if item is None or user_id is None or dest is None:
            return self._t("camera.retired")
        self.state.expect_input(user_id, CONSOLE_CAMERA, f"thr|{camera_id}", INPUT_TTL_SEC)
        return await self._ask_for_text(dest, self._t(
            "thr.ask", title=item.get("title") or camera_id, threshold=float(item.get("threshold") or 0),
            min=float(view.get("min") or 0.05), max=float(view.get("max") or 0.95)))

    async def _apply_threshold(self, camera_id: str, text: str) -> str:
        raw = (text or "").strip().lower()
        if raw in catalog_words(THRESHOLD_AUTO_WORDS):
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
            home = self.routes.primary(camera_id)
            if camera is not None and camera.status == "online" and home is not None:
                await self._request_media(camera_id, home, "snapshot", None)
                # Своя тема — «в её теме»; плоско и тема локации — туда, куда идут её события.
                key = "add.first_frame" if self.routes.dedicated(camera_id) else "add.first_frame_feed"
                await self.notify_console(self._t(key, title=camera.title))
                return
            await asyncio.sleep(FIRST_FRAME_POLL_SEC)
        key = "add.no_first_frame" if self.routes.dedicated(camera_id) else "add.no_first_frame_card"
        await self.notify_console(self._t(key, camera_id=camera_id))

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
        await self.set_command_menu()
        if self.routes.ready():
            await self.refresh_console()
            await self.refresh_all_panels(force=True)
        return self._t("lang.changed", lang=self.lang)

    async def set_command_menu(self) -> None:
        """Меню «/» в Telegram: на старте и после /lang.

        Явный язык установки (/lang, CCTV_LANG) — у всех клиентов, как и весь остальной
        вывод: иначе при lang=ru клиент на английском видел бы английское меню. Без явного
        языка — по языку Telegram пользователя: ru для русского клиента, иначе en.
        """
        from telegram import BotCommand

        def commands(lang: str) -> list:
            return [BotCommand(name, i18n.t(f"command.{name}", lang)) for name in MENU_COMMANDS]

        explicit = (i18n.match(self.state.get_service(LANG_KEY))
                    or i18n.match(getattr(self.cfg, "lang", "")))
        try:
            await self.tg.set_my_commands(commands(explicit or i18n.DEFAULT_LANG))
            if explicit:
                # Список под код языка сильнее общего — снять, чтобы общий видели все.
                await self.tg.delete_my_commands(language_code="ru")
            else:
                await self.tg.set_my_commands(commands("ru"), language_code="ru")
        except Exception as exc:  # меню — удобство, а не условие работы
            self.log(f"меню команд не установлено: {type(exc).__name__}")

    # --- карточка, режим и люди: /cam, /mode, /invite ------------------------------
    def find_camera(self, query: str) -> str | None:
        """Камера по имени, camera_id или хэштегу: точное совпадение, затем
        единственное по началу слова, затем единственное по подстроке."""
        wanted = (query or "").strip().lstrip("#").lower()
        if not wanted:
            return None
        cameras = self.state.active_cameras()
        names = {c.camera_id: {c.camera_id.lower(), (c.title or "").lower(),
                               hashtag(c.title).lstrip("#")} for c in cameras}
        for matcher in (lambda n: wanted in n,
                        lambda n: any(x.startswith(wanted) for x in n),
                        lambda n: any(wanted in x for x in n)):
            found = [camera_id for camera_id, n in names.items() if matcher(n)]
            if found:
                return found[0] if len(found) == 1 else None  # неоднозначно — пусть уточнят
        return None

    def _card_place(self, camera_id: str, origin: Dest | None) -> bool:
        """Где карточка камеры работает: на карте, в месте камеры, в личке допущенного."""
        return origin is not None and (self.routes.is_console(origin) or self.routes.unplaced(origin)
                                       or self.routes.belongs(camera_id, origin)
                                       or self.routes.personal(origin))

    async def on_cam(self, user_id: int | None, args: list[str], *, chat_id: int | None,
                     thread: int | None = None) -> str | None:
        """/cam <имя> — карточка камеры сообщением здесь же (то же, что на карте, без карты).
        Без имени в теме камеры — её карточка, иначе список имён."""
        if not self.allowed(user_id):
            return self._t("no_access")
        origin = self.routes.origin(chat_id, thread)
        query = " ".join(args).strip()
        camera_id = self.find_camera(query) if query else self.routes.camera_at(origin)
        if camera_id is None:
            names = ", ".join(c.title for c in self.state.active_cameras()) or self._t("menu.empty")
            key = "cam.not_found" if query else "cam.usage"
            return self._t(key, query=query, cameras=names)
        if not self._card_place(camera_id, origin):
            return self._t("cam.wrong_place")
        await self._show(origin, None, f"card:{camera_id}")
        return None

    def _preset_name(self, preset: str) -> str:
        return self._t(f"mode.preset.{preset}")

    async def mode_menu(self, dest: Dest | None) -> None:
        """Меню /mode: текущий пресет, что значит каждый, кнопки выбора."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if dest is None:
            return
        ttl = self.cfg.callback_ttl_sec
        current = self.routes.preset
        lines = [self._t("mode.current", preset=self._preset_name(current)), ""]
        lines += [self._t(f"mode.about.{preset}") for preset in PRESETS]
        if self.chat_id is None:
            lines += ["", self._t("mode.no_group")]
        rows = [[InlineKeyboardButton(
            ("✅ " if preset == current else "") + self._preset_name(preset),
            callback_data=f"cv:mode:{self.state.issue_callback(CONSOLE_CAMERA, 'mode', ttl, preset)}")]
            for preset in PRESETS]
        await self._send(dest, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def on_mode(self, user_id: int | None, args: list[str], *, chat_id: int | None,
                      thread: int | None = None) -> str | None:
        """/mode — меню пресета; /mode camera|location|flat — сразу сменить."""
        if not self.allowed(user_id):
            return self._t("no_access")
        if not self.is_owner(user_id):
            return self._t("access.owner_only")
        if args:
            return await self.apply_mode(args[0].strip().lower())
        await self.mode_menu(self.routes.origin(chat_id, thread))
        return None

    async def apply_mode(self, preset: str) -> str:
        """Сменить пресет маршрутов. Камеры и их id не меняются; темы и карта
        заводятся сверкой реестра, прежняя карта говорит, куда переехала."""
        if preset not in PRESETS:
            return self._t("mode.unknown", presets=", ".join(PRESETS))
        if preset == self.routes.preset:
            return self._t("mode.already", preset=self._preset_name(preset))
        if preset != "flat" and self.chat_id is None:
            return self._t("mode.no_group")
        if self.chat_id is not None:
            # Плоско в группе тоже нужен админ: пароль камеры удаляется, карта закрепляется.
            problems = await self._group_problems(self.chat_id, forum=preset != "flat")
            if problems:
                return "\n".join([self._t("wizard.group_needs")] + problems)
        previous = self.routes.preset
        self.routes.set_preset(preset)
        try:
            await self.sync_registry()
            await self._retire_screens()
        except Exception as exc:  # Telegram отказал (права, темы): вернуть как было
            self.log(f"режим {previous} → {preset}: не применён ({type(exc).__name__}: {exc})")
            self.routes.set_preset(previous)
            return self._t("mode.failed", preset=self._preset_name(preset))
        self.log(f"режим маршрутов: {previous} → {preset}")
        answer = self._t("mode.changed", preset=self._preset_name(preset))
        if preset == "location" and not any(self.routes.location(c.camera_id)
                                            for c in self.state.active_cameras()):
            answer += "\n" + self._t("mode.location_hint")
        if not self.state.active_cameras():
            answer += "\n" + self._t("mode.add_first")
        return answer

    async def _apply_location(self, camera_id: str, text: str) -> str:
        """Тег локации с карточки: секция на карте, тема в пресете «тема на локацию»."""
        value = (text or "").strip()[:LOCATION_MAX]
        clear = value.lower() in catalog_words(LOCATION_CLEAR_WORDS) or value == "-"
        if not value:
            return self._t("location.empty")
        self.state.set_camera_location(camera_id, None if clear else value)
        if self.routes.mode(camera_id) == "location" and self.routes.ready():
            try:
                await self.ensure_topic(camera_id, self.routes.title(camera_id))
            except Exception as exc:
                self.log(f"локация {camera_id}: тема не заведена ({type(exc).__name__})")
        await self.refresh_panel(camera_id)
        await self.refresh_console()
        location = self.routes.location(camera_id)
        if clear:
            return self._t("location.cleared", title=self.routes.title(camera_id))
        return self._t("location.set", title=self.routes.title(camera_id), location=location)

    async def on_invite(self, user_id: int | None, *, chat_id: int | None,
                        chat_type: str = "private") -> str | None:
        """/invite — одноразовая ссылка для ещё одного человека и список приглашённых.

        Только в личке: ссылку из группы мог бы открыть любой её участник.
        Приглашённый — такой же допущенный, как из CCTV_ALLOWED_USER_IDS: без
        группы события приходят ему в личку, со своей картой и своим звуком.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if not self.allowed(user_id):
            return self._t("no_access")
        if not self.is_owner(user_id):
            return self._t("access.owner_only")
        if chat_type != "private" or chat_id is None:
            return self._t("invite.private_only")
        code = self.state.issue_invite(INVITE_TTL_SEC)
        try:
            username = getattr(await self.tg.get_me(), "username", "") or ""
        except Exception as exc:
            self.log(f"приглашение: getMe {type(exc).__name__}")
            username = ""
        start = f"{INVITE_PREFIX}{code}"
        link = f"https://t.me/{username}?start={start}" if username else f"/start {start}"
        lines = [self._t("invite.link", link=link, hours=INVITE_TTL_SEC // 3600)]
        members = self.state.members()
        lines.append(self._t("invite.where_group" if self.routes.preset != "flat" or self.chat_id
                             else "invite.where_private"))
        if members:
            lines += ["", self._t("invite.members")]
            lines += [f"• {name or user}" for user, name in members]
        ttl = self.cfg.callback_ttl_sec
        rows = [[InlineKeyboardButton(
            self._t("invite.kick_button", name=(name or str(user))[:32]),
            callback_data=f"cv:kick:{self.state.issue_callback(CONSOLE_CAMERA, 'kick', ttl, str(user))}")]
            for user, name in members]
        await self._send(Dest(chat_id), "\n".join(lines),
                         reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        return None

    async def _join(self, user_id: int, code: str, name: str) -> str | None:
        if not code or not self.state.take_invite(code):
            self.log(f"приглашение: код не подошёл ({user_id})")
            return self._t("invite.bad_code")
        self.state.add_member(user_id, name)
        self.log(f"приглашение: допущен {user_id}")
        await self.notify_owner(self._t("invite.joined", name=name or str(user_id)))
        if self.routes.preset == "flat" and self.chat_id is None:
            await self.refresh_console()  # своя карта в личке нового человека
            return self._t("invite.welcome_private")
        return self._t("invite.welcome_group")

    async def _kick(self, user_id: int) -> str:
        if not self.state.remove_member(user_id):
            return self._t("invite.not_member")
        self.log(f"приглашение: снят доступ {user_id}")
        for screen in self.state.screens():
            if screen.chat_id == user_id:
                self.state.forget_screen(screen.chat_id, screen.message_id)
        return self._t("invite.removed")

    # --- команды ------------------------------------------------------------
    async def show_keyboard(self, thread: int | None, *, chat_id: int | None = None) -> str | None:
        """Поставить постоянную клавиатуру: в теме камеры — с её именем в подсказке.

        None — это не тема камеры (меню покажет список камер).
        """
        camera_id = self.routes.camera_at(self.routes.origin(chat_id, thread))
        if camera_id is None:
            return None
        if not self.routes.active(camera_id):
            return self._t("camera.retired")
        await self.refresh_panel(camera_id)
        return self._t("keyboard.pinned", title=self.routes.title(camera_id))

    async def menu_text(self) -> str:
        cameras = self.state.active_cameras()
        if not cameras:
            return self._t("menu.empty")
        lines = [self._t("menu.header" if self.routes.forum else "menu.header_flat")]
        lines += [f"• {c.title} ({c.camera_id})" for c in cameras]
        lines.append(self._t("menu.footer"))
        return "\n".join(lines)

    async def sync_registry(self) -> None:
        """Свести маршруты с реестром Bridge: новые камеры заводим, снятые закрываем."""
        if not self.routes.ready():
            return  # доставлять некуда, пока мастер не привязал группу
        async with self._sync_lock:
            await self._sync_registry()

    async def _sync_registry(self) -> None:
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
        await self.refresh_progress()

    async def ensure_panel(self, camera_id: str) -> None:
        """Тема камеры, заведённая до появления панели, получает её при первом же старте.

        Панель живёт только в своей теме камеры; в общей теме и в плоском чате
        её место займёт карточка камеры по запросу.
        """
        if self.state.panel_for(camera_id) is not None:
            await self.refresh_panel(camera_id)
            return
        home = self.routes.camera_topic(camera_id)
        if home is None or not self.routes.dedicated(camera_id):
            return
        posted = await self._send(home, self.panel_text(camera_id),
                                  reply_markup=self.control_markup(camera_id))
        message_id = int(getattr(posted, "message_id", None) or posted["message_id"])
        self.state.bind_panel(camera_id, message_id)
        try:
            await self.tg.pin_chat_message(
                chat_id=home.chat_id, message_id=message_id, disable_notification=True
            )
        except Exception as exc:
            self.log(f"панель {camera_id}: закрепить не удалось ({type(exc).__name__})")
        await self.refresh_panel(camera_id)
