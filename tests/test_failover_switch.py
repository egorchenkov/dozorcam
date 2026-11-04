"""Отказоустойчивость: ручное переключение роли из бота на двух локальных экземплярах.

Бот: два настоящих супервизора контейнера (``python -m cctv.container bot``) — узлы
a и b на loopback, каталог конфига общий, state и статус у каждого свои. Бот —
настоящий процесс бота против мока Bot API. Мок различает узлы по префиксу адреса
API (CCTV_TELEGRAM_API у каждого узла свой), держит очередь апдейтов, как Telegram,
и пишет каждый опрос и каждый ответ бота. Человек шлёт /nodes и жмёт кнопки панели —
бот переводит роль на b и обратно. Проверяется: в каждый момент Telegram опрашивает
один узел, на каждое действие человека — ровно один ответ, проверка пригодности видна
в панели.

Движок: два супервизора в процессе теста с подменёнными процессами роли; переключение
нажатием панели через клиент бота (cctv.bot.nodes) — процессы роли никогда не живут на
двух узлах сразу, с ролью переезжает только каталог state, память процессов — нет.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from cctv import container
from cctv.bot import nodes as bot_nodes

from tests.test_failover_armed import TOKEN, SECRET, FakeProc, TcpRecorder, free_port

OWNER = 7
ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTelegram:
    """Мок Bot API для двух узлов: /<узел>/bot<TOKEN>/<метод>.

    getUpdates отдаёт апдейты с update_id ≥ offset (подтверждённые — как в Telegram —
    уходят из очереди), а без них держит long poll ``HOLD_SEC``. deleteWebhook с
    drop_pending_updates очищает очередь. Опросы пишутся интервалами по узлам.
    """

    HOLD_SEC = 0.3
    CONFLICT = object()

    def __init__(self, conflicts: bool = False) -> None:
        # conflicts — как настоящий Telegram: новый getUpdates перебивает long poll другого
        # процесса того же бота, и перебитый опрос получает 409 Conflict.
        self.conflicts = conflicts
        self.conflicted: list[str] = []
        self.terminate: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.queue: list[dict] = []
        self.next_update = 1000
        self.next_message = 500
        self.polls: list[tuple[str, float, float]] = []   # (узел, начало, конец)
        self.inflight: dict[str, int] = {}
        self.overlaps: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, dict]] = []       # (узел, метод, параметры)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, payload) -> None:
                body = json.dumps({"ok": True, "result": payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(body)

            def params(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if "json" in (self.headers.get("Content-Type") or ""):
                    return json.loads(raw or b"{}")
                result = {}
                for key, value in urllib.parse.parse_qsl(raw.decode()):
                    try:
                        result[key] = json.loads(value)
                    except ValueError:
                        result[key] = value
                return result

            def handle_any(self) -> None:
                parts = self.path.split("?")[0].strip("/").split("/")
                if len(parts) != 3 or parts[1] != f"bot{TOKEN}":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                node, method, params = parts[0], parts[2], self.params()
                result = fake.handle(node, method, params)
                if result is fake.CONFLICT:
                    body = json.dumps({"ok": False, "error_code": 409, "description":
                                       "Conflict: terminated by other getUpdates request; "
                                       "make sure that only one bot instance is running"}).encode()
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    with contextlib.suppress(OSError):
                        self.wfile.write(body)
                    return
                self.reply(result)

            do_GET = do_POST = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, node: str) -> str:
        return f"http://127.0.0.1:{self.port}/{node}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- Bot API -----------------------------------------------------------
    def _message(self, chat_id, text: str, markup=None) -> dict:
        with self.lock:
            self.next_message += 1
            message_id = self.next_message
        chat = {"id": int(chat_id), "type": "private" if int(chat_id) > 0 else "supergroup"}
        result = {"message_id": message_id, "date": int(time.time()), "chat": chat, "text": text,
                  "from": {"id": 1234567, "is_bot": True, "first_name": "Dozorcam", "username": "t_bot"}}
        if markup:
            result["reply_markup"] = markup
        return result

    def handle(self, node: str, method: str, params: dict):
        if method == "getUpdates":
            return self._poll(node, params)
        with self.lock:
            self.calls.append((node, method, params))
            if method == "deleteWebhook" and params.get("drop_pending_updates"):
                self.queue.clear()
        if method == "getMe":
            return {"id": 1234567, "is_bot": True, "first_name": "Dozorcam", "username": "t_bot",
                    "can_join_groups": True, "can_read_all_group_messages": False,
                    "supports_inline_queries": False}
        if method in ("sendMessage", "editMessageText"):
            return self._message(params.get("chat_id", OWNER), str(params.get("text") or ""),
                                 params.get("reply_markup"))
        return True

    def _poll(self, node: str, params: dict) -> list:
        start = time.monotonic()
        offset = int(params.get("offset") or 0)
        with self.lock:
            for other, count in self.inflight.items():
                if other != node and count:
                    self.overlaps.append((node, other))
            self.inflight[node] = self.inflight.get(node, 0) + 1
            if self.conflicts:
                for other, event in self.terminate.items():
                    if other != node:
                        event.set()
            terminated = self.terminate[node] = threading.Event()
            # Подтверждённые offset'ом апдейты Telegram больше не отдаёт никому.
            self.queue = [item for item in self.queue if item["update_id"] >= offset]
        try:
            deadline = start + self.HOLD_SEC
            while time.monotonic() < deadline:
                if terminated.is_set():
                    with self.lock:
                        self.conflicted.append(node)
                    return self.CONFLICT
                with self.lock:
                    if self.queue:
                        return list(self.queue)
                time.sleep(0.02)
            return []
        finally:
            with self.lock:
                if self.terminate.get(node) is terminated:
                    del self.terminate[node]
                self.inflight[node] -= 1
                self.polls.append((node, start, time.monotonic()))

    # --- человек -----------------------------------------------------------
    def _push(self, update: dict) -> int:
        with self.lock:
            self.next_update += 1
            update["update_id"] = self.next_update
            self.queue.append(update)
            return self.next_update

    def send_text(self, text: str) -> int:
        with self.lock:
            self.next_message += 1
            message_id = self.next_message
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text[:1] == "/" else []
        return self._push({"message": {
            "message_id": message_id, "date": int(time.time()), "text": text, "entities": entities,
            "chat": {"id": OWNER, "type": "private"},
            "from": {"id": OWNER, "is_bot": False, "first_name": "Owner", "language_code": "en"}}})

    def press(self, message: dict, data: str) -> str:
        query_id = f"q{self.next_update + 1}"
        self._push({"callback_query": {
            "id": query_id, "chat_instance": "ci", "data": data, "message": message,
            "from": {"id": OWNER, "is_bot": False, "first_name": "Owner", "language_code": "en"}}})
        return query_id

    # --- что видел Telegram ----------------------------------------------
    def owner_calls(self, method: str) -> list[tuple[str, dict]]:
        with self.lock:
            return [(node, params) for node, name, params in self.calls
                    if name == method and str(params.get("chat_id", "")) == str(OWNER)]

    def answers(self, query_id: str) -> list[str]:
        with self.lock:
            return [node for node, name, params in self.calls
                    if name == "answerCallbackQuery" and params.get("callback_query_id") == query_id]

    def pollers_since(self, moment: float) -> set[str]:
        with self.lock:
            return {node for node, start, _ in self.polls if start >= moment}


def button(markup: dict | None, prefix: str) -> str:
    for row in (markup or {}).get("inline_keyboard", []):
        for item in row:
            if str(item.get("callback_data", "")).startswith(prefix):
                return item["callback_data"]
    return ""


class BotSwitchTwoInstancesTest(unittest.TestCase):
    """Два экземпляра бота — отдельные процессы супервизора, переключение кнопками бота."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.config = root / "config"
        self.config.mkdir()
        self.telegram = FakeTelegram()
        self.addCleanup(self.telegram.close)
        agents = {name: free_port() for name in ("a", "b")}
        nodes = "".join(
            f'[[failover.nodes]]\nname = "{name}"\npriority = {index}\n'
            f'engine_agent = "http://127.0.0.1:{free_port()}"\nbot_agent = "http://127.0.0.1:{port}"\n'
            for index, (name, port) in enumerate(agents.items(), start=1))
        (self.config / "config.toml").write_text(
            f"[bot]\nchat_id = -100777\nallowed_user_ids = [{OWNER}]\n\n[failover]\ncheck_sec = 5\n" + nodes)
        (self.config / "secrets.toml").write_text(f'bot_token = "{TOKEN}"\n\n[failover]\nsecret = "{SECRET}"\n')
        (self.config / "secrets.toml").chmod(0o600)
        self.procs: dict[str, subprocess.Popen] = {}
        self.dirs = {}
        for name in ("a", "b"):
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
            os.killpg(proc.pid, 15)
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait(5)

    def status(self, node: str) -> dict:
        try:
            return json.loads((self.dirs[node] / "run" / "supervisor.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, proc in self.procs.items():
                if proc.poll() is not None:
                    self.fail(f"супервизор {name} упал: {self.logs()}")
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        self.fail(f"не дождался: {what}\n{self.logs()}")

    def logs(self) -> str:
        return "\n".join(f"--- {name}\n" + (path / "supervisor.log").read_text()[-3000:]
                         for name, path in self.dirs.items())

    def only_poller(self, node: str, since: float) -> None:
        """С момента since Telegram опрашивает только node — и он действительно опрашивает."""
        self.wait_for(lambda: node in self.telegram.pollers_since(time.monotonic() - 1),
                      30, f"опрос Telegram с узла {node}")
        polled_from = time.monotonic()
        time.sleep(1.5)
        self.assertEqual({node}, self.telegram.pollers_since(polled_from))

    def panel(self, node: str) -> tuple[dict, list[str]]:
        """/nodes от владельца: панель приходит одним сообщением и только с узла node."""
        before = len(self.telegram.owner_calls("sendMessage"))
        self.telegram.send_text("/nodes")
        sent = self.wait_for(lambda: self.telegram.owner_calls("sendMessage")[before:], 30, "панель /nodes")
        time.sleep(1.0)  # дубль пришёл бы за это время
        sent = self.telegram.owner_calls("sendMessage")[before:]
        self.assertEqual([node], [item[0] for item in sent], "на /nodes ответил не один узел")
        params = sent[0][1]
        return {"message_id": 0, "date": int(time.time()), "text": params["text"],
                "chat": {"id": OWNER, "type": "private"},
                "from": {"id": 1234567, "is_bot": True, "first_name": "Dozorcam"},
                "reply_markup": params.get("reply_markup")}, sent

    def press(self, node: str, message: dict, data: str) -> dict:
        """Нажатие кнопки: ровно один ответ на callback и одна правка панели — с узла node."""
        before = len(self.telegram.owner_calls("editMessageText"))
        query_id = self.telegram.press(message, data)
        self.wait_for(lambda: self.telegram.answers(query_id), 30, f"ответ на кнопку {data}")
        time.sleep(1.0)
        self.assertEqual([node], self.telegram.answers(query_id), f"на кнопку {data} ответили не один раз")
        edits = self.telegram.owner_calls("editMessageText")[before:]
        self.assertEqual([node], [item[0] for item in edits])
        return {**message, "text": edits[0][1]["text"], "reply_markup": edits[0][1].get("reply_markup")}

    def switch_bot(self, source: str, target: str) -> None:
        message, _ = self.panel(source)
        go = button(message["reply_markup"], f"nd:go:bot:{target}")
        self.assertTrue(go, f"в панели нет кнопки «bot → {target}»: {message}")
        confirm = self.press(source, message, go)
        ok = button(confirm["reply_markup"], f"nd:ok:bot:{target}:")
        self.assertTrue(ok, confirm)
        started = self.press(source, confirm, ok)
        self.assertIn(f"Switching the bot to {target}", started["text"])
        moment = time.monotonic()
        self.wait_for(lambda: self.status(target).get("state") == "running"
                      and self.status(source).get("state") == "armed", 120, f"бот на узле {target}")
        self.assertEqual(target, self.status(source).get("holder"))
        self.only_poller(target, moment)

    def test_switch_bot_there_and_back_without_duplicates(self) -> None:
        self.wait_for(lambda: self.status("a").get("state") == "running"
                      and self.status("b").get("state") == "armed", 60, "a держит бота, b наготове")
        self.only_poller("a", time.monotonic())

        # Проверка пригодности раз в check_sec видна в панели: и у своего узла, и у запасного.
        message, _ = self.panel("a")
        text = message["text"]
        self.assertIn("Fitness is checked every 5 s.", text)
        self.assertRegex(text, r"● a \(priority 1\) — this node\n.*\n   bot: active · Telegram reachable · "
                               r"checked \d+ s ago")
        self.assertRegex(text, r"● b \(priority 2\)\n.*\n   bot: armed · Telegram reachable · checked \d+ s ago")
        # Возраст проверки не растёт дальше периода: проба идёт по таймеру, а не однажды.
        time.sleep(6)
        message, _ = self.panel("a")
        ages = [int(age) for age in re.findall(r"checked (\d+) s ago", message["text"])]
        self.assertEqual(2, len(ages), message["text"])
        self.assertTrue(all(age <= 5 + 3 for age in ages), message["text"])

        self.switch_bot("a", "b")
        self.switch_bot("b", "a")

        # Итог: ни одного опроса Telegram двумя узлами одновременно (409 Conflict у настоящего API),
        # а на каждое действие человека ответил ровно один узел (проверено по ходу).
        self.assertEqual([], self.telegram.overlaps)
        message, _ = self.panel("a")
        self.assertIn("Last switch: bot b → a, done.", message["text"])


class EngineSwitchTest(unittest.TestCase):
    """Движок: переключение кнопкой панели (клиент бота → агент бота → агенты движка)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = pathlib.Path(self.tmp.name)
        self.config, self.run_dir = root / "config", root / "run"
        self.config.mkdir()
        self.run_dir.mkdir()
        self.camera = TcpRecorder()
        self.addCleanup(self.camera.close)
        (self.config / "cameras.json").write_text(json.dumps({"cameras": [{
            "camera_id": "yard", "title": "Yard", "rtsp_url": f"rtsp://viewer:pw@127.0.0.1:{self.camera.port}/main"}]}))
        nodes = "".join(
            f'[[failover.nodes]]\nname = "{name}"\npriority = {index}\n'
            f'engine_agent = "http://127.0.0.1:{free_port()}"\nbot_agent = "http://127.0.0.1:{free_port()}"\n'
            for index, name in enumerate(("a", "b"), start=1))
        (self.config / "config.toml").write_text(
            f"[bot]\nchat_id = -100777\nallowed_user_ids = [{OWNER}]\n\n[failover]\ncheck_sec = 5\n" + nodes)
        (self.config / "secrets.toml").write_text(f'[failover]\nsecret = "{SECRET}"\n')
        (self.config / "secrets.toml").chmod(0o600)
        self.started: list[FakeProc] = []
        self.timeline: list[tuple[float, str, int]] = []   # (время, узел, +1/−1) процессов движка

        test = self

        class Proc(FakeProc):
            def __init__(self, argv, env=None):
                super().__init__(argv, env)
                self.node = (env or {}).get("CCTV_NODE", "?")
                self.env = dict(env or {})
                self.memory = {"started_at": time.monotonic()}   # «память процесса»
                if "bot" not in self.argv:
                    test.timeline.append((time.monotonic(), self.node, +1))

            def terminate(self):
                if self.returncode is None and "bot" not in self.argv:
                    test.timeline.append((time.monotonic(), self.node, -1))
                super().terminate()

            kill = terminate

        def popen(argv, env=None, **_kw):
            proc = Proc(argv, env)
            self.started.append(proc)
            return proc

        for patcher in (mock.patch.object(container, "STATUS_PATH", self.run_dir / "supervisor.json"),
                        mock.patch.object(container, "RUN_DIR", self.run_dir),
                        mock.patch.object(container.subprocess, "Popen", side_effect=popen),
                        mock.patch.object(container.signal, "signal"),
                        mock.patch.object(container, "TICK_SEC", 0.1),
                        # четыре супервизора в одном процессе: общий файл статуса им не нужен
                        mock.patch.object(container, "write_status"),
                        mock.patch.object(container, "token_rejected", return_value=""),
                        mock.patch("cctv.failover.agent.SWITCH_DELAY_SEC", 0.2)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sups = {}
        for name in ("a", "b"):
            for cls in (container.EngineSupervisor, container.BotSupervisor):
                self.sups[(name, cls.role)] = self.launch(cls, name)

    def launch(self, cls, node: str):
        env = {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(self.config), "CCTV_NODE": node,
               "CCTV_STATE_DIR": str(self.root / node / "state"), "CCTV_BOT_TOKEN": TOKEN,
               "CCTV_TELEGRAM_API": f"http://127.0.0.1:{free_port()}"}
        with mock.patch.dict(os.environ, env, clear=True):
            sup = cls()
        if isinstance(sup, container.EngineSupervisor):
            sup.proxy_config = self.run_dir / f"cameras.proxy.{node}.json"
            sup.empty_config = self.run_dir / f"cameras.empty.{node}.json"
        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                sup.run()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def stop():
            sup.stopping = True
            thread.join(20)

        self.addCleanup(stop)
        return sup

    def wait_for(self, predicate, timeout: float = 30.0, what: str = ""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail(f"не дождался: {what}")

    def engine_procs(self, node: str) -> list[FakeProc]:
        return [proc for proc in self.started if proc.node == node and "bot" not in proc.argv]

    def alive(self, node: str) -> int:
        return sum(1 for proc in self.engine_procs(node) if proc.poll() is None)

    def bot_panel(self) -> bot_nodes.NodesPanel:
        """Клиент панели, каким его видит процесс бота узла a (адрес и ключ — из его окружения)."""
        bot = next(proc for proc in self.started if proc.node == "a" and "bot" in proc.argv)
        return bot_nodes.NodesPanel(bot.env["CCTV_NODES_URL"], bot.env["CCTV_NODES_TOKEN"])

    def press_through_bot(self, panel: bot_nodes.NodesPanel, target: str) -> dict:
        view = asyncio.run(panel.view("en"))
        go = next(item["data"] for row in view["buttons"] for item in row if item["data"] == f"go:engine:{target}")
        confirm = asyncio.run(panel.press(bot_nodes.PREFIX + go, "en"))
        ok = next(item["data"] for row in confirm["buttons"] for item in row if item["data"].startswith("ok:"))
        return asyncio.run(panel.press(bot_nodes.PREFIX + ok, "en"))

    def test_engine_switch_there_and_back_state_on_disk_memory_lost(self) -> None:
        engine_a, engine_b = self.sups[("a", "engine")], self.sups[("b", "engine")]
        self.wait_for(lambda: self.alive("a") == 3 and engine_a.layer.running, what="движок на a")
        self.wait_for(lambda: any(proc.node == "a" and "bot" in proc.argv for proc in self.started), what="бот на a")
        self.assertEqual(0, self.alive("b"))
        first = self.engine_procs("a")
        for proc in first:
            proc.memory["seen_person"] = True          # память процессов — только в них
        state_a = self.root / "a" / "state"
        state_a.mkdir(parents=True, exist_ok=True)
        (state_a / "cameras.json").write_text('{"cameras": [{"camera_id": "yard"}]}')   # диск роли
        (state_a / "diag").mkdir(exist_ok=True)
        (state_a / "diag" / "today.log").write_text("node a diagnostics")              # журнал узла

        panel = self.bot_panel()
        reply = self.press_through_bot(panel, "b")
        self.assertIn("Switching the engine to b", reply["text"])
        self.wait_for(lambda: self.alive("b") == 3 and engine_b.layer.running and self.alive("a") == 0,
                      what="движок переехал на b")
        self.assertFalse(engine_a.layer.active())

        # С ролью переехал диск (реестр), а не диагностика узла и не память процессов.
        state_b = self.root / "b" / "state"
        self.assertEqual('{"cameras": [{"camera_id": "yard"}]}', (state_b / "cameras.json").read_text())
        self.assertFalse((state_b / "diag").exists())
        second = self.engine_procs("b")
        self.assertTrue(second)
        self.assertTrue(all(proc not in first for proc in second))
        self.assertTrue(all("seen_person" not in proc.memory for proc in second))
        self.assertTrue(all(proc.env["CCTV_NODE"] == "b" for proc in second))
        carried = {key: value for key, value in second[0].env.items() if key.startswith("CCTV_")}
        self.assertNotIn("seen_person", json.dumps(carried))

        # Обратно — тем же путём; панель показывает итог прошлого переключения.
        view = asyncio.run(panel.view("en"))
        self.assertIn("Last switch: engine a → b, done.", view["text"])
        self.press_through_bot(panel, "a")
        self.wait_for(lambda: self.alive("a") == 3 and self.alive("b") == 0 and engine_a.layer.running,
                      what="движок вернулся на a")
        self.assertTrue(all(proc not in first for proc in self.engine_procs("a")[len(first):]))

        # Ни в один момент процессы движка (= RTSP/ONVIF к камерам) не жили на двух узлах сразу.
        count = {"a": 0, "b": 0}
        for _, node, delta in sorted(self.timeline):
            count[node] += delta
            self.assertFalse(count["a"] and count["b"], f"движок жил на двух узлах сразу: {self.timeline}")


if __name__ == "__main__":
    unittest.main()
