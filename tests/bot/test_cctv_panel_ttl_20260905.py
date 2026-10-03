#!/usr/bin/env python3
"""TTL кнопок: закреплённая панель переживает спокойную неделю.

Прецедент вечера 05.09.2026: панель «дачи» не перерисовывалась с 19:27
(камера спокойна — текст не менялся), часовой TTL токенов истёк в 20:28, и
«📷 Кадр» отвечал «Кнопка устарела» вместо кадра. Кнопки панели теперь живут
неделю; часовой TTL остаётся у клипа под конкретный кадр — он обязан истечь
вместе с окном записи.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


from cctv.bot.bot import CctvBot  # noqa: E402
from cctv.bot.bridge import Bridge  # noqa: E402
from cctv.bot.state import State  # noqa: E402
from test_cctv_contract_20260824 import make_config  # noqa: E402

import httpx  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class PanelTokenTtlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.clock = _Clock()
        self.state = State(":memory:", now=self.clock)
        self.addCleanup(self.state.close)

        def handler(_request: httpx.Request):
            return httpx.Response(200, content=b"jpeg",
                                  headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url="https://bridge.test", transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), tg=None)
        self.state.bind_topic("dacha", 746, "Дача")

    def _snap_token(self) -> str:
        markup = self.bot.control_markup("dacha")
        for row in markup.inline_keyboard:
            for item in row:
                if item.callback_data.startswith("cv:snap:"):
                    return item.callback_data.split(":", 2)[2]
        raise AssertionError("на панели нет кнопки кадра")

    def test_panel_buttons_survive_a_quiet_week(self) -> None:
        """Спокойная камера не перерисовывает панель — кнопки обязаны выжить."""
        token = self._snap_token()
        self.clock.advance(7 * 24 * 3600 - 60)
        self.state.purge_expired_callbacks()
        self.assertEqual(("dacha", "snap", None), self.state.resolve_callback(token))

    def test_panel_buttons_die_past_their_ttl(self) -> None:
        """Неделя прошла с запасом — токен протух, press-time проверки не спасают."""
        token = self._snap_token()
        self.clock.advance(7 * 24 * 3600 + 60)
        self.state.purge_expired_callbacks()
        self.assertIsNone(self.state.resolve_callback(token))

    def test_console_markup_tokens_survive_a_quiet_week(self) -> None:
        """Пульт закреплён и молча сбоить перерисовкой может дольше часа — токены не умирают."""
        import asyncio
        markup = asyncio.run(self.bot.console_markup())
        datas = [item.callback_data for row in markup.inline_keyboard for item in row]
        panel = next(d for d in datas if d.startswith("cv:panel:"))
        token = panel.split(":", 2)[2]
        self.clock.advance(7 * 24 * 3600 - 60)
        self.state.purge_expired_callbacks()
        self.assertEqual(("console", "panel", None), self.state.resolve_callback(token))

    def test_frame_clip_button_keeps_hourly_ttl(self) -> None:
        """Клип под конкретный кадр истекает вместе с окном записи — час, не неделя."""
        markup = self.bot.frame_markup("dacha", "2026-09-05T18:00:00Z")
        data = markup.inline_keyboard[0][0].callback_data
        self.assertTrue(data.startswith("cv:clip:"))
        token = data.split(":", 2)[2]
        self.clock.advance(3600 + 60)
        self.state.purge_expired_callbacks()
        self.assertIsNone(self.state.resolve_callback(token))


if __name__ == "__main__":
    unittest.main()
