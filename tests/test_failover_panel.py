"""Панель /nodes: проверка пригодности раз в минуту видна в боте.

Без check_sec в config.toml проба идёт раз в 60 с, и панель так и пишет; у каждого
узла и роли — итог пробы и её возраст. Непригодному узлу кнопки переключения нет.
"""
from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace

from cctv import failover
from cctv.failover import panel

NODES = ('[failover]\n[[failover.nodes]]\nname = "a"\nengine_agent = "http://127.0.0.1:18782"\n'
         'bot_agent = "http://127.0.0.1:18783"\n[[failover.nodes]]\nname = "b"\n'
         'engine_agent = "http://127.0.0.1:18792"\nbot_agent = "http://127.0.0.1:18793"\n')
NOW = 1_000_000.0


def load_cfg():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "config.toml").write_text(NODES)
        (root / "secrets.toml").write_text('[failover]\nsecret = "unit-test-failover-secret-0123456789"\n')
        (root / "secrets.toml").chmod(0o600)
        return failover.load({"CCTV_CONFIG_DIR": tmp, "CCTV_NODE": "a"})


def fake_agent(cfg):
    holders = {"engine": "a", "bot": "a"}
    return SimpleNamespace(cfg=cfg, holder=holders.get, switching=None, last_switch=None,
                           lock=threading.Lock(), assign={})


def status(running: bool, health: dict) -> dict:
    return {"running": running, "now": NOW, "health": health}


STATUSES = {
    ("a", "engine"): status(True, {"fit": True, "ok": 3, "total": 3, "checked_at": NOW - 12}),
    ("a", "bot"): status(True, {"fit": True, "checked_at": NOW - 12}),
    ("b", "engine"): status(False, {"fit": False, "ok": 0, "total": 3, "checked_at": NOW - 41}),
    ("b", "bot"): status(False, {"fit": True, "checked_at": NOW - 41}),
}


class PanelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = load_cfg()
        self.assertEqual(60, self.cfg.check_sec)

    def test_minute_check_is_visible(self) -> None:
        text = panel.view(fake_agent(self.cfg), "en", statuses=STATUSES)["text"]
        self.assertIn("Fitness is checked every 60 s.", text)
        self.assertIn("● a (priority 1) — this node\n   engine: active · cameras reachable 3/3 · checked 12 s ago\n"
                      "   bot: active · Telegram reachable · checked 12 s ago", text)
        self.assertIn("● b (priority 2)\n   engine: armed · cameras reachable 0/3 · checked 41 s ago\n"
                      "   bot: armed · Telegram reachable · checked 41 s ago", text)

    def test_minute_check_is_visible_in_russian(self) -> None:
        text = panel.view(fake_agent(self.cfg), "ru", statuses=STATUSES)["text"]
        self.assertIn("Пригодность проверяется каждые 60 с.", text)
        self.assertIn("проверен 41 с назад", text)

    def test_unfit_or_silent_node_gets_no_button(self) -> None:
        buttons = panel.view(fake_agent(self.cfg), "en", statuses=STATUSES)["buttons"]
        data = [button["data"] for row in buttons for button in row]
        self.assertEqual(["go:bot:b", panel.REFRESH], data)
        silent = {**STATUSES, ("b", "bot"): None}
        view = panel.view(fake_agent(self.cfg), "en", statuses=silent)
        self.assertEqual([panel.REFRESH], [button["data"] for row in view["buttons"] for button in row])
        self.assertIn("   bot: does not answer", view["text"])

    def test_not_checked_yet(self) -> None:
        fresh = {key: status(value["running"], {}) for key, value in STATUSES.items()}
        text = panel.view(fake_agent(self.cfg), "en", statuses=fresh)["text"]
        self.assertEqual(4, text.count("not checked yet"))


if __name__ == "__main__":
    unittest.main()
