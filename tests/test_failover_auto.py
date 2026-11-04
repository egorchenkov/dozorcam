"""Отказоустойчивость, шаг 5: автопереключение по приоритету (auto.py).

* По умолчанию выключено: без ``auto = true`` молчащего держателя никто не сменяет,
  бот на конфликт опроса не выходит — всё как в шагах 1–4.
* Решение по кворуму — чистая функция: роль забирает следующий по приоритету узел,
  только когда кворум слышит друг друга и никто из него не слышит держателя.
* Три живых агента на loopback (настоящие HTTP и сердцебиение, супервизор — цикл,
  который запускает «процессы» роли, пока агент говорит «включись»): отказ держателя —
  роль у следующего по приоритету; без кворума — ни у кого; отрезанный держатель
  замирает сам раньше, чем роль заберут; копия state держателя уже у запасного.
* Telegram как арбитр: три настоящих супервизора бота против мока Bot API, который,
  как Telegram, отвечает 409 перебитому опросу. Узел-держатель «зависает» (SIGSTOP)
  — бот забирает следующий по приоритету; зависший просыпается — его опрос
  отступает, на каждое действие человека по-прежнему ровно один ответ.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from cctv import failover
from cctv.bot import config as bot_config
from cctv.bot import main as bot_main
from cctv.failover import auto, wire
from cctv.failover.agent import Agent

from tests.test_failover_armed import SECRET, TOKEN, free_port
from tests.test_failover_switch import OWNER, ROOT, FakeTelegram, button

NAMES = ("a", "b", "c")


def write_config(root: pathlib.Path, ports: dict, extra: str = "", head: str = "") -> None:
    nodes = "".join(
        f'[[failover.nodes]]\nname = "{name}"\npriority = {index}\n'
        f'engine_agent = "http://127.0.0.1:{ports[name][0]}"\nbot_agent = "http://127.0.0.1:{ports[name][1]}"\n'
        for index, name in enumerate(ports, start=1))
    (root / "config.toml").write_text(head + "[failover]\ncheck_sec = 5\n" + extra + nodes)
    (root / "secrets.toml").write_text(f'bot_token = "{TOKEN}"\n\n[failover]\nsecret = "{SECRET}"\n')
    (root / "secrets.toml").chmod(0o600)


def load(root: pathlib.Path, me: str):
    return failover.load({"CCTV_CONFIG_DIR": str(root), "CCTV_NODE": me})


class DefaultOffTest(unittest.TestCase):
    """auto не задан — автопереключения нет, бот на конфликт не выходит."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def ports(self, names=NAMES) -> dict:
        return {name: (free_port(), free_port()) for name in names}

    def test_auto_is_off_by_default(self) -> None:
        write_config(self.root, self.ports())
        cfg = load(self.root, "b")
        self.assertFalse(cfg.auto)
        self.assertEqual((5, 30), (cfg.heartbeat_sec, cfg.dead_after_sec))
        with tempfile.TemporaryDirectory() as state:
            agent = Agent(cfg, "bot", pathlib.Path(state), log=lambda _line: None)
            self.assertNotIn("CCTV_POLL_CONFLICT", agent.panel_env())
            self.assertEqual({}, agent.status()["heard"])
            self.assertFalse(agent.conflict())   # не держатель — но и ничего не меняет
        resolved = bot_config.load({"CCTV_BOT_TOKEN": TOKEN, "CCTV_CHAT_ID": "-100777",
                                    "CCTV_ALLOWED_USER_IDS": str(OWNER), "CCTV_STATE_DIR": state})
        self.assertFalse(resolved.poll_conflict_exit)
        self.assertIsNone(bot_main.poll_error_callback(resolved))

    def test_silent_holder_is_not_replaced_without_auto(self) -> None:
        """Три агента без auto: держатель замолчал — роль остаётся за ним, как в шагах 1–4."""
        ports = self.ports()
        write_config(self.root, ports)
        agents = {name: Agent(load(self.root, name), "engine", self.root / name, log=lambda _line: None)
                  for name in NAMES}
        for agent in agents.values():
            self.addCleanup(agent.stop)
            agent.start()
            agent.report(agent.active())
        self.assertTrue(agents["a"].active())
        agents["a"].stop()
        agents["a"].report(False)
        for agent in (agents["b"], agents["c"]):
            agent.check()        # сверка с соседями, как раз в check_sec
        self.assertEqual(["a", "a"], [agents[name].holder() for name in ("b", "c")])
        self.assertFalse(any(agents[name].active() for name in ("b", "c")))
        self.assertFalse(any(thread.name.startswith(("failover-beat", "failover-snapshot")) for agent in agents.values()
                             for thread in agent._threads))

    def test_auto_needs_three_nodes_and_sane_timing(self) -> None:
        write_config(self.root, self.ports(("a", "b")), "auto = true\n")
        with self.assertRaises(failover.FailoverError) as caught:
            load(self.root, "a")
        self.assertEqual("auto_nodes", caught.exception.code)
        for timing in ("heartbeat_sec = 5\ndead_after_sec = 10\n", "dead_after_sec = 601\n",
                       "heartbeat_sec = 0\n", "auto = 1\n"):
            write_config(self.root, self.ports(), "auto = true\n" + timing if "auto" not in timing else timing)
            with self.assertRaises(failover.FailoverError, msg=timing):
                load(self.root, "a")
        write_config(self.root, self.ports(), "auto = true\nheartbeat_sec = 2\ndead_after_sec = 8\n")
        cfg = load(self.root, "a")
        self.assertEqual((True, 2, 8, 2, 4.0), (cfg.auto, cfg.heartbeat_sec, cfg.dead_after_sec, cfg.quorum,
                                                 cfg.fence_sec))

    def test_conflict_exit_code_is_one_number(self) -> None:
        self.assertEqual(auto.CONFLICT_EXIT, bot_main.POLL_CONFLICT_EXIT)


class DecisionTest(unittest.TestCase):
    """Кто забирает роль — по возрастам «слышал» и кворуму, без сети."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        write_config(root, {name: (free_port(), free_port()) for name in NAMES}, "auto = true\n")
        self.cfg = {name: load(root, name) for name in NAMES}   # dead_after 30, fence 15

    def decide(self, me: str, own: dict, heard: dict, fit=None, elsewhere=False) -> bool:
        views = {name: {"heard": ages} for name, ages in heard.items()}
        return auto.takeover(self.cfg[me], "a", own, views, fit or {}, elsewhere)

    def test_next_by_priority_takes_over_when_quorum_agrees(self) -> None:
        self.assertTrue(self.decide("b", {"a": 40, "b": 0, "c": 3}, {"c": {"a": 41}}))
        self.assertFalse(self.decide("c", {"a": 40, "b": 3, "c": 0}, {"b": {"a": 41}}), "c ждёт b")

    def test_no_quorum_no_takeover(self) -> None:
        self.assertFalse(self.decide("b", {"a": 400, "b": 0, "c": 400}, {}))
        self.assertFalse(self.decide("b", {"a": 400, "b": 0, "c": 16}, {"c": {"a": 400}}), "c слышали давно")

    def test_someone_in_quorum_still_hears_the_holder(self) -> None:
        self.assertFalse(self.decide("b", {"a": 40, "b": 0, "c": 3}, {"c": {"a": 2}}))
        self.assertFalse(self.decide("b", {"a": 20, "b": 0, "c": 3}, {"c": {"a": 41}}), "сам слышал 20 с назад")
        self.assertFalse(self.decide("b", {"a": 3, "b": 0, "c": 3}, {"c": {"a": 41}}))

    def test_unfit_candidate_is_skipped_and_running_role_blocks(self) -> None:
        own_c = {"a": 40, "b": 3, "c": 0}
        self.assertTrue(self.decide("c", own_c, {"b": {"a": 41}}, fit={"b": False}))
        self.assertFalse(self.decide("b", {"a": 40, "b": 0, "c": 3}, {"c": {"a": 41}}, fit={"b": False}))
        self.assertFalse(self.decide("b", {"a": 40, "b": 0, "c": 3}, {"c": {"a": 41}}, elsewhere=True))

    def test_fresh_agent_does_not_count_the_holder_dead(self) -> None:
        """Только что запущенный агент не слышал держателя — но и молчит тот не дольше его запуска."""
        own = auto.ages({}, started=100.0, now=103.0, nodes=NAMES, me="b")
        self.assertEqual({"a": 3.0, "b": 0.0, "c": 3.0}, own)
        self.assertFalse(self.decide("b", own, {"c": {"a": 3.0}}))


class Node:
    """Агент роли и «супервизор»: процессы роли живут, пока агент говорит «включись»."""

    def __init__(self, test, name: str) -> None:
        self.test, self.name = test, name
        self.agent = None
        self.stop_event = threading.Event()
        self.thread = None
        self.start()

    def start(self) -> None:
        self.agent = Agent(load(self.test.root, self.name), getattr(self.test, "ROLE", "engine"),
                           self.test.root / "state" / self.name,
                           log=lambda line: self.test.log.append(f"{self.name}: {line}"))
        self.agent.start()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._supervise, daemon=True)
        self.thread.start()

    def _supervise(self) -> None:
        while not self.stop_event.is_set():
            self.agent.report(self.agent.active())
            self.stop_event.wait(0.05)

    def kill(self) -> None:
        """Узел умер: ни агента, ни процессов роли."""
        self.stop_event.set()
        self.thread.join(2)
        self.agent.report(False)
        self.agent.stop()

    @property
    def running(self) -> bool:
        return self.agent.running and not self.stop_event.is_set()


class ThreeAgentsTest(unittest.TestCase):
    """Три агента движка с auto: heartbeat 1 с, держатель мёртв через 4 с, сам замирает через 2 с."""

    HEARTBEAT, DEAD = 1, 4

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.log: list[str] = []
        self.ports = {name: (free_port(), free_port()) for name in NAMES}
        write_config(self.root, self.ports,
                     f"auto = true\nheartbeat_sec = {self.HEARTBEAT}\ndead_after_sec = {self.DEAD}\n")
        self.nodes = {name: Node(self, name) for name in NAMES}
        for node in self.nodes.values():
            self.addCleanup(lambda node=node: node.kill() if not node.stop_event.is_set() else None)
        # Выборка «кто держит роль» каждые 20 мс на всё время теста: двух сразу быть не должно.
        self.samples: list[tuple[float, tuple[str, ...]]] = []
        self.sampling = threading.Event()
        sampler = threading.Thread(target=self._sample, daemon=True)
        sampler.start()
        self.addCleanup(self.sampling.set)
        self.wait_for(lambda: self.running() == ("a",), 10, "a держит роль")

    def _sample(self) -> None:
        while not self.sampling.is_set():
            self.samples.append((time.monotonic(), self.running()))
            time.sleep(0.02)

    def running(self) -> tuple[str, ...]:
        return tuple(name for name, node in self.nodes.items() if node.running)

    def wait_for(self, predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail(f"не дождался: {what}\n" + "\n".join(self.log[-40:]))

    def assert_never_two(self) -> None:
        doubles = [names for _, names in self.samples if len(names) > 1]
        self.assertEqual([], doubles[:3], "роль работала на двух узлах сразу")

    def test_holder_dies_next_by_priority_takes_over(self) -> None:
        a, b, c = (self.nodes[name] for name in NAMES)
        died = time.monotonic()
        a.kill()
        self.wait_for(lambda: self.running() == ("b",), self.DEAD + 6, "b забрал роль")
        took = time.monotonic() - died
        self.assertGreaterEqual(took, self.DEAD - 0.5, "роль ушла раньше, чем держатель замолчал")
        self.wait_for(lambda: c.agent.holder() == "b", 3, "c узнал нового держателя")
        assign = b.agent.assign["engine"]
        self.assertEqual(("b", 1, "a", True, False), (assign["holder"], assign["epoch"], assign["prev"],
                                                      assign["auto"], assign["carry"]))
        self.assertFalse(c.running)
        self.assertEqual({"role": "engine", "to": "b", "from": "a", "ok": True, "auto": True},
                         {key: b.agent.last_switch[key] for key in ("role", "to", "from", "ok", "auto")})
        # Узел a вернулся: узнаёт новую эпоху и встаёт наготове, роль сама обратно не едет.
        a.start()
        self.wait_for(lambda: a.agent.holder() == "b", 5, "a узнал эпоху 1")
        time.sleep(self.DEAD + 1)
        self.assertEqual(("b",), self.running())
        self.assert_never_two()

    def test_no_quorum_no_takeover_then_quorum_returns(self) -> None:
        a, b, c = (self.nodes[name] for name in NAMES)
        a.kill()
        c.kill()
        time.sleep(self.DEAD * 2.5)
        self.assertEqual((), self.running(), "одиночный узел без кворума забрал роль")
        self.assertEqual("a", b.agent.holder())
        self.assertFalse(b.agent.active())
        # Вернулся c — кворум из двух (b, c) есть, держатель a молчит: роль у b, следующего по приоритету.
        c.start()
        self.wait_for(lambda: self.running() == ("b",), self.DEAD + 8, "b забрал роль при кворуме")
        self.assertEqual("b", c.agent.holder())
        self.assert_never_two()

    def test_isolated_holder_freezes_before_the_role_moves(self) -> None:
        a, b, c = (self.nodes[name] for name in NAMES)
        # Сеть между a и остальными пропала в обе стороны; процессы a живы.
        a.agent.server.shutdown()
        a.agent.server.server_close()
        real_call = a.agent.client.call

        def cut(base, *args, **kwargs):
            raise wire.WireError("URLError")

        cut_at = time.monotonic()
        with mock.patch.object(a.agent.client, "call", side_effect=cut):
            self.wait_for(lambda: not a.running, self.DEAD, "отрезанный a замер сам")
            frozen = time.monotonic() - cut_at
            self.wait_for(lambda: self.running() == ("b",), self.DEAD + 6, "b забрал роль")
            moved = time.monotonic() - cut_at
            self.assertLess(frozen, moved)
            self.assertEqual("a", a.agent.holder(), "a не знает о новой эпохе — и всё равно не работает")
            self.assertTrue(a.agent.status()["fenced"])
        # Сеть вернулась: a слышит соседей, узнаёт эпоху 1 и остаётся наготове.
        self.assertEqual(real_call, a.agent.client.call)
        a.agent.server = None
        a.agent._serve()
        self.wait_for(lambda: a.agent.holder() == "b", 5, "a узнал эпоху 1")
        time.sleep(1.5)
        self.assertEqual(("b",), self.running())
        self.assert_never_two()

    def test_spare_node_starts_on_the_holders_state_copy(self) -> None:
        """Держатель раздаёт копию state (база SQLite — согласованно, через backup), новый стартует на ней."""
        a, b, c = (self.nodes[name] for name in NAMES)
        for name in ("a", "b"):
            (self.root / "state" / name).mkdir(parents=True, exist_ok=True)   # каталог state роли — от супервизора
        db = self.root / "state" / "a" / "events.sqlite3"
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE topic (camera TEXT, thread INTEGER)")
        conn.execute("INSERT INTO topic VALUES ('yard', 42)")
        conn.commit()   # строка лежит в WAL, основной файл базы её ещё не видел
        (self.root / "state" / "a" / "cameras.json").write_text('{"cameras": [{"camera_id": "yard"}]}')
        (self.root / "state" / "b" / "stale.json").write_text("{}")
        copy = self.root / "state" / "b" / "events.sqlite3"
        self.wait_for(lambda: copy.is_file(), 20, "копия state у запасного b")
        self.assertFalse((self.root / "state" / "b" / "stale.json").exists())
        a.kill()
        conn.close()
        self.wait_for(lambda: self.running() == ("b",), self.DEAD + 6, "b забрал роль")
        with sqlite3.connect(copy) as check:
            self.assertEqual([("yard", 42)], check.execute("SELECT * FROM topic").fetchall())
        self.assertIn("yard", (self.root / "state" / "b" / "cameras.json").read_text())
        self.assertEqual("b", json.loads((self.root / "state" / "b" / "failover.json").read_text())["engine"]["holder"])
        self.assert_never_two()


    def test_frozen_holder_does_not_stall_the_heartbeat(self) -> None:
        """Держатель замер посреди выдачи копии state (соединения принимает, не отвечает):
        загрузка копии у запасных висит, а сердцебиение идёт — роль уходит вовремя."""
        a, b, c = (self.nodes[name] for name in NAMES)
        for name in ("a", "b"):
            (self.root / "state" / name).mkdir(parents=True, exist_ok=True)
        calib = self.root / "state" / "a" / "calib.json"
        calib.write_text("{}")
        self.wait_for(lambda: (self.root / "state" / "b" / "calib.json").is_file(), 20, "копия state у b")
        release = threading.Event()
        self.addCleanup(release.set)
        real_status = a.agent.status

        def hang_status():
            release.wait(30)
            return real_status()

        sha = self.wait_for(lambda: a.agent.snapshot, 10, "копия у a")[0]
        with mock.patch.object(a.agent, "snapshot_data", side_effect=lambda role: release.wait(30) and None):
            calib.write_text('{"yard": 1}')
            self.wait_for(lambda: a.agent.snapshot and a.agent.snapshot[0] != sha, 10, "новая копия у a")
            time.sleep(a.agent.cfg.check_sec + 1)   # запасные уже ждут её у a
            # a замер целиком: процессы роли и циклы агента стоят, сокет принимает и молчит.
            self.addCleanup(a.agent.stop)
            a.stop_event.set()
            a.agent._stop.set()
            frozen = time.monotonic()
            with mock.patch.object(a.agent, "status", side_effect=hang_status):
                self.wait_for(lambda: self.running() == ("b",), self.DEAD + 6, "b забрал роль у замершего a")
        self.assertLess(time.monotonic() - frozen, self.DEAD + 6)
        self.assert_never_two()

class PollConflictTest(unittest.TestCase):
    """Процесс бота с CCTV_POLL_CONFLICT=exit на 409 выходит сразу и больше Telegram не трогает."""

    def test_callback_leaves_on_conflict_only(self) -> None:
        from telegram.error import Conflict, NetworkError

        cfg = mock.Mock(poll_conflict_exit=True)
        left: list[int] = []
        callback = bot_main.poll_error_callback(cfg, exit=left.append)
        callback(NetworkError("timeout"))
        self.assertEqual([], left)
        callback(Conflict("terminated by other getUpdates request"))
        self.assertEqual([bot_main.POLL_CONFLICT_EXIT], left)


class FencedHolderLogTest(unittest.TestCase):
    """Замерший держатель (auto, кворума не слышно) пишет в журнал «замерла», а не
    «наготове: её держит узел <он сам>» — живой прогон на трёх узлах путал этим журнал."""

    def hold(self, holder: str) -> list[str]:
        from cctv import container

        layer = mock.Mock()
        layer.cfg.me = "a"
        layer.holder.return_value = holder
        layer.assignment.return_value = (holder, 3)
        layer.active.return_value = False
        sup = container.Supervisor.__new__(container.Supervisor)
        sup.role, sup.children, sup.stopping, sup.layer = "bot", [], False, layer
        # Один виток цикла ожидания: после него «правка конфига» его завершает.
        ticks = iter([("before",), ("changed",), ("changed",)])
        sup.fingerprint = lambda: next(ticks, ("changed",))
        lines: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(container, "STATUS_PATH", pathlib.Path(tmp) / "supervisor.json"), \
                mock.patch.object(container, "log", lines.append):
            sup.hold_armed(("before",))
        return lines

    def test_fenced_holder_says_frozen(self) -> None:
        lines = self.hold("a")
        self.assertTrue(any("роль bot замерла" in line for line in lines), lines)
        self.assertFalse(any("наготове" in line for line in lines), lines)

    def test_armed_node_names_the_holder(self) -> None:
        self.assertIn("роль bot наготове: её держит узел b (эпоха 3)", self.hold("b"))


class ArmedHealthHolderTest(unittest.TestCase):
    """health узла наготове (`python -m cctv.container health`) называет текущего держателя и
    эпоху — как агент и /nodes — после автоперехватов, возврата кнопками и выхода из pause.

    Раньше супервизор читал держателя один раз при входе в ожидание и писал его в статус
    каждый тик: после двух автоперехватов и возврата кнопками health часами называл
    прежнего держателя, а проснувшийся после pause узел — себя. Агенты настоящие (auto,
    три узла), без сети: назначения приходят через adopt, как от соседей.
    """

    def setUp(self) -> None:
        from cctv import container

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        write_config(self.root, {name: (free_port(), free_port()) for name in NAMES},
                     "auto = true\nheartbeat_sec = 1\ndead_after_sec = 4\n")
        self.lines: list[str] = []
        self.status_path = self.root / "supervisor.json"
        patches = [mock.patch.object(container, "STATUS_PATH", self.status_path),
                   mock.patch.object(container, "TICK_SEC", 0.2),
                   mock.patch.object(container, "log", self.lines.append),
                   mock.patch.dict(os.environ, {"CCTV_CONFIG_DIR": str(self.root), "CCTV_LANG": "en"})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def armed(self, me: str, fenced: bool = False):
        from cctv import container

        agent = Agent(load(self.root, me), "bot", self.root / "state" / me, log=self.lines.append)
        if fenced:
            agent.started_mono -= 1000   # соседей не слышно давно: держатель без кворума
        sup = container.Supervisor.__new__(container.Supervisor)
        sup.role, sup.children, sup.stopping, sup.layer = "bot", [], False, agent
        sup.fingerprint = lambda: ("same",)
        thread = threading.Thread(target=sup.hold_armed, args=(("same",),), daemon=True)
        thread.start()

        def finish():
            sup.stopping = True
            agent.stop()
            thread.join(3)

        self.addCleanup(finish)
        return agent

    def health(self, holder: str, epoch: int) -> None:
        """Статус и health называют holder/epoch — не позже пары тиков после назначения."""
        import contextlib
        import io

        from cctv import container

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                status = json.loads(self.status_path.read_text())
            except (OSError, ValueError):
                status = {}
            if status.get("holder") == holder and status.get("epoch") == epoch and status.get("detail") == holder:
                break
            time.sleep(0.05)
        self.assertEqual((holder, epoch, holder, "armed"),
                         (status.get("holder"), status.get("epoch"), status.get("detail"), status.get("state")),
                         "\n".join(self.lines))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, container.health())
        self.assertEqual(f"armed: the bot role is held by node {holder} (epoch {epoch})", out.getvalue().strip())

    def test_health_follows_automatic_takeovers(self) -> None:
        agent = self.armed("c")
        self.health("a", 0)
        # Держатель a замолчал — роль забрал b; потом b замолчал, вернувшийся a забрал её назад.
        self.assertTrue(agent.adopt("bot", {"holder": "b", "epoch": 1, "prev": "a", "auto": True}))
        self.health("b", 1)
        self.assertTrue(agent.adopt("bot", {"holder": "a", "epoch": 2, "prev": "b", "auto": True}))
        self.health("a", 2)
        self.assertIn("роль bot наготове: её держит узел b (эпоха 1)", self.lines)
        self.assertIn("роль bot наготове: её держит узел a (эпоха 2)", self.lines)

    def test_health_follows_return_by_buttons(self) -> None:
        agent = self.armed("c")
        self.assertTrue(agent.adopt("bot", {"holder": "b", "epoch": 2, "prev": "a", "auto": True}))
        self.health("b", 2)
        # Кнопкой /nodes роль вернули главному узлу: новая эпоха с передачей state.
        self.assertTrue(agent.adopt("bot", {"holder": "a", "epoch": 3, "prev": "b", "carry": True}))
        self.health("a", 3)

    def test_health_after_pause_names_the_new_holder_not_itself(self) -> None:
        # Узел a держал роль, «заснул» (pause) и проснулся без кворума: замер, держит сам.
        agent = self.armed("a", fenced=True)
        self.health("a", 0)
        self.assertTrue(any("роль bot замерла" in line for line in self.lines), self.lines)
        # Пока он спал, роль забрал b — health должен назвать b, а не этот узел.
        self.assertTrue(agent.adopt("bot", {"holder": "b", "epoch": 1, "prev": "a", "auto": True}))
        self.health("b", 1)
        self.assertIn("роль bot наготове: её держит узел b (эпоха 1)", self.lines)


class FreezeReasonLogTest(unittest.TestCase):
    """Замирание держателя в журнале называет одну причину — кворум; конфликт опроса
    Telegram агент пишет отдельной строкой. Раньше одна строка «нет кворума узлов или опрос
    Telegram у другого процесса» не говорила, что случилось."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        write_config(self.root, {name: (free_port(), free_port()) for name in NAMES},
                     "auto = true\nheartbeat_sec = 1\ndead_after_sec = 4\n")
        self.lines: list[str] = []
        self.agent = Agent(load(self.root, "a"), "bot", self.root / "state" / "a", log=self.lines.append)

    def test_holder_without_quorum_names_the_quorum(self) -> None:
        from cctv import container

        self.agent.started_mono -= 1000   # b и c не слышны
        sup = container.Supervisor.__new__(container.Supervisor)
        sup.role, sup.children, sup.stopping, sup.layer = "bot", [], False, self.agent
        sup.wired = ()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(container, "STATUS_PATH", pathlib.Path(tmp) / "supervisor.json"), \
                mock.patch.object(container, "log", self.lines.append):
            sup.supervise({}, ("same",))
        frozen = [line for line in self.lines if line.endswith("— замираю")]
        self.assertEqual(["роль bot: нет кворума узлов — слышно 1 из 3, нужно 2 (слышны: a) — замираю"], frozen)
        self.assertFalse([line for line in self.lines if "Telegram" in line], self.lines)

    def test_poll_conflict_is_a_separate_line(self) -> None:
        # Кворум есть: перехватил устаревший процесс — опрос возобновится, роль не снимается.
        self.assertTrue(self.agent.conflict())
        self.assertIn("опрос Telegram перехватил другой процесс", self.lines[-1])
        self.assertNotIn("кворума нет", self.lines[-1])
        # Кворума нет: роль замирает, и причина названа — опрос у другого процесса без кворума.
        self.agent.started_mono -= 1000
        self.assertFalse(self.agent.conflict())
        self.assertIn("опрос Telegram у другого процесса", self.lines[-1])
        self.assertTrue(self.lines[-1].endswith("— замираю"), self.lines[-1])


class BotAutoFailoverTest(unittest.TestCase):
    """Три супервизора бота (отдельные процессы), auto, мок Telegram с 409 как у настоящего."""

    DEAD = 8

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.config = root / "config"
        self.config.mkdir()
        self.telegram = FakeTelegram(conflicts=True)
        self.addCleanup(self.telegram.close)
        write_config(self.config, {name: (free_port(), free_port()) for name in NAMES},
                     f"auto = true\nheartbeat_sec = 1\ndead_after_sec = {self.DEAD}\n",
                     head=f"[bot]\nchat_id = -100777\nallowed_user_ids = [{OWNER}]\n\n")
        self.procs: dict[str, subprocess.Popen] = {}
        self.dirs = {}
        for name in NAMES:
            node_dir = root / name
            (node_dir / "run").mkdir(parents=True)
            self.dirs[name] = node_dir
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(node_dir), "PYTHONPATH": str(ROOT),
                   "CCTV_CONFIG_DIR": str(self.config), "CCTV_NODE": name,
                   "CCTV_STATE_DIR": str(node_dir / "state"), "CCTV_RUNTIME_DIR": str(node_dir / "run"),
                   "CCTV_SUPERVISOR_STATUS": str(node_dir / "run" / "supervisor.json"),
                   "CCTV_TELEGRAM_API": self.telegram.url(name), "CCTV_EVENTS_PORT": str(free_port()),
                   "CCTV_BRIDGE_URL": f"http://127.0.0.1:{free_port()}", "CCTV_UPDATE_CHECK": "0",
                   "CCTV_LANG": "en"}
            log = open(node_dir / "supervisor.log", "w")
            self.addCleanup(log.close)
            proc = subprocess.Popen([sys.executable, "-m", "cctv.container", "bot"], env=env, cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.procs[name] = proc
            self.addCleanup(self._stop, proc)

    def _stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGCONT)
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)

    def status(self, node: str) -> dict:
        try:
            return json.loads((self.dirs[node] / "run" / "supervisor.json").read_text())
        except (OSError, ValueError):
            return {}

    def logs(self) -> str:
        return "\n".join(f"--- {name}\n" + (path / "supervisor.log").read_text()[-3000:]
                         for name, path in self.dirs.items())

    def wait_for(self, predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        self.fail(f"не дождался: {what}\n{self.logs()}")

    def polled_only_by(self, node: str, settle: bool = False) -> None:
        """Опрашивает Telegram только node. settle — сначала дождаться, пока это так:
        проснувшийся устаревший процесс успевает перебить один опрос держателя, и тот
        возобновляет его после паузы перезапуска."""
        self.wait_for(lambda: node in self.telegram.pollers_since(time.monotonic() - 1), 30, f"опрос с узла {node}")

        def window() -> set[str]:
            since = time.monotonic()
            time.sleep(2.0)
            return self.telegram.pollers_since(since)

        if settle:
            self.wait_for(lambda: window() == {node}, 30, f"опрос только с узла {node}")
        self.assertEqual({node}, window(), self.logs())

    def nodes_panel(self, node: str) -> str:
        before = len(self.telegram.owner_calls("sendMessage"))
        self.telegram.send_text("/nodes")
        self.wait_for(lambda: self.telegram.owner_calls("sendMessage")[before:], 30, "панель /nodes")
        time.sleep(1.5)  # дубль пришёл бы за это время
        sent = self.telegram.owner_calls("sendMessage")[before:]
        self.assertEqual([node], [item[0] for item in sent], "на /nodes ответил не один узел")
        return sent[0][1]["text"]

    def test_hung_holder_is_replaced_and_steps_back_when_it_wakes(self) -> None:
        self.wait_for(lambda: self.status("a").get("state") == "running"
                      and {self.status(n).get("state") for n in ("b", "c")} == {"armed"}, 90,
                      "a держит бота, b и c наготове")
        self.polled_only_by("a")
        text = self.nodes_panel("a")
        self.assertIn("Automatic switching is on: when a node holding a role is silent for 8 s", text)
        self.assertIn("2 of 3 nodes", text)

        # Узел a завис целиком: и агент, и процесс бота.
        os.killpg(self.procs["a"].pid, signal.SIGSTOP)
        hung = time.monotonic()
        self.wait_for(lambda: self.status("b").get("state") == "running", 60, "b забрал бота")
        self.assertGreaterEqual(time.monotonic() - hung, self.DEAD - 1)
        self.assertEqual("armed", self.status("c").get("state"))
        self.polled_only_by("b")
        # Рассылка назначения замершему a не стопорит сердцебиение b: b не замирает «без кворума».
        self.assertNotIn("нет кворума", (self.dirs["b"] / "supervisor.log").read_text())
        text = self.nodes_panel("b")
        self.assertIn("Last switch: bot a → b, automatic (a went silent).", text)
        self.assertRegex(text, r"● a \(priority 1\)\n   engine: does not answer\n   bot: does not answer")

        # a проснулся: его опрос отступает (409 или замирание без кворума), держит бота b.
        sent_before = len(self.telegram.owner_calls("sendMessage"))
        os.killpg(self.procs["a"].pid, signal.SIGCONT)
        self.wait_for(lambda: self.status("a").get("state") == "armed" and self.status("a").get("holder") == "b",
                      30, "проснувшийся a наготове")
        self.polled_only_by("b", settle=True)
        self.assertEqual([], [node for node, _ in self.telegram.owner_calls("sendMessage")[sent_before:]
                              if node != "b"], "проснувшийся узел что-то отправил")
        text = self.nodes_panel("b")
        self.assertRegex(text, r"● a \(priority 1\)\n   engine: .*\n   bot: armed")
        log_a = (self.dirs["a"] / "supervisor.log").read_text()
        self.assertIn("роль bot — узел b (эпоха 1, был a)", log_a)
        # Опрос один на всех: после того как всё улеглось, ни одного пересечения long poll.
        settled = time.monotonic()
        time.sleep(3)
        with self.telegram.lock:
            late = [poll for poll in self.telegram.polls if poll[1] >= settled and poll[0] != "b"]
        self.assertEqual([], late)


if __name__ == "__main__":
    unittest.main()
