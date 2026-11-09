"""Отказоустойчивость: узел наготове (armed) не ходит к камерам и не опрашивает Telegram.

Два узла на loopback (a — главный, b — запасной), каталог конфига один. Супервизор
роли запускается по-настоящему (run) с агентом слоя; процессы роли подменены —
запуск любого из них и есть «открыл RTSP/ONVIF» или «стал опрашивать Telegram»,
поэтому armed-узел не должен запустить ни одного. Камера и Bot API — локальные
приёмники, которые записывают каждое соединение и каждый запрос.
"""
from __future__ import annotations

import contextlib
import http.server
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

from cctv import container
from cctv.failover import probe

TOKEN = "1234567:AAFakeTokenForUnitTestsOnly000000000"
SECRET = "unit-test-failover-secret-0123456789"


_ISSUED_PORTS: set[int] = set()


def free_port() -> int:
    """Свободный порт loopback, ни разу не выданный в этом процессе: ядро может вернуть
    только что освобождённый порт повторно, и два агента конфига получали один адрес."""
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _ISSUED_PORTS:
            _ISSUED_PORTS.add(port)
            return port


class TcpRecorder:
    """TCP-приёмник «камеры»: сколько соединений и что в них прислали."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections: list[bytes] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(1.0)
                data = b""
                try:
                    while chunk := conn.recv(4096):
                        data += chunk
                except OSError:
                    pass
                self.connections.append(data)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(2)
        self.sock.close()


class BotApiRecorder:
    """Bot API: отвечает ok на всё и записывает методы (getMe, getUpdates…)."""

    def __init__(self) -> None:
        methods = self.methods = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self):
                methods.append(self.path.rsplit("/", 1)[-1].split("?")[0])
                body = json.dumps({"ok": True, "result": {"id": 1, "is_bot": True, "username": "t"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _reply

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeProc:
    """Процесс роли вместо настоящего: жив, пока его не остановили."""

    pid = 4242

    def __init__(self, argv, env=None) -> None:
        self.argv, self.returncode = list(argv), None
        if "rtsp-proxy" in self.argv and env and env.get("CCTV_PROXY_CAMERA_CONFIG"):
            # как настоящий прокси: runtime-реестр, без которого мост и конвейер ждут
            pathlib.Path(env["CCTV_PROXY_CAMERA_CONFIG"]).write_text('{"cameras": []}')

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    kill = terminate

    def wait(self, _timeout=None):
        return self.returncode


class ArmedNodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.config, self.run_dir = root / "config", root / "run"
        self.config.mkdir()
        self.run_dir.mkdir()
        self.camera = TcpRecorder()               # основной поток (rtsp_url)
        self.camera_http = TcpRecorder()          # снимки/веб камеры (snapshot_url, ONVIF-порт)
        self.camera_sub = TcpRecorder()           # поток детекции (detect_rtsp_url)
        self.telegram = BotApiRecorder()
        for closer in (self.camera.close, self.camera_http.close, self.camera_sub.close, self.telegram.close):
            self.addCleanup(closer)
        host = "127.0.0.1"
        (self.config / "cameras.json").write_text(json.dumps({"cameras": [{
            "camera_id": "yard", "title": "Yard",
            "rtsp_url": f"rtsp://viewer:pw@{host}:{self.camera.port}/main",
            "detect_rtsp_url": f"rtsp://viewer:pw@{host}:{self.camera_sub.port}/sub",
            "snapshot_url": f"http://{host}:{self.camera_http.port}/onvif/snapshot"}]}))
        ports = {name: (free_port(), free_port()) for name in ("a", "b")}
        nodes = "".join(
            f'[[failover.nodes]]\nname = "{name}"\npriority = {index}\n'
            f'engine_agent = "http://127.0.0.1:{engine}"\nbot_agent = "http://127.0.0.1:{bot}"\n'
            for index, (name, (engine, bot)) in enumerate(ports.items(), start=1))
        (self.config / "config.toml").write_text(
            "[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n\n[failover]\ncheck_sec = 5\n" + nodes)
        (self.config / "secrets.toml").write_text(f'[failover]\nsecret = "{SECRET}"\n')
        (self.config / "secrets.toml").chmod(0o600)
        self.base_env = {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(self.config),
                         "CCTV_STATE_DIR": str(root / "state"), "CCTV_BOT_TOKEN": TOKEN,
                         "CCTV_TELEGRAM_API": self.telegram.url}
        self.started: list[FakeProc] = []

        def popen(argv, env=None, **_kw):
            proc = FakeProc(argv, env)
            self.started.append(proc)
            return proc

        for patcher in (mock.patch.object(container, "STATUS_PATH", self.run_dir / "supervisor.json"),
                        mock.patch.object(container, "RUN_DIR", self.run_dir),
                        mock.patch.object(container.subprocess, "Popen", side_effect=popen),
                        mock.patch.object(container.signal, "signal"),
                        mock.patch.object(container, "TICK_SEC", 0.1)):
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- обвязка -------------------------------------------------------------
    def launch(self, cls, node: str):
        with mock.patch.dict(os.environ, {**self.base_env, "CCTV_NODE": node}, clear=True):
            sup = cls()
        if isinstance(sup, container.EngineSupervisor):
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
        return sup

    def wait_for(self, predicate, timeout: float = 15.0, what: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"не дождался: {what}")

    def status(self) -> dict:
        try:
            return json.loads((self.run_dir / "supervisor.json").read_text())
        except (OSError, ValueError):
            return {}

    def checked(self, sup) -> bool:
        return sup.layer is not None and bool(sup.layer.health.get("checked_at"))

    # --- armed ---------------------------------------------------------------
    def test_armed_engine_starts_no_camera_process_and_only_probes_tcp(self) -> None:
        sup = self.launch(container.EngineSupervisor, "b")
        self.wait_for(lambda: self.status().get("state") == "armed", what="статус armed")
        self.wait_for(lambda: self.checked(sup), what="проверка пригодности")
        time.sleep(0.5)  # и ещё несколько тиков супервизора
        self.assertEqual([], [proc.argv for proc in self.started],
                         "armed-узел запустил процессы движка (rtsp-proxy/мост/конвейер = RTSP/ONVIF к камерам)")
        self.assertEqual({"state": "armed", "role": "engine", "node": "b", "holder": "a"},
                         {key: self.status().get(key) for key in ("state", "role", "node", "holder")})
        self.assertEqual({"fit": True, "ok": 1, "total": 1},
                         {key: sup.layer.health.get(key) for key in ("fit", "ok", "total")})
        # Проба — голое TCP-соединение на порт основного потока: ни байта RTSP
        # (OPTIONS/DESCRIBE), к снимкам/ONVIF и потоку детекции — ни одного соединения.
        self.assertTrue(self.camera.connections)
        self.assertEqual({b""}, set(self.camera.connections))
        self.assertEqual([], self.camera_http.connections)
        self.assertEqual([], self.camera_sub.connections)
        # healthcheck контейнера зелёный: armed — штатное состояние, не авария.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, container.health())
        self.assertIn("armed", out.getvalue())

    def test_armed_bot_does_not_poll_telegram(self) -> None:
        sup = self.launch(container.BotSupervisor, "b")
        self.wait_for(lambda: self.status().get("state") == "armed", what="статус armed")
        self.wait_for(lambda: self.checked(sup), what="проверка пригодности")
        time.sleep(0.5)
        self.assertEqual([], [proc.argv for proc in self.started], "armed-узел запустил процесс бота")
        self.assertTrue(sup.layer.health.get("fit"))
        # Пригодность — getMe; getUpdates (опрос, который конфликтует с активным ботом) — никогда.
        self.assertIn("getMe", self.telegram.methods)
        self.assertEqual({"getMe"}, set(self.telegram.methods))

    def test_active_node_starts_role_and_freezes_into_armed_when_role_leaves(self) -> None:
        """Контроль: тот же конфиг на главном узле запускает роль; роль ушла — процессы
        остановлены, узел наготове и больше их не запускает."""
        sup = self.launch(container.EngineSupervisor, "a")
        self.wait_for(lambda: any("rtsp-proxy" in proc.argv for proc in self.started),
                      what="запуск rtsp-proxy на активном узле")
        self.wait_for(lambda: self.status().get("state") == "running", what="статус running")
        sup.layer.adopt("engine", {"holder": "b", "epoch": 1, "prev": "a", "carry": False})
        self.wait_for(lambda: self.status().get("state") == "armed", what="замирание")
        self.assertTrue(all(proc.poll() is not None for proc in self.started), "процессы роли не остановлены")
        started = len(self.started)
        connections = len(self.camera.connections)
        time.sleep(1.0)
        self.assertEqual(started, len(self.started), "замерший узел снова запустил процессы")
        self.assertFalse(sup.layer.running)
        self.assertEqual(connections, len(self.camera.connections))  # проба — раз в check_sec, не каждый тик


class ProbeTest(unittest.TestCase):
    """Проба пригодности сама по себе: только TCP-connect и только getMe."""

    def test_camera_probe_sends_nothing_and_counts_reachable(self) -> None:
        camera = TcpRecorder()
        self.addCleanup(camera.close)
        dead = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            registry = pathlib.Path(tmp) / "cameras.json"
            registry.write_text(json.dumps({"cameras": [
                {"camera_id": "a", "rtsp_url": f"rtsp://u:p@127.0.0.1:{camera.port}/x"},
                {"camera_id": "b", "rtsp_url": f"rtsp://u:p@127.0.0.1:{dead}/x"},
                {"camera_id": "c", "rtsp_url": "rtsp://u:p@127.0.0.1:1/x", "enabled": False}]}))
            result = probe.cameras(registry)
        self.assertEqual({"fit": True, "ok": 1, "total": 2}, result)
        deadline = time.monotonic() + 3
        while not camera.connections and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([b""], camera.connections)

    def test_camera_probe_unfit_when_nothing_reachable_and_fit_without_cameras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = pathlib.Path(tmp) / "cameras.json"
            registry.write_text(json.dumps({"cameras": [{"rtsp_url": f"rtsp://127.0.0.1:{free_port()}/x"}]}))
            self.assertFalse(probe.cameras(registry)["fit"])
            registry.write_text('{"cameras": []}')
            self.assertEqual({"fit": True, "ok": 0, "total": 0}, probe.cameras(registry))

    def test_telegram_probe_is_get_me_only(self) -> None:
        api = BotApiRecorder()
        self.addCleanup(api.close)
        self.assertEqual({"fit": True, "reason": ""}, probe.telegram(api.url, TOKEN))
        self.assertEqual(["getMe"], api.methods)
        self.assertEqual({"fit": False, "reason": "no_token"}, probe.telegram(api.url, ""))
        self.assertEqual({"fit": False, "reason": "unreachable"},
                         probe.telegram(f"http://127.0.0.1:{free_port()}", TOKEN, timeout=2))


if __name__ == "__main__":
    unittest.main()
