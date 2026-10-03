"""Уведомления владельцу — личкой от самого бота, без внешних служб доставки.

Два пути: сторож бота (мост перестал отвечать / снова отвечает) и команда
cctv-notify для OnFailure-юнитов (упавшую службу бот сам не увидит).
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cctv import notify
from cctv import i18n
from cctv.bot import bot as bot_module
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import BridgeError
from cctv.bot.state import State

ROOT = pathlib.Path(__file__).resolve().parents[1]


class NotifyCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        env = {"CCTV_BOT_TOKEN": "0:TEST", "CCTV_ALLOWED_USER_IDS": "7 8",
               "CCTV_NOTIFY_STATE": str(self.dir / "dedup.json")}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def run_notify(self, *argv, side_effect=None):
        with mock.patch.object(notify, "telegram_send", return_value="42",
                               side_effect=side_effect) as sender, \
                mock.patch.object(notify.time, "sleep"):
            rc = notify.main(["--text", "служба cctv-bridge не работает", *argv])
        return rc, sender

    def test_owner_gets_private_message_from_the_bot(self):
        rc, sender = self.run_notify()
        self.assertEqual(0, rc)
        self.assertEqual(["7", "8"], [call.args[1] for call in sender.call_args_list])
        token, _, text, silent = sender.call_args.args
        self.assertEqual("0:TEST", token)
        self.assertTrue(text.startswith(notify.PREFIX))
        self.assertFalse(silent)

    def test_owner_ids_narrow_the_audience(self):
        with mock.patch.dict(os.environ, {"CCTV_OWNER_IDS": "8"}):
            rc, sender = self.run_notify()
        self.assertEqual(0, rc)
        self.assertEqual(["8"], [call.args[1] for call in sender.call_args_list])

    def test_detail_stays_out_of_the_message(self):
        rc, sender = self.run_notify("--detail", "rc=3 unit=cctv-bridge")
        self.assertEqual(0, rc)
        self.assertNotIn("rc=3", sender.call_args.args[2])

    def test_repeat_in_window_collapses(self):
        first, sender = self.run_notify()
        second, again = self.run_notify()
        self.assertEqual((0, 0), (first, second))
        self.assertEqual(2, sender.call_count)
        again.assert_not_called()

    def test_not_configured_is_code_2(self):
        with mock.patch.dict(os.environ, {"CCTV_BOT_TOKEN": ""}):
            rc, sender = self.run_notify()
        self.assertEqual(2, rc)
        sender.assert_not_called()

    def test_nobody_reached_is_code_1(self):
        rc, sender = self.run_notify(side_effect=RuntimeError("Forbidden: bot can't initiate"))
        self.assertEqual(1, rc)
        self.assertEqual(2 * notify.ATTEMPTS, sender.call_count)

    def test_units_call_the_bot_voice_on_failure(self):
        """Отказ службы обязан быть слышен: OnFailure есть у всей цепочки движка."""
        for name in ("cctv-bridge", "cctv-pipeline", "cctv-provision", "cctv-rtsp-proxy"):
            unit = (ROOT / "deploy" / "systemd" / f"{name}.service").read_text(encoding="utf-8")
            self.assertIn("OnFailure=cctv-alert@%N.service", unit, name)
        alert = (ROOT / "deploy" / "systemd" / "cctv-alert@.service").read_text(encoding="utf-8")
        self.assertIn("/bin/cctv-notify", alert)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"message_id": len(self.sent)}


class FakeBridge:
    def __init__(self) -> None:
        self.down = False

    def registry(self):
        if self.down:
            raise BridgeError("unavailable", "connection refused")
        return [], None


class Cfg:
    chat_id = -100
    allowed_user_ids = frozenset({7, 8})
    owner_ids = frozenset({7})


class BridgeWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.tg, self.bridge = FakeTelegram(), FakeBridge()
        self.bot = CctvBot(Cfg(), self.state, self.bridge, self.tg)

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            asyncio.run(self.bot.watch_health())

    def test_bridge_outage_and_recovery_reach_owner_privately(self):
        self.tick()
        self.assertEqual([], self.tg.sent, "исправный мост при старте — не новость")
        self.bridge.down = True
        self.tick()
        self.assertEqual([], self.tg.sent, "один пропуск — ещё не авария")
        self.tick(3)
        self.assertEqual(1, len(self.tg.sent), "авария объявляется один раз")
        self.assertEqual(7, self.tg.sent[0]["chat_id"])
        self.assertNotIn("message_thread_id", self.tg.sent[0])
        self.assertIn(i18n.t(bot_module.BRIDGE_DOWN_KEY, self.bot.lang), self.tg.sent[0]["text"])
        self.bridge.down = False
        self.tick(2)
        self.assertEqual(2, len(self.tg.sent))
        self.assertIn(i18n.t(bot_module.BRIDGE_UP_KEY, self.bot.lang), self.tg.sent[1]["text"])

    def test_single_blip_is_silent(self):
        self.tick()
        self.bridge.down = True
        self.tick()
        self.bridge.down = False
        self.tick()
        self.assertEqual([], self.tg.sent)


if __name__ == "__main__":
    unittest.main()
