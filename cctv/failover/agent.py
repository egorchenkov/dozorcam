"""Агент роли: живёт в супервизоре контейнера и решает «включись или замри».

На узле два агента — у супервизора движка и у супервизора бота: каждый
управляет только процессами своей роли. Назначение «кто держит роль»
хранится у каждого агента для обеих ролей как (узел, эпоха): эпоху повышает
только переключение, при расхождении побеждает бо́льшая эпоха, при равной —
узел с большим приоритетом. Раз в check_sec агент проверяет свою пригодность
(probe) и сверяет назначения с соседями — пропустивший переключение узел
догоняет сам.

Переключение (ручное, из панели бота): сначала старый держатель замирает и
подтверждает, что процессы роли остановлены, потом новый забирает его каталог
state и запускается. Двух активных на роль не бывает даже на секунду; не
поднялся новый — роль возвращается старому.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import pathlib
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__
from . import handover, panel, wire
from .config import ROLES, Failover

STATE_FILE = "failover.json"
STARTUP_TIMEOUT_SEC = 3.0
PEER_TIMEOUT_SEC = 3.0
# Замирание: супервизор гасит процессы роли (до 15 с на процесс), активация —
# prepare (getMe у бота) и запуск процессов.
FREEZE_TIMEOUT_SEC = 60.0
ACTIVATE_TIMEOUT_SEC = 90.0
HANDOVER_TIMEOUT_SEC = 60.0
# Пауза перед переключением: бот успевает показать «переключаю» до того,
# как его самого остановят.
SWITCH_DELAY_SEC = 3.0
MAX_BODY = 64 * 1024


def _assignment(holder: str, epoch: int, prev: str = "", carry: bool = False) -> dict:
    return {"holder": holder, "epoch": int(epoch), "prev": prev or holder, "carry": bool(carry)}


class Agent:
    def __init__(self, cfg: Failover, role: str, state_dir: pathlib.Path, probe=None, *,
                 log=print, clock=time.time) -> None:
        if role not in ROLES:
            raise ValueError(role)
        self.cfg, self.role, self.state_dir = cfg, role, pathlib.Path(state_dir)
        self.probe = probe or (lambda: {"fit": True})
        self.log, self.clock = log, clock
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.assign = self._load()
        self.running = False
        self.health: dict = {"fit": None, "checked_at": 0.0}
        self.peers_seen: dict[str, dict] = {}
        self.switching: dict | None = None
        self.last_switch: dict | None = None
        self._switch_lock = threading.Lock()
        self.panel_token = secrets.token_urlsafe(24)
        self.client = wire.Client(cfg, clock)
        self.verifier = wire.Verifier(cfg.secret, clock)
        self.server: ThreadingHTTPServer | None = None
        self.panel_server: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- что видит супервизор: «включись / замри» ------------------------------
    def active(self) -> bool:
        with self.lock:
            return self.assign[self.role]["holder"] == self.cfg.me

    def holder(self, role: str | None = None) -> str:
        with self.lock:
            return self.assign[role or self.role]["holder"]

    def report(self, running: bool) -> None:
        """Супервизор: процессы роли запущены (True) или остановлены (False)."""
        with self.changed:
            if running != self.running:
                self.running = running
                self.changed.notify_all()

    def wait(self, timeout: float) -> None:
        """Сон супервизора, который прерывает смена назначения."""
        with self.changed:
            self.changed.wait(timeout)

    def wiring(self) -> dict[str, str]:
        """Адрес соседней роли для процессов этой: мост для бота, приёмник событий для движка."""
        other = "bot" if self.role == "engine" else "engine"
        node = self.cfg.node(self.holder(other))
        if node is None:
            return {}
        if self.role == "bot" and node.bridge_url:
            return {"CCTV_BRIDGE_URL": node.bridge_url}
        if self.role == "engine" and node.events_url:
            return {"CCTV_EVENTS_URL": node.events_url}
        return {}

    def panel_env(self) -> dict[str, str]:
        """Боту — адрес панели узлов и одноразовый (на запуск супервизора) ключ к ней."""
        if self.panel_server is None:
            return {}
        port = self.panel_server.server_address[1]
        return {"CCTV_NODES_URL": f"http://127.0.0.1:{port}", "CCTV_NODES_TOKEN": self.panel_token}

    # --- жизненный цикл --------------------------------------------------------
    def start(self) -> None:
        self._serve()
        self.gossip(timeout=STARTUP_TIMEOUT_SEC)
        thread = threading.Thread(target=self._loop, name=f"failover-{self.role}", daemon=True)
        thread.start()
        self._threads.append(thread)
        self.log(f"отказоустойчивость: узел {self.cfg.me}, роль {self.role}, "
                 f"держит {self.holder()} (эпоха {self.assign[self.role]['epoch']})")

    def stop(self) -> None:
        self._stop.set()
        for server in (self.server, self.panel_server):
            if server is not None:
                server.shutdown()
                server.server_close()
        self.server = self.panel_server = None
        with self.changed:
            self.changed.notify_all()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.check()
            self._stop.wait(self.cfg.check_sec)

    def check(self) -> None:
        """Раз в check_sec: своя пригодность и сверка назначений с соседями."""
        try:
            result = dict(self.probe())
        except Exception as exc:  # проба не должна ронять агента
            result = {"fit": False, "reason": type(exc).__name__}
        result["checked_at"] = self.clock()
        with self.lock:
            self.health = result
        self.gossip()

    # --- назначения ------------------------------------------------------------
    def _load(self) -> dict:
        top = self.cfg.top
        result = {role: _assignment(top, 0) for role in ROLES}
        try:
            saved = json.loads((self.state_dir / STATE_FILE).read_text())
        except (OSError, ValueError):
            return result
        for role in ROLES:
            item = saved.get(role) if isinstance(saved, dict) else None
            if (isinstance(item, dict) and self.cfg.node(str(item.get("holder"))) is not None
                    and isinstance(item.get("epoch"), int)):
                result[role] = _assignment(item["holder"], item["epoch"], str(item.get("prev") or ""),
                                           bool(item.get("carry")))
        return result

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / STATE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.assign))
        os.replace(tmp, path)

    def _newer(self, item: dict, current: dict) -> bool:
        if item["epoch"] != current["epoch"]:
            return item["epoch"] > current["epoch"]
        # Равная эпоха и разные держатели — побеждает главный по приоритету: все узлы
        # приходят к одному ответу без переговоров.
        return self.cfg.rank(item["holder"]) < self.cfg.rank(current["holder"])

    def adopt(self, role: str, item: dict) -> bool:
        """Принять назначение, если оно новее своего. True — назначение сменилось."""
        try:
            item = _assignment(str(item["holder"]), int(item["epoch"]), str(item.get("prev") or ""),
                               bool(item.get("carry")))
        except (KeyError, TypeError, ValueError):
            return False
        if role not in ROLES or self.cfg.node(item["holder"]) is None:
            return False
        with self.lock:
            if not self._newer(item, self.assign[role]):
                return False
            taking = (role == self.role and item["holder"] == self.cfg.me
                      and self.assign[role]["holder"] != self.cfg.me)
        if taking and item["carry"] and item["prev"] != self.cfg.me and not self._take_state(item["prev"]):
            return False
        with self.changed:
            if not self._newer(item, self.assign[role]):
                return False
            before = self.assign[role]["holder"]
            self.assign[role] = item
            self._save()
            self.changed.notify_all()
        self.log(f"отказоустойчивость: роль {role} — узел {item['holder']} (эпоха {item['epoch']}, "
                 f"был {before})")
        return True

    def _take_state(self, source: str) -> bool:
        """Каталог state роли с узла source. False — source ещё не замер: принимать роль рано.

        Не ответил вовсе — роль принимается на своём state: держатель недоступен,
        а назначение с ним уже согласовано (иначе эпохи бы не было).
        """
        node = self.cfg.node(source)
        if node is None:
            return True
        try:
            data = self.client.call(node.agent(self.role), "GET", f"/v1/handover?role={self.role}",
                                    timeout=HANDOVER_TIMEOUT_SEC, raw=True)
        except wire.WireError as exc:
            if str(exc) == "HTTP 409":
                self.log(f"отказоустойчивость: узел {source} ещё держит роль {self.role}, жду")
                return False
            self.log(f"отказоустойчивость: state роли {self.role} с узла {source} не получен ({exc}), "
                     "запускаюсь на своём")
            return True
        try:
            files = handover.unpack(data, self.state_dir)
        except handover.HandoverError as exc:
            self.log(f"отказоустойчивость: state роли {self.role} с узла {source} не разобран ({exc})")
            return False
        self.log(f"отказоустойчивость: state роли {self.role} принят с узла {source} (файлов {files})")
        return True

    def wait_running(self, want: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.changed:
            while self.running != want:
                left = deadline - time.monotonic()
                if left <= 0 or self._stop.is_set():
                    return False
                self.changed.wait(min(left, 1.0))
            return True

    # --- соседи ----------------------------------------------------------------
    def agents(self) -> list[tuple[str, str, str]]:
        """(узел, роль, адрес) всех агентов установки, начиная с главного узла."""
        return [(node.name, role, node.agent(role)) for node in self.cfg.ordered for role in ROLES]

    def own_url(self) -> str:
        return self.cfg.node(self.cfg.me).agent(self.role)

    def call(self, url: str, method: str, path: str, payload: dict | None = None, *,
             timeout: float = PEER_TIMEOUT_SEC) -> dict:
        """Запрос агенту; к себе — без сети (агент бота управляет и своей ролью)."""
        if url == self.own_url():
            if method == "GET" and path == "/v1/status":
                return self.status()
            if method == "POST" and path == "/v1/assign":
                return self.handle_assign(payload or {})
        return self.client.call(url, method, path, payload, timeout=timeout)

    def statuses(self, timeout: float = PEER_TIMEOUT_SEC) -> dict[tuple[str, str], dict | None]:
        """Статусы всех агентов параллельно; не ответил — None."""
        targets = self.agents()

        def fetch(target):
            try:
                return self.call(target[2], "GET", "/v1/status", timeout=timeout)
            except wire.WireError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
            results = list(pool.map(fetch, targets))
        found = {(node, role): result for (node, role, _), result in zip(targets, results)}
        with self.lock:
            now = self.clock()
            for (node, role), result in found.items():
                if result is not None:
                    self.peers_seen[f"{node}/{role}"] = {"at": now}
        return found

    def gossip(self, timeout: float = PEER_TIMEOUT_SEC) -> dict:
        found = self.statuses(timeout)
        for result in found.values():
            for role in ROLES:
                item = (result or {}).get("assign", {}).get(role)
                if isinstance(item, dict):
                    self.adopt(role, item)
        return found

    def status(self) -> dict:
        with self.lock:
            return {"node": self.cfg.me, "role": self.role, "version": __version__,
                    "assign": json.loads(json.dumps(self.assign)), "running": self.running,
                    "active": self.assign[self.role]["holder"] == self.cfg.me,
                    "health": dict(self.health), "now": self.clock()}

    def handle_assign(self, payload: dict) -> dict:
        role = payload.get("role")
        if role not in ROLES:
            return {"ok": False, "error": "bad_role"}
        changed = self.adopt(role, payload)
        with self.lock:
            current = dict(self.assign[role])
        same = (current["holder"] == payload.get("holder") and current["epoch"] == payload.get("epoch"))
        if not same:
            return {"ok": False, "error": "stale", "assign": current}
        if role == self.role and payload.get("wait"):
            want = current["holder"] == self.cfg.me
            timeout = ACTIVATE_TIMEOUT_SEC if want else FREEZE_TIMEOUT_SEC
            if not self.wait_running(want, timeout):
                return {"ok": False, "error": "timeout", "running": self.running, "assign": current}
        return {"ok": True, "changed": changed, "running": self.running, "assign": current}

    def handover_data(self, role: str) -> bytes | None:
        """Архив state для нового держателя — только если роль здесь замерла."""
        if role != self.role or self.active() or self.running:
            return None
        return handover.pack(self.state_dir)

    # --- переключение ----------------------------------------------------------
    def request_switch(self, role: str, target: str) -> str:
        """Начать переключение в фоне; код ответа для панели."""
        if role not in ROLES or self.cfg.node(target) is None:
            return "bad"
        if not self._switch_lock.acquire(blocking=False):
            return "busy"
        with self.lock:
            self.switching = {"role": role, "from": self.holder(role), "to": target, "at": self.clock()}
        thread = threading.Thread(target=self._switch_thread, args=(role, target), daemon=True)
        thread.start()
        return "started"

    def _switch_thread(self, role: str, target: str) -> None:
        try:
            time.sleep(SWITCH_DELAY_SEC)
            outcome = self.switch(role, target)
        except Exception as exc:  # переключение не должно ронять агента
            outcome = {"ok": False, "error": type(exc).__name__}
        finally:
            with self.lock:
                self.switching = None
            self._switch_lock.release()
        with self.lock:
            self.last_switch = {"role": role, "to": target, "at": self.clock(),
                                "epoch": self.assign[role]["epoch"], **outcome}
        self.log(f"отказоустойчивость: переключение {role} → {target}: {outcome}")

    def switch(self, role: str, target: str) -> dict:
        found = self.gossip()
        with self.lock:
            current = dict(self.assign[role])
        old = current["holder"]
        if old == target:
            return {"ok": True, "error": "already", "from": old}
        urls = {node.name: node.agent(role) for node in self.cfg.nodes}
        view = found.get((target, role))
        if view is None:
            return {"ok": False, "error": "target_unreachable", "from": old}
        if view.get("health", {}).get("fit") is False:
            return {"ok": False, "error": "target_unfit", "from": old}
        if found.get((old, role)) is None:
            return {"ok": False, "error": "holder_unreachable", "from": old}
        epoch = current["epoch"] + 1
        move = {"role": role, "holder": target, "epoch": epoch, "prev": old, "carry": True}
        # 1. Старый держатель замирает и подтверждает остановку процессов роли.
        try:
            frozen = self.call(urls[old], "POST", "/v1/assign", {**move, "wait": True},
                               timeout=FREEZE_TIMEOUT_SEC + 5)
        except wire.WireError:
            frozen = {"ok": False, "error": "no_answer"}
        if not frozen.get("ok"):
            self._restore(role, old, epoch + 1, urls)
            return {"ok": False, "error": "freeze_failed", "from": old}
        # 2. Новый забирает state старого и подтверждает запуск.
        try:
            started = self.call(urls[target], "POST", "/v1/assign", {**move, "wait": True},
                                timeout=ACTIVATE_TIMEOUT_SEC + HANDOVER_TIMEOUT_SEC + 5)
        except wire.WireError:
            started = {"ok": False, "error": "no_answer"}
        if not started.get("ok"):
            self._restore(role, old, epoch + 1, urls, also=target)
            return {"ok": False, "error": "activate_failed", "from": old}
        self._broadcast({**move}, skip={urls[old], urls[target]})
        return {"ok": True, "from": old}

    def _restore(self, role: str, old: str, epoch: int, urls: dict, also: str = "") -> None:
        """Откат: роль снова у старого держателя, без передачи state."""
        back = {"role": role, "holder": old, "epoch": epoch, "prev": also or old, "carry": False}
        if also:
            try:
                self.call(urls[also], "POST", "/v1/assign", {**back, "wait": True},
                          timeout=FREEZE_TIMEOUT_SEC + 5)
            except wire.WireError:
                pass
        try:
            self.call(urls[old], "POST", "/v1/assign", {**back, "wait": True},
                      timeout=ACTIVATE_TIMEOUT_SEC + 5)
        except wire.WireError:
            pass
        self._broadcast(back, skip={urls[old], urls.get(also, "")})

    def _broadcast(self, item: dict, skip: set[str]) -> None:
        targets = [url for _, _, url in self.agents() if url not in skip]

        def send(url):
            try:
                self.call(url, "POST", "/v1/assign", item)
            except wire.WireError:
                pass  # догонит сверкой раз в check_sec

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(targets), 1)) as pool:
            list(pool.map(send, targets))

    # --- HTTP ------------------------------------------------------------------
    def _serve(self) -> None:
        """Сеть — только подписанные запросы агентов; панель бота — отдельный loopback-порт."""
        parts = urllib.parse.urlsplit(self.own_url())
        host = parts.hostname or "0.0.0.0"
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host = "0.0.0.0"  # имя хоста: слушаем все адреса, доступ — по подписи
        self.server = self._listen((host, parts.port), _peer_handler(self), wire.server_context(self.cfg))
        if self.role == "bot":
            # Панель — только процессу бота этого узла: loopback, ключ на запуск супервизора.
            self.panel_server = self._listen(("127.0.0.1", 0), _panel_handler(self), None)

    def _listen(self, address, handler, context) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(address, handler)
        server.daemon_threads = True
        if context is not None:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, name=f"failover-http-{self.role}", daemon=True)
        thread.start()
        self._threads.append(thread)
        return server


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    agent: Agent

    def log_message(self, *_args) -> None:
        pass

    def send(self, code: int, payload, content_type: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        return self.rfile.read(length) if 0 <= length <= MAX_BODY else None

    def json_body(self) -> tuple[bytes, dict] | None:
        body = self.body()
        if body is None:
            return None
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return None
        return (body, payload) if isinstance(payload, dict) else None


def _peer_handler(agent: Agent):
    class Peer(_Handler):
        def signed(self, body: bytes) -> bool:
            return agent.verifier.check(self.command, self.path, self.headers.get(wire.TIME_HEADER),
                                        self.headers.get(wire.SIGN_HEADER), body)

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if not self.signed(b""):
                return self.send(403, {"ok": False})
            if path == "/v1/status":
                return self.send(200, agent.status())
            if path == "/v1/handover":
                try:
                    data = agent.handover_data(dict(urllib.parse.parse_qsl(query)).get("role", ""))
                except handover.HandoverError as exc:
                    return self.send(422, {"ok": False, "error": str(exc)})
                if data is None:
                    return self.send(409, {"ok": False, "error": "role_running"})
                return self.send(200, data, "application/gzip")
            return self.send(404, {"ok": False})

        def do_POST(self) -> None:
            parsed = self.json_body()
            if parsed is None:
                return self.send(400, {"ok": False})
            body, payload = parsed
            if not self.signed(body):
                return self.send(403, {"ok": False})
            if self.path == "/v1/assign":
                return self.send(200, agent.handle_assign(payload))
            return self.send(404, {"ok": False})

    return Peer


def _panel_handler(agent: Agent):
    class Panel(_Handler):
        def allowed(self) -> bool:
            header = self.headers.get("Authorization") or ""
            return secrets.compare_digest(header.encode(), f"Bearer {agent.panel_token}".encode())

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if not self.allowed():
                return self.send(403, {"ok": False})
            if path == "/v1/panel":
                return self.send(200, panel.view(agent, dict(urllib.parse.parse_qsl(query)).get("lang")))
            return self.send(404, {"ok": False})

        def do_POST(self) -> None:
            parsed = self.json_body()
            if parsed is None:
                return self.send(400, {"ok": False})
            if not self.allowed():
                return self.send(403, {"ok": False})
            if self.path == "/v1/panel":
                payload = parsed[1]
                return self.send(200, panel.press(agent, str(payload.get("press") or ""), payload.get("lang")))
            return self.send(404, {"ok": False})

    return Panel
