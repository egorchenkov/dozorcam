"""Отказоустойчивость: агент роли занимает свой адрес, когда тот появится, и обе роли
узла работают от одного каталога конфига.

* Адрес агента ещё недоступен при старте контейнера (адрес VPN поднимается позже
  Docker после перезагрузки): роль ждёт, healthcheck называет причину, а bind
  повторяется сам — адрес появился, роль стартует без рестарта контейнера.
* Разные каталоги конфига у ролей одного узла: агент пишет в журнал предупреждение
  (агенты обеих ролей раздают ревизии, и роль приняла бы ревизию соседней роли).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from cctv import container, failover
from cctv.failover.agent import Agent

from tests.test_failover_armed import SECRET, TOKEN, FakeProc, free_port


def write_config(root: pathlib.Path, ports: dict[str, tuple[int, int]], extra: str = "") -> None:
    nodes = "".join(
        f'[[failover.nodes]]\nname = "{name}"\npriority = {index}\n'
        f'engine_agent = "http://127.0.0.1:{engine}"\nbot_agent = "http://127.0.0.1:{bot}"\n'
        for index, (name, (engine, bot)) in enumerate(ports.items(), start=1))
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(
        "[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n" + extra + "\n[failover]\ncheck_sec = 5\n" + nodes)
    (root / "secrets.toml").write_text(f'[failover]\nsecret = "{SECRET}"\n')
    (root / "secrets.toml").chmod(0o600)
    (root / "cameras.json").write_text('{"cameras": []}\n')


class BindRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.config, self.run_dir = root / "config", root / "run"
        self.run_dir.mkdir()
        self.ports = {name: (free_port(), free_port()) for name in ("a", "b")}
        write_config(self.config, self.ports)
        self.env = {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(self.config),
                    "CCTV_STATE_DIR": str(root / "state"), "CCTV_BOT_TOKEN": TOKEN, "CCTV_NODE": "a"}
        self.started: list[FakeProc] = []

        def popen(argv, env=None, **_kw):
            proc = FakeProc(argv, env)
            self.started.append(proc)
            return proc

        for patcher in (mock.patch.object(container, "STATUS_PATH", self.run_dir / "supervisor.json"),
                        mock.patch.object(container, "RUN_DIR", self.run_dir),
                        mock.patch.object(container.subprocess, "Popen", side_effect=popen),
                        mock.patch.object(container.signal, "signal"),
                        mock.patch.object(container, "TICK_SEC", 0.1),
                        mock.patch.object(container, "BIND_RETRY_SEC", 0.5)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def status(self) -> dict:
        try:
            return json.loads((self.run_dir / "supervisor.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout: float = 15.0, what: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"не дождался: {what}")

    def test_role_waits_for_agent_address_and_starts_when_it_comes_up(self) -> None:
        port = self.ports["a"][0]
        # Адрес агента движка недоступен: bind даёт OSError, как у ещё не поднятого адреса VPN.
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        self.addCleanup(blocker.close)

        with mock.patch.dict(os.environ, self.env, clear=True):
            sup = container.EngineSupervisor()
        sup.proxy_config = self.run_dir / "cameras.proxy.json"
        sup.empty_config = self.run_dir / "cameras.empty.json"
        out = io.StringIO()

        def run():
            with contextlib.redirect_stdout(out):
                sup.run()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def stop():
            sup.stopping = True
            thread.join(20)
            self.assertFalse(thread.is_alive(), "супервизор не остановился")

        self.addCleanup(stop)

        self.wait_for(lambda: self.status().get("state") == "waiting", what="статус waiting")
        time.sleep(1.5)  # несколько повторов bind
        self.assertIsNone(sup.layer)
        self.assertEqual([], self.started, "роль стартовала без агента слоя")
        # healthcheck — unhealthy с причиной: какой адрес и что повтор идёт сам.
        with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()) as health:
            self.assertEqual(1, container.health())
        self.assertIn("waiting:", health.getvalue())
        self.assertIn(f"http://127.0.0.1:{port}", health.getvalue())
        self.assertIn("retrying every 0.5 s", health.getvalue())
        # Журнал — одна строка о причине, а не по строке на каждый повтор.
        self.assertEqual(1, out.getvalue().count("cannot listen"))

        blocker.close()  # адрес появился
        self.wait_for(lambda: sup.layer is not None, what="агент занял адрес")
        self.wait_for(lambda: any("rtsp-proxy" in proc.argv for proc in self.started),
                      what="запуск роли без рестарта контейнера")
        self.wait_for(lambda: self.status().get("state") == "running", what="статус running")


class SharedConfigDirTest(unittest.TestCase):
    """Агент сравнивает свой каталог конфига с каталогом соседней роли узла."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        # Агенты не слушают: рассылка ревизий соседям уходит в закрытые порты и молча пропадает.
        self.ports = {name: (free_port(), free_port()) for name in ("a", "b")}

    def agent(self, role: str, config: pathlib.Path) -> tuple[Agent, list[str]]:
        logs: list[str] = []
        cfg = failover.load({"CCTV_CONFIG_DIR": str(config), "CCTV_NODE": "a"})
        agent = Agent(cfg, role, self.root / role / "state", log=logs.append, local_dir=config)
        agent.share_tick()
        return agent, logs

    def compare(self, engine: Agent, bot: Agent) -> None:
        engine._check_sibling({("a", "bot"): bot.status(), ("a", "engine"): engine.status()})

    def test_different_dirs_of_one_node_are_reported_once(self) -> None:
        write_config(self.root / "engine-config", self.ports)
        write_config(self.root / "bot-config", self.ports, extra="# каталог бота\n")
        engine, logs = self.agent("engine", self.root / "engine-config")
        bot, _ = self.agent("bot", self.root / "bot-config")
        self.compare(engine, bot)
        self.assertEqual([], [line for line in logs if "один каталог" in line],
                         "одна сверка — ещё не расхождение: правку роли замечают не одновременно")
        for _ in range(3):
            self.compare(engine, bot)
        warned = [line for line in logs if "один каталог" in line]
        self.assertEqual(1, len(warned), logs)
        self.assertIn("docs/failover.md", warned[0])

    def test_one_dir_for_both_roles_is_quiet(self) -> None:
        write_config(self.root / "config", self.ports)
        engine, logs = self.agent("engine", self.root / "config")
        bot, _ = self.agent("bot", self.root / "config")
        for _ in range(3):
            self.compare(engine, bot)
        self.assertTrue(engine.local_sha)
        self.assertEqual(engine.local_sha, bot.status()["local"])
        self.assertEqual([], [line for line in logs if "один каталог" in line])


if __name__ == "__main__":
    unittest.main()
