#!/usr/bin/env python3
"""Маршруты доставки: куда боту писать о камере — `route(camera_id) → [Dest, …]`.

До 0.3.0 в боте было жёстко зашито «одна камера = одна тема форума», и тема
одновременно различала камеру, управляла ею и фильтровала ленту. Теперь тема —
только «куда доставлять», и это решает маршрут камеры по пресету установки:

- ``camera``   — тема на камеру (как было; дефолт для существующих установок);
- ``location`` — тема на локацию (тег камеры ``location``, иначе площадка реестра);
- ``flat``     — без тем: группа целиком или, без группы, личка каждого допущенного.

Личка — не следствие пресета: при любом пресете с группой копия события идёт в
личку тем допущенным, кто включил её у камеры кнопкой «📩 Мне в личку» (подписка,
`motion_subs`). Без группы личка и есть лента, подписка там — звук.

Пресет лишь заполняет маршруты: у камеры может быть свой режим (`cameras.mode`),
camera_id при смене режима не меняется. Весь `message_thread_id` бота живёт
здесь — ядро (bot.py) работает с ``Dest`` и не знает, тема это или личка.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .state import CameraRecord, State

PRESETS = ("camera", "location", "flat")
DEFAULT_PRESET = "camera"
PRESET_KEY = "route_preset"
CONSOLE_KEY = "console_thread"
# Тема локации для камер без тега и площадки — ключ в location_topics.
NO_LOCATION = ""
# Хэштег Telegram — буквы, цифры и «_»; длинный хвост читать никто не станет.
HASHTAG_MAX = 32


@dataclass(frozen=True)
class Dest:
    """Одно место доставки: чат и тема (None — без темы: обычная группа или личка)."""
    chat_id: int
    thread_id: int | None = None

    @property
    def private(self) -> bool:
        """Личка человека: у пользователей Telegram id положительный, у групп — нет."""
        return self.chat_id > 0

    @property
    def in_topic(self) -> bool:
        """Тема форума, а не чат целиком (группа без тем, General, личка)."""
        return self.thread_id is not None

    def kw(self) -> dict:
        """Аргументы адреса для Bot API (send_message, send_photo, …)."""
        return {"chat_id": self.chat_id, "message_thread_id": self.thread_id}


def hashtag(text: str | None) -> str:
    """«Крыльцо-2» → «#крыльцо_2». Пустое — пустая строка, не «#».

    Telegram считает хэштегом слово из букв, цифр и «_», но не одни цифры —
    у такой камеры к хэштегу добавляется «cam».
    """
    word = "".join(ch if ch.isalnum() else "_" for ch in (text or "").strip().lower())
    word = "_".join(part for part in word.split("_") if part)[:HASHTAG_MAX].strip("_")
    if not word:
        return ""
    if word.isdigit():
        word = f"cam{word}"
    return f"#{word}"


class Router:
    """Маршруты и темы. Тексты, иконки и адресаты — снаружи: слой не знает i18n.

    `group` — группа установки (None — не привязана); `recipients` — кто получает
    личку в плоском режиме без группы; `asker` — кто нажал (пульт без группы —
    его личка); `t` — перевод ключа каталога; `icon` — иконка темы по
    подсказкам (площадка, имя).
    """

    def __init__(self, state: State, tg, *, group: Callable[[], int | None],
                 recipients: Callable[[], list[int]], t: Callable[..., str],
                 icon: Callable[..., str], asker: Callable[[], int | None] = lambda: None,
                 log=lambda _m: None) -> None:
        self.state = state
        self.tg = tg
        self._group = group
        self._recipients = recipients
        self._asker = asker
        self._t = t
        self._icon = icon
        self.log = log

    # --- пресет и режим --------------------------------------------------------
    @property
    def preset(self) -> str:
        """Пресет установки. Нет записи — «тема на камеру»: так жили все до 0.3.0."""
        saved = self.state.get_service(PRESET_KEY)
        return saved if saved in PRESETS else DEFAULT_PRESET

    @property
    def chosen(self) -> bool:
        """Пресет выбран явно (мастер, /mode), а не взят по умолчанию."""
        return self.state.get_service(PRESET_KEY) in PRESETS

    def set_preset(self, preset: str) -> None:
        if preset not in PRESETS:
            raise ValueError(f"unknown route preset: {preset!r}")
        self.state.set_service(PRESET_KEY, preset)

    def mode(self, camera_id: str) -> str:
        record = self.state.camera(camera_id)
        if record is not None and record.mode in PRESETS:
            return record.mode
        return self.preset

    @property
    def forum(self) -> bool:
        """Нужны ли темы: пресет с темами (камера/локация), а не плоский."""
        return self.preset != "flat"

    def ready(self) -> bool:
        """Есть куда доставлять: группа — для режимов с темами, группа или личка — для плоского."""
        if self._group() is not None:
            return True
        return self.preset == "flat" and bool(self._recipients())

    # --- камеры ----------------------------------------------------------------
    def camera(self, camera_id: str) -> CameraRecord | None:
        return self.state.camera(camera_id)

    def active(self, camera_id: str) -> bool:
        record = self.state.camera(camera_id)
        if record is not None:
            return record.status == "active"
        topic = self.state.topic_for(camera_id)
        return topic is not None and topic.status == "active"

    def title(self, camera_id: str) -> str:
        record = self.state.camera(camera_id)
        if record is not None and record.title:
            return record.title
        topic = self.state.topic_for(camera_id)
        return topic.title if topic is not None and topic.title else camera_id

    def location(self, camera_id: str) -> str:
        """Локация камеры: явный тег, иначе площадка реестра, если это не имя камеры.

        Мост ставит площадку новой камеры равной её имени (так её заводит мастер),
        и такая «площадка» локацией не является — иначе каждая камера жила бы в
        своей «локации», и пресет «тема на локацию» ничем не отличался бы от темы
        на камеру.
        """
        record = self.state.camera(camera_id)
        if record is None:
            return ""
        if record.location is not None:
            return record.location.strip()
        site = (record.site or "").strip()
        if not site:
            return ""
        if site.lower() in {camera_id.lower(), (record.title or "").strip().lower()}:
            # Площадка, совпавшая с именем, — локация, если она есть ещё у одной
            # камеры: «Дача» с соседями «Дача-2/3» на той же площадке не должна
            # отрываться от них в свою тему.
            shared = any(other.camera_id != camera_id
                         and (other.location if other.location is not None else other.site or ""
                              ).strip().lower() == site.lower()
                         for other in self.state.active_cameras())
            return site if shared else ""
        return site

    def hashtags(self, camera_id: str) -> str:
        """«#калитка #дача»: фильтр ленты нажатием — и в теме, и в личке."""
        tags = [hashtag(self.title(camera_id)), hashtag(self.location(camera_id))]
        unique = []
        for tag in tags:
            if tag and tag not in unique:
                unique.append(tag)
        return " ".join(unique)

    def dedicated(self, camera_id: str) -> bool:
        """Камера живёт в своей теме: имя в подписи не нужно, его даёт тема."""
        return self.mode(camera_id) == "camera"

    # --- маршрут -----------------------------------------------------------------
    def route(self, camera_id: str) -> list[Dest]:
        """Куда доставлять события камеры: лента (`feed`) и личные копии
        подписчиков (`personal_copies`) — событие уходит во все места сразу.
        Пусто — маршрут ещё не готов (нет темы/группы).

        Ничего не создаёт: темы заводит `ensure`.
        """
        if not self.active(camera_id):
            return []
        feed = self.feed(camera_id)
        return feed + [dest for dest in self.personal_copies(camera_id) if dest not in feed]

    def personal_copies(self, camera_id: str) -> list[Dest]:
        """Личка подписчиков камеры при привязанной группе. Только допущенные:
        снятый с доступа (/invite) копий больше не получает. Без группы личка
        каждого — сама лента, копии не нужны."""
        if self._group() is None or not self.active(camera_id):
            return []
        people = set(self._recipients())
        return [Dest(user_id) for user_id in self.state.motion_subscribers(camera_id)
                if user_id in people]

    def personal_copy(self, camera_id: str, dest: Dest) -> bool:
        """Место — личная копия события, а не лента."""
        return dest.private and dest in self.personal_copies(camera_id)

    def feed(self, camera_id: str) -> list[Dest]:
        """Лента камеры по пресету: тема камеры, тема локации, группа целиком или,
        без группы, личка каждого допущенного."""
        if not self.active(camera_id):
            return []
        group = self._group()
        mode = self.mode(camera_id)
        if mode == "flat":
            if group is not None:
                return [Dest(group)]
            return [Dest(user_id) for user_id in self._recipients()]
        if group is None:
            return []
        if mode == "location":
            thread = self.state.location_topic(self.location(camera_id))
            return [Dest(group, thread)] if thread is not None else []
        topic = self.state.topic_for(camera_id)
        if topic is None or topic.status != "active":
            return []
        return [Dest(group, topic.thread_id)]

    def primary(self, camera_id: str) -> Dest | None:
        """Первое место маршрута — для ответа, когда кнопку нажали не в нём (пульт)."""
        dests = self.feed(camera_id) or self.route(camera_id)
        return dests[0] if dests else None

    def belongs(self, camera_id: str, dest: Dest) -> bool:
        """Место камеры: её маршрут, её собственная тема или тема её локации. Темы
        остаются местом камеры и после смены пресета (/mode): закреплённая там
        панель и кадры в ленте темы не должны превращаться в мёртвые кнопки."""
        return (dest in self.route(camera_id) or dest == self.camera_topic(camera_id)
                or dest == self.location_place(camera_id))

    def location_place(self, camera_id: str) -> Dest | None:
        """Тема локации камеры, если она заведена (пресет «тема на локацию» сейчас или раньше)."""
        group = self._group()
        if group is None:
            return None
        thread = self.state.location_topic(self.location(camera_id))
        return Dest(group, thread) if thread is not None else None

    def personal(self, dest: Dest | None) -> bool:
        """Личка допущенного человека: там работают карточка (/cam) и кнопки любой
        камеры, а кадр и клип приходят ему же — при любом пресете."""
        return dest is not None and dest.private and dest.chat_id in self._recipients()

    def reply_to(self, camera_id: str, origin: Dest | None) -> Dest | None:
        """Куда вернуть ответ на действие с камерой: туда, где нажали, если это её
        место или личка допущенного (кто попросил клип — тому и уходит), иначе в
        её основной маршрут."""
        if origin is not None and (self.belongs(camera_id, origin) or self.personal(origin)):
            return origin
        return self.primary(camera_id)

    def origin(self, chat_id: int | None, thread: int | None) -> Dest | None:
        """Откуда пришло нажатие или сообщение. Чат None — группа установки
        (старые вызовы и тесты знали только тему).

        Тема имеет смысл только в группе установки: в личке её нет. В плоском
        режиме тема остаётся, если это наша тема (камеры, локации, пульт): группа
        с темами, переведённая в «плоско», хранит панели и кадры в темах, и
        кнопки там обязаны отвечать в ту же тему (жалоба 09.10.2026). Прочий
        message_thread_id — reply-цепочка обычной супергруппы, а не тема.
        """
        chat = chat_id if chat_id is not None else self._group()
        if chat is None:
            return None
        if chat != self._group() or (not self.forum and not self.known_thread(thread)):
            thread = None
        return Dest(chat, thread)

    def known_thread(self, thread: int | None) -> bool:
        """Тема, заведённая ботом: камеры, локации или пульта."""
        if thread is None:
            return False
        if self.state.camera_for_thread(thread) is not None:
            return True
        if any(saved == thread for _location, saved, _title in self.state.location_topics()):
            return True
        return self.state.get_service(CONSOLE_KEY) == str(thread)

    def unplaced(self, dest: Dest) -> bool:
        """Группа с темами, но нажатие вне темы (General): место не известно —
        как и до 0.3.0, это не чужая тема, ответ уйдёт в маршрут камеры."""
        return self.forum and dest.thread_id is None and dest.chat_id == self._group()

    def camera_at(self, dest: Dest | None) -> str | None:
        """Камера, которой отдано это место целиком (тема камеры). Иначе None."""
        if dest is None or dest.thread_id is None or dest.chat_id != self._group():
            return None
        return self.state.camera_for_thread(dest.thread_id)

    def silent_for(self, camera_id: str, dest: Dest) -> bool:
        """Звук события: в личке — подписан ли этот человек; в группе — тихо всегда.

        Подписчик слышит событие в личке (копия со звуком), и звонок в группе
        был бы вторым сигналом о том же. До 0.3.1 группа звенела, «если
        подписан хоть кто-то», — строка и кнопка подписки поэтому и спорили
        друг с другом (аудит 09.10.2026, Б-5).
        """
        if dest.private:
            return dest.chat_id not in self.state.motion_subscribers(camera_id)
        return True

    # --- темы ----------------------------------------------------------------------
    async def ensure(self, camera_id: str, title: str, site: str | None = None,
                     ) -> tuple[list[Dest], Dest | None]:
        """Запомнить камеру и довести её маршрут: завести тему камеры или локации.

        Вернуть (маршрут, новая тема камеры или None) — в новую тему камеры ядро
        кладёт паспорт с панелью. Повтор идемпотентен, переименование — правкой темы.
        """
        self.state.note_camera(camera_id, title or camera_id, site)
        group = self._group()
        mode = self.mode(camera_id)
        fresh = None
        if mode == "camera" and group is not None:
            fresh = await self._camera_topic(group, camera_id, title, site or "")
        elif mode == "location" and group is not None:
            await self._location_topic(group, self.location(camera_id))
        return self.route(camera_id), fresh

    async def _camera_topic(self, group: int, camera_id: str, title: str, site: str) -> Dest | None:
        topic = self.state.topic_for(camera_id)
        if topic is not None:
            if title and title != topic.title:
                self.state.rename_topic(camera_id, title)
                await self.tg.edit_forum_topic(
                    chat_id=group, message_thread_id=topic.thread_id, name=title,
                    icon_custom_emoji_id=self._icon(title, site, camera_id),
                )
            if topic.status != "active":
                # Камеру вернули в реестр: архивная тема снова её, а не новая.
                self.state.bind_topic(camera_id, topic.thread_id, title or topic.title)
                await self._reopen(group, topic.thread_id)
            return None
        created = await self.tg.create_forum_topic(
            chat_id=group, name=title or camera_id,
            icon_custom_emoji_id=self._icon(title, site, camera_id),
        )
        thread = int(getattr(created, "message_thread_id", None) or created["message_thread_id"])
        self.state.bind_topic(camera_id, thread, title or camera_id)
        self.log(f"камера {camera_id}: создана тема {thread}")
        return Dest(group, thread)

    async def _reopen(self, group: int, thread: int) -> None:
        reopen = getattr(self.tg, "reopen_forum_topic", None)
        if reopen is None:
            return
        try:
            await reopen(chat_id=group, message_thread_id=thread)
        except Exception as exc:  # тема могла быть и не закрыта
            self.log(f"тема {thread}: открыть заново не удалось ({type(exc).__name__})")

    async def _location_topic(self, group: int, location: str) -> Dest:
        thread = self.state.location_topic(location)
        if thread is not None:
            return Dest(group, thread)
        name = location or self._t("route.no_location")
        created = await self.tg.create_forum_topic(
            chat_id=group, name=name[:128], icon_custom_emoji_id=self._icon(location),
        )
        thread = int(getattr(created, "message_thread_id", None) or created["message_thread_id"])
        self.state.bind_location_topic(location, thread, name)
        self.log(f"локация {location or '-'}: создана тема {thread}")
        return Dest(group, thread)

    async def rename(self, camera_id: str, title: str) -> None:
        """Новое имя камеры. Тема камеры переименовывается, тема локации — нет."""
        self.state.rename_topic(camera_id, title)
        topic = self.state.topic_for(camera_id)
        group = self._group()
        if topic is None or topic.status != "active" or group is None:
            return
        try:
            await self.tg.edit_forum_topic(chat_id=group, message_thread_id=topic.thread_id,
                                           name=title, icon_custom_emoji_id=self._icon(title, camera_id))
        except Exception as exc:
            self.log(f"переименование темы {camera_id}: {type(exc).__name__}")

    def camera_topic(self, camera_id: str) -> Dest | None:
        """Своя тема камеры (действующая) — там живёт закреплённая панель."""
        topic = self.state.topic_for(camera_id)
        group = self._group()
        if topic is None or topic.status != "active" or group is None:
            return None
        return Dest(group, topic.thread_id)

    def retire(self, camera_id: str) -> tuple[list[Dest], Dest | None]:
        """Снять камеру: (кому сказать об этом, своя тема для закрытия или None).

        Своя тема закрывается и остаётся архивом; общая тема локации и плоский
        чат не закрываются — там живут другие камеры. Уже снятая — ([], None).
        """
        if not self.active(camera_id):
            return [], None
        dests = self.route(camera_id)
        own = self.camera_topic(camera_id)
        self.state.retire_camera(camera_id)
        if own is not None:
            self.state.retire_topic(camera_id)
            if own not in dests:
                dests.append(own)
        return dests, own

    async def close(self, dest: Dest) -> None:
        """Закрыть тему камеры (после прощального сообщения в ней)."""
        await self.tg.close_forum_topic(chat_id=dest.chat_id, message_thread_id=dest.thread_id)
        self.log(f"тема {dest.thread_id} закрыта")

    # --- пульт ---------------------------------------------------------------------
    def console(self) -> Dest | None:
        """Пульт без создания: тема пульта в форуме, сам чат — в плоском режиме."""
        group = self._group()
        if self.preset == "flat":
            if group is not None:
                return Dest(group)
            recipients = self._recipients()
            # Пульт без группы — личка того, кто нажал: итог поиска камер, меню
            # модели и порогов приходят ему, а не владельцу (аудит 09.10, Б-9).
            asker = self._asker()
            if asker in recipients:
                return Dest(asker)
            return Dest(recipients[0]) if recipients else None
        saved = self.state.get_service(CONSOLE_KEY)
        if group is None or saved is None:
            return None
        return Dest(group, int(saved))

    def has_console(self) -> bool:
        return self.state.get_service(CONSOLE_KEY) is not None or self.preset == "flat"

    async def ensure_console(self, title: str, icon: str) -> Dest | None:
        """Служебная тема одна на весь пульт; её id переживает перезапуск."""
        existing = self.console()
        if existing is not None or self.preset == "flat":
            return existing
        group = self._group()
        if group is None:
            return None
        try:
            created = await self.tg.create_forum_topic(chat_id=group, name=title,
                                                       icon_custom_emoji_id=icon)
        except Exception as exc:
            self.log(f"пульт: тему создать не удалось ({type(exc).__name__})")
            return None
        thread = int(getattr(created, "message_thread_id", None) or created["message_thread_id"])
        self.state.set_service(CONSOLE_KEY, str(thread))
        self.log(f"пульт: создана тема {thread}")
        return Dest(group, thread)

    def map_places(self) -> list[Dest]:
        """Где живёт карта камер: тема пульта в форуме, сама группа в плоском режиме,
        а без группы — личка каждого допущенного (у каждого своя карта)."""
        if self.preset == "flat":
            group = self._group()
            if group is not None:
                return [Dest(group)]
            return [Dest(user_id) for user_id in self._recipients()]
        console = self.console()
        return [console] if console is not None else []

    def is_console(self, dest: Dest | None) -> bool:
        """Нажатие на пульте: тема пульта; в плоском режиме — сам чат группы или личка
        допущенного (без группы у каждого своя лента)."""
        if dest is None:
            return False
        if self.preset == "flat":
            if dest.thread_id is not None:
                return False
            group = self._group()
            if group is not None:
                return dest.chat_id == group
            return dest.private and dest.chat_id in self._recipients()
        console = self.console()
        return console is not None and dest == console

    def forum_topics(self) -> list[tuple[Dest, str, str | None]]:
        """Темы установки для разовой правки иконок: (место, имя, camera_id|None)."""
        group = self._group()
        if group is None:
            return []
        topics = [(Dest(group, t.thread_id), t.title, t.camera_id) for t in self.state.active_topics()]
        topics += [(Dest(group, thread), title, None)
                   for _location, thread, title in self.state.location_topics()]
        return topics

    async def edit_topic(self, dest: Dest, name: str, icon: str) -> None:
        await self.tg.edit_forum_topic(chat_id=dest.chat_id, message_thread_id=dest.thread_id,
                                       name=name, icon_custom_emoji_id=icon)

    # --- заявки медиа и кадры ----------------------------------------------------------
    def _frame_chat(self, dest: Dest) -> int:
        """Кадры группы установки хранятся под чатом 0 — как до 0.3.0."""
        return 0 if dest.chat_id == self._group() else dest.chat_id

    def remember_request(self, request_id: str, camera_id: str, kind: str, dest: Dest) -> bool:
        return self.state.remember_request(request_id, camera_id, kind, dest.thread_id,
                                           chat_id=dest.chat_id)

    def take_request(self, request_id: str) -> tuple[str, str, Dest] | None:
        """(camera_id, kind, куда вернуть) или None. Заявка без чата — из группы."""
        pending = self.state.take_request(request_id)
        if pending is None:
            return None
        chat = pending.chat_id if pending.chat_id is not None else self._group()
        if chat is None:
            return None
        return pending.camera_id, pending.kind, Dest(chat, pending.thread_id)

    def remember_frame(self, message_id: int, camera_id: str, dest: Dest, center_at: str,
                       ttl_sec: int) -> None:
        self.state.remember_frame(message_id, camera_id, dest.thread_id, center_at, ttl_sec,
                                  chat_id=self._frame_chat(dest))

    def resolve_frame(self, message_id: int, dest: Dest | None):
        """(FrameReply, место кадра) или None; кадр ищется в чате, где ответили."""
        if dest is None:
            return None
        frame = self.state.resolve_frame_reply(message_id, self._frame_chat(dest))
        if frame is None:
            return None
        return frame, Dest(dest.chat_id, frame.thread_id)

    # --- склейка событий -----------------------------------------------------------------
    def recent_post(self, camera_id: str, dest: Dest, window_sec: float):
        if window_sec <= 0:
            return None
        return self.state.event_post(camera_id, dest.chat_id, dest.thread_id, window_sec)

    def remember_post(self, camera_id: str, dest: Dest, message_id: int, first_at: str,
                      person: bool) -> None:
        self.state.remember_event_post(camera_id, dest.chat_id, dest.thread_id, message_id,
                                       first_at, person)

    def merge_post(self, camera_id: str, dest: Dest, person: bool) -> None:
        self.state.merge_event_post(camera_id, dest.chat_id, dest.thread_id, person)

    def forget_post(self, camera_id: str, dest: Dest) -> None:
        self.state.forget_event_post(camera_id, dest.chat_id, dest.thread_id)

    # --- экраны: карта и карточка ----------------------------------------------------------
    def screen_place(self, screen) -> Dest:
        return Dest(screen.chat_id, screen.thread_id)

    def remember_screen(self, dest: Dest, message_id: int, view: str, *, home: bool = False) -> None:
        self.state.remember_screen(dest.chat_id, dest.thread_id, message_id, view, home=home)

    def home_screen(self, dest: Dest):
        """Закреплённая карта места (или None)."""
        return self.state.home_screen(dest.chat_id, dest.thread_id)
