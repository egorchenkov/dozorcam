#!/usr/bin/env python3
"""Обновление с 0.3.0: закреп «Карта камер переехала…» снимается при первом старте.

0.3.0 при /mode и подключении группы правил прежнюю карту в «переехала», но оставлял
её закреплённой и забывал её id (аудит 09.10, Б-10). После /mode flat → /mode camera
на 0.3.0 в закрепе General висело «переехала» поверх живой карты «Пульта», в личке —
тоже, если группу подключали после «Сюда». Обновлённый бот этих сообщений не знает:
при старте он смотрит последнее закреплённое (getChat) в группе и личках людей и
снимает своё «переехала» — один раз, чужие закрепы и живые карты не трогает.
Найдено предрелизной регрессией 0.3.1 (стенд LXD и прод 09.10).
"""
from __future__ import annotations

import unittest

from telegram.error import BadRequest, TimedOut

from cctv.bot.bot import STALE_MOVED_KEY
from test_cctv_flow_20260824 import OWNER
from test_stab_030_20261009 import FRIEND, GROUP, Harness, StabTelegram

BOT_ID = 4242
PEOPLE = 77


class Message:
    def __init__(self, message_id: int, text: str, author: int = BOT_ID) -> None:
        self.message_id = message_id
        self.text = text
        self.from_user = type("User", (), {"id": author})()


class PinnedTelegram(StabTelegram):
    """getChat отдаёт последнее закреплённое; откреп снимает его и открывает предыдущее."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pins: dict[int, list[Message]] = {}  # chat → закрепы, последний — в конце
        self.failures: dict[int, Exception] = {}

    async def get_chat(self, **kwargs):
        chat = kwargs["chat_id"]
        self._record("get_chat", kwargs)
        if chat in self.failures:
            raise self.failures[chat]
        pins = self.pins.get(chat) or []
        return type("Chat", (), {"is_forum": self.forum, "pinned_message": pins[-1] if pins else None})()

    async def unpin_chat_message(self, **kwargs):
        await super().unpin_chat_message(**kwargs)
        pins = self.pins.get(kwargs["chat_id"]) or []
        self.pins[kwargs["chat_id"]] = [m for m in pins if m.message_id != kwargs["message_id"]]


class StaleMovedPinTest(Harness, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tg = PinnedTelegram(forum=True)
        self.bot.tg = self.bot.routes.tg = self.tg

    def unpinned(self) -> list[tuple[int, int]]:
        return [(c["chat_id"], c["message_id"]) for c in self.tg.of("unpin_chat_message")]

    async def test_moved_map_left_by_030_is_unpinned_in_group_and_private_chat(self) -> None:
        moved, moved_group = self.t("map.moved"), self.t("map.moved_group")
        live_map = Message(1211, self.t("map.header", cameras=3, online=3, events=0))
        # Прод 09.10: Пульт — живая карта, поверх него в General «переехала» после flat → camera.
        self.tg.pins[GROUP] = [live_map, Message(7919, moved)]
        # Личка: «Сюда», затем группа — карта лички «переехала» (0.3.0) и ещё одна от /mode.
        self.tg.pins[OWNER] = [Message(5, moved), Message(9, moved_group)]
        await self.bot.unpin_stale_moved()
        self.assertEqual([(GROUP, 7919), (OWNER, 9), (OWNER, 5)], self.unpinned())
        self.assertEqual([live_map], self.tg.pins[GROUP])  # живая карта осталась в закрепе
        self.assertEqual("done", self.state.get_service(STALE_MOVED_KEY))
        # Разово: второй старт getChat не зовёт.
        calls = len(self.tg.of("get_chat"))
        await self.bot.unpin_stale_moved()
        self.assertEqual(calls, len(self.tg.of("get_chat")))

    async def test_other_pins_are_not_touched(self) -> None:
        # Человек закрепил своё сообщение с тем же текстом; бот закрепил не «переехала».
        self.tg.pins[GROUP] = [Message(3, self.t("map.moved"), author=PEOPLE)]
        self.tg.pins[OWNER] = [Message(4, "📍 Камер: 3")]
        await self.bot.unpin_stale_moved()
        self.assertEqual([], self.unpinned())
        self.assertEqual("done", self.state.get_service(STALE_MOVED_KEY))

    async def test_english_installation_too(self) -> None:
        from cctv import i18n
        self.tg.pins[GROUP] = [Message(11, i18n.t("map.moved", "en"))]
        await self.bot.unpin_stale_moved()
        self.assertEqual([(GROUP, 11)], self.unpinned())

    async def test_network_failure_retries_on_next_start(self) -> None:
        self.tg.pins[GROUP] = [Message(7919, self.t("map.moved"))]
        self.tg.failures[GROUP] = TimedOut()
        self.tg.failures[FRIEND] = BadRequest("Chat not found")  # личку не открывали — не повод повторять
        await self.bot.unpin_stale_moved()
        self.assertEqual([], self.unpinned())
        self.assertIsNone(self.state.get_service(STALE_MOVED_KEY))
        del self.tg.failures[GROUP]
        await self.bot.unpin_stale_moved()
        self.assertEqual([(GROUP, 7919)], self.unpinned())
        self.assertEqual("done", self.state.get_service(STALE_MOVED_KEY))


if __name__ == "__main__":
    unittest.main()
