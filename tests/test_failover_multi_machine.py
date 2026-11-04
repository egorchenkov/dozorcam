"""Отказоустойчивость на разных машинах: дефекты, найденные живым прогоном шагов 1–3.

Тесты на loopback их не ловили: там адрес моста из compose, bind и адрес в ссылках
на медиа совпадают. На двух машинах с mTLS:

- бот не стартовал: супервизор проверял конфиг с http-адресом моста из compose, а не
  с https-адресом моста держателя движка, который подставляет слой;
- ссылки на медиа вели на bind движка (0.0.0.0), и бот отвергал кадр с моста узла;
- движок площадки вне слоя слал события на loopback из compose мимо списка
  [engine] events_url в config.toml;
- healthcheck движка с мостом на адресе узла стучался в 127.0.0.1.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import types
import unittest
from unittest import mock

from cctv import container, settings
from cctv.failover import config as fo_config
from cctv.failover.agent import Agent

ROOT = pathlib.Path(__file__).resolve().parents[1]

FAILOVER = """
[failover]
[[failover.nodes]]
name = "dacha"
address = "192.0.2.10"
bridge_url = "https://192.0.2.10:8780"
events_url = "https://192.0.2.10:8781/v1/events"

[[failover.nodes]]
name = "city"
address = "192.0.2.20"
bridge_url = "https://192.0.2.20:8780"
events_url = "https://192.0.2.20:8781/v1/events"
"""


def fake_agent(role: str, me: str, holders: dict[str, str]):
    """Agent.wiring без сети: только назначение ролей и описание узлов."""
    nodes = tuple(fo_config.Node(name=name, priority=index, engine_agent=f"https://{addr}:8782",
                                 bot_agent=f"https://{addr}:8783", bridge_url=f"https://{addr}:8780",
                                 events_url=f"https://{addr}:8781/v1/events")
                  for index, (name, addr) in enumerate((("dacha", "192.0.2.10"), ("city", "192.0.2.20")), 1))
    cfg = types.SimpleNamespace(me=me, node=lambda name: next((n for n in nodes if n.name == name), None))
    return types.SimpleNamespace(role=role, cfg=cfg, holder=lambda r=None: holders[r or role])


class WiringTest(unittest.TestCase):
    def test_engine_links_media_to_its_own_bridge(self) -> None:
        agent = fake_agent("engine", "city", {"engine": "city", "bot": "dacha"})
        wiring = Agent.wiring(agent)
        self.assertEqual("https://192.0.2.20:8780", wiring["CCTV_PUBLIC_URL"])
        self.assertEqual("https://192.0.2.10:8781/v1/events", wiring["CCTV_EVENTS_URL"])

    def test_bot_gets_bridge_of_engine_holder(self) -> None:
        agent = fake_agent("bot", "dacha", {"engine": "city", "bot": "dacha"})
        self.assertEqual({"CCTV_BRIDGE_URL": "https://192.0.2.20:8780"}, Agent.wiring(agent))

    def test_media_url_of_engine_matches_bridge_url_of_bot(self) -> None:
        """Ссылка движка (CCTV_PUBLIC_URL) — ровно тот мост, который бот принимает."""
        from cctv.bot.bridge import Bridge

        engine = Agent.wiring(fake_agent("engine", "city", {"engine": "city", "bot": "dacha"}))
        bot = Agent.wiring(fake_agent("bot", "dacha", {"engine": "city", "bot": "dacha"}))
        client = Bridge.__new__(Bridge)
        client.cfg = types.SimpleNamespace(bridge_base_url=bot["CCTV_BRIDGE_URL"])
        self.assertTrue(client.is_bridge_url(engine["CCTV_PUBLIC_URL"] + "/v1/media/token"))
        client.cfg = types.SimpleNamespace(bridge_base_url="https://0.0.0.0:8780")
        self.assertFalse(client.is_bridge_url(engine["CCTV_PUBLIC_URL"] + "/v1/media/token"))


class PrepareWithWiringTest(unittest.TestCase):
    """Бот с mTLS стартует, хотя в окружении узла — http-адрес моста из compose."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        tls = root / "tls"
        tls.mkdir()
        for name in ("node.crt", "node.key", "ca.crt"):
            (tls / name).write_text("x")
        (root / "config.toml").write_text(
            '[common]\ninternal_tls = true\n\n[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n'
            f'bridge_client_cert = "{tls}/node.crt"\nbridge_client_key = "{tls}/node.key"\n'
            f'bridge_ca = "{tls}/ca.crt"\n' + FAILOVER)
        secrets = root / "secrets.toml"
        secrets.write_text('[bot]\nbot_token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"\n\n'
                           '[failover]\nsecret = "unit-test-failover-secret-0123456789"\n')
        secrets.chmod(0o600)
        self.env = {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(root), "CCTV_NODE": "dacha",
                    "CCTV_STATE_DIR": str(root / "state"),
                    # как подставляет compose: loopback по http
                    "CCTV_BRIDGE_URL": "http://127.0.0.1:8780"}

    def make(self, wiring: dict[str, str] | None):
        with mock.patch.dict(os.environ, self.env, clear=True):
            sup = container.BotSupervisor()
        sup.layer = None if wiring is None else types.SimpleNamespace(wiring=lambda: dict(wiring))
        return sup

    def test_bot_validates_with_bridge_of_engine_holder(self) -> None:
        with mock.patch.object(container, "token_rejected", return_value=""):
            sup = self.make({"CCTV_BRIDGE_URL": "https://192.0.2.20:8780"})
            self.assertIsInstance(sup.prepare(), dict)
            # Без слоя — прежняя проверка: http при mTLS отвергается.
            problem = self.make(None).prepare()
        self.assertIsInstance(problem, tuple)
        self.assertIn("https", problem[1])


class ComposeAddressesTest(unittest.TestCase):
    """Compose не перекрывает адреса из config.toml: умолчание — только loopback этого узла."""

    def test_compose_leaves_addresses_to_config(self) -> None:
        compose = (ROOT / "compose.yml").read_text()
        self.assertIn("CCTV_EVENTS_URL: ${CCTV_EVENTS_URL:-}", compose)
        self.assertIn("CCTV_BRIDGE_URL: ${CCTV_BRIDGE_URL:-}", compose)
        self.assertFalse(re.search(r"CCTV_(EVENTS|BRIDGE)_URL: http", compose))

    def site_engine_env(self, root: pathlib.Path, compose_env: dict[str, str]) -> dict[str, str]:
        """Окружение движка, как его соберёт супервизор: пустые CCTV_* из compose сняты."""
        env = {key: value for key, value in compose_env.items() if value}
        env["CCTV_CONFIG_DIR"] = str(root)
        return settings.apply("engine", env)

    def test_site_engine_takes_receivers_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config.toml").write_text(
                '[common]\ninternal_tls = true\n\n[engine]\n'
                'events_url = ["https://192.0.2.10:8781/v1/events", "https://192.0.2.20:8781/v1/events"]\n')
            env = self.site_engine_env(root, {"CCTV_EVENTS_URL": "", "CCTV_EVENTS_PORT": "8781", "CCTV_PORT": "8790"})
            self.assertEqual("https://192.0.2.10:8781/v1/events https://192.0.2.20:8781/v1/events",
                             env["CCTV_EVENTS_URL"])

    def test_loopback_defaults_follow_compose_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            engine = self.site_engine_env(root, {"CCTV_EVENTS_URL": "", "CCTV_EVENTS_PORT": "8791"})
            self.assertEqual("http://127.0.0.1:8791/v1/events", engine["CCTV_EVENTS_URL"])
            bot = settings.apply("bot", {"CCTV_CONFIG_DIR": str(root), "CCTV_BRIDGE_PORT": "8790"})
            self.assertEqual("http://127.0.0.1:8790", bot["CCTV_BRIDGE_URL"])
            # Без портов — прежние умолчания.
            self.assertEqual("http://127.0.0.1:8781/v1/events",
                             settings.apply("engine", {"CCTV_CONFIG_DIR": str(root)})["CCTV_EVENTS_URL"])
            self.assertEqual("http://127.0.0.1:8780",
                             settings.apply("bot", {"CCTV_CONFIG_DIR": str(root)})["CCTV_BRIDGE_URL"])


class HealthBindTest(unittest.TestCase):
    """Healthcheck mTLS-моста стучится туда, где мост слушает (адрес узла), а не в loopback."""

    def check(self, bind: str) -> tuple[str, int]:
        hosts = []

        def connect(address, timeout=None):
            hosts.append(address)
            return mock.Mock()

        status = {"role": "engine", "state": "running", "updated_at": 1e12, "children": {"bridge": True}}
        env = {"CCTV_INTERNAL_TLS": "1", "CCTV_PORT": "8790", "CCTV_BIND": bind, "CCTV_CONFIG_DIR": "/nonexistent"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(container.STATUS_PATH.__class__, "read_text", return_value=__import__("json").dumps(status)), \
                mock.patch.object(container.socket, "create_connection", side_effect=connect), \
                mock.patch("builtins.print"):
            code = container.health()
        return hosts[0], code

    def test_health_follows_bind(self) -> None:
        self.assertEqual((("192.0.2.10", 8790), 0), self.check("192.0.2.10"))
        self.assertEqual((("127.0.0.1", 8790), 0), self.check("0.0.0.0"))
        self.assertEqual((("127.0.0.1", 8790), 0), self.check(""))


if __name__ == "__main__":
    unittest.main()
