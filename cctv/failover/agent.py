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

Конфиг (share.py): держатель роли раздаёт ревизии каталога конфига и настройки
роли из чата остальным агентам — сразу после правки и сверкой раз в check_sec.

Автопереключение (auto.py, ``auto = true``): агенты роли раз в heartbeat_sec
слышат друг друга; держатель без кворума замирает сам, молчащего держателя
сменяет следующий по приоритету узел при кворуме. Держатель раздаёт запасным
копию своего state раз в check_sec — на ней роль и стартует после отказа.
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
from . import auto, handover, panel, share, wire
from .config import ROLES, Failover

STATE_FILE = "failover.json"
STARTUP_TIMEOUT_SEC = 3.0
PEER_TIMEOUT_SEC = 3.0
# Замирание: супервизор гасит процессы роли (до 15 с на процесс), активация —
# prepare (getMe у бота) и запуск процессов.
FREEZE_TIMEOUT_SEC = 60.0
ACTIVATE_TIMEOUT_SEC = 90.0
HANDOVER_TIMEOUT_SEC = 60.0
# Как часто держатель роли смотрит, не правили ли его каталог конфига и настройки роли.
SHARE_TICK_SEC = 2.0
# Пауза перед переключением: бот успевает показать «переключаю» до того,
# как его самого остановят.
SWITCH_DELAY_SEC = 3.0
MAX_BODY = 2 * share.MAX_BYTES + 64 * 1024   # ревизия конфига — в JSON с экранированием


def _assignment(holder: str, epoch: int, prev: str = "", carry: bool = False, auto: bool = False) -> dict:
    """Назначение роли; auto — роль забрал узел сам, когда держатель замолчал (auto.py)."""
    return {"holder": holder, "epoch": int(epoch), "prev": prev or holder, "carry": bool(carry),
            "auto": bool(auto)}


class Agent:
    def __init__(self, cfg: Failover, role: str, state_dir: pathlib.Path, probe=None, *,
                 log=print, clock=time.time, local_dir: pathlib.Path | None = None) -> None:
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
        # Раздаваемый конфиг: локальный каталог узла (источник правок на держателе роли),
        # своя копия в state и последняя разосланная версия настроек роли.
        self.local_dir = pathlib.Path(local_dir) if local_dir is not None else None
        self.store = share.Store(self.state_dir)
        self.disk_lock = threading.RLock()
        self.role_sent = ""
        # Автопереключение: когда (по монотонным часам) слышали агента роли на каждом
        # узле и его последний статус; отступление после конфликта опроса Telegram.
        self.started_mono = time.monotonic()
        self.heard: dict[str, float] = {}
        self.views: dict[str, dict | None] = {}
        # Копия state для запасных: держатель собирает её раз в check_sec, запасной
        # помнит, какую уже разложил у себя.
        self.snapshot: tuple[str, bytes] | None = None
        self.snapshot_applied = ""

    # --- что видит супервизор: «включись / замри» ------------------------------
    def active(self) -> bool:
        with self.lock:
            if self.assign[self.role]["holder"] != self.cfg.me:
                return False
            if not self.cfg.auto:
                return True
            # Автопереключение: держатель работает, только пока слышит кворум.
            return auto.has_quorum(self._ages(), self.cfg.fence_sec, self.cfg.quorum)

    def conflict(self) -> bool:
        """Супервизор: процесс бота вышел — опрос Telegram перехватил другой процесс.

        True — этот узел держит роль и кворум это подтверждает: перехватил устаревший
        процесс (его узел сам замрёт), опрос возобновится после паузы перезапуска.
        False — узел роль не держит или кворума не слышит: роль здесь замирает.
        """
        holding = self.active()
        if holding:
            self.log(f"отказоустойчивость: опрос Telegram перехватил другой процесс, но роль {self.role} "
                     "за этим узлом по кворуму — возобновлю опрос после паузы")
        else:
            self.log(f"отказоустойчивость: опрос Telegram у другого процесса, а роль {self.role} этому узлу "
                     "кворумом не подтверждена — замираю")
        return holding

    def _ages(self) -> dict[str, float]:
        return auto.ages(self.heard, self.started_mono, time.monotonic(),
                         [node.name for node in self.cfg.nodes], self.cfg.me)

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
        """Адреса для процессов роли: мост для бота, приёмник событий для движка.

        Движку — ещё и адрес его собственного моста (bridge_url своего узла) как
        CCTV_PUBLIC_URL: по нему строятся ссылки на медиа в событиях, а бот берёт медиа
        только с того адреса моста, который знает. Без этого ссылка вела на bind
        (0.0.0.0) и кадр с моста другого узла бот отвергал.
        """
        other = "bot" if self.role == "engine" else "engine"
        node = self.cfg.node(self.holder(other))
        result: dict[str, str] = {}
        if self.role == "engine":
            own = self.cfg.node(self.cfg.me)
            if own is not None and own.bridge_url:
                result["CCTV_PUBLIC_URL"] = own.bridge_url
        if node is None:
            return result
        if self.role == "bot" and node.bridge_url:
            result["CCTV_BRIDGE_URL"] = node.bridge_url
        if self.role == "engine" and node.events_url:
            result["CCTV_EVENTS_URL"] = node.events_url
        return result

    def panel_env(self) -> dict[str, str]:
        """Боту — адрес панели узлов и одноразовый (на запуск супервизора) ключ к ней.

        Обеим ролям — CCTV_ORIGIN, имя своего узла как строка подписи: движок ставит
        её в события (откуда кадр и клип), бот — в подписи и карточки (кто отвечает).
        Процессам это только текст для человека, а не знание об узлах и соседях.
        """
        result = {"CCTV_ORIGIN": self.cfg.me}
        if self.role == "bot" and self.cfg.auto:
            # Telegram — арбитр: перехватили опрос — процесс бота выходит, а не борется за него.
            result["CCTV_POLL_CONFLICT"] = "exit"
        if self.panel_server is None:
            return result
        port = self.panel_server.server_address[1]
        return {**result, "CCTV_NODES_URL": f"http://127.0.0.1:{port}", "CCTV_NODES_TOKEN": self.panel_token}

    # --- жизненный цикл --------------------------------------------------------
    def start(self) -> None:
        self._serve()
        self.gossip(timeout=STARTUP_TIMEOUT_SEC)
        self.share_tick()
        thread = threading.Thread(target=self._loop, name=f"failover-{self.role}", daemon=True)
        thread.start()
        self._threads.append(thread)
        if self.cfg.auto:
            # Копия state — своим потоком: её загрузка у замершего держателя длится до
            # HANDOVER_TIMEOUT_SEC и не должна задерживать сердцебиение (кворум бы «пропал»).
            for target, name in ((self._beat_loop, "beat"), (self._snapshot_loop, "snapshot")):
                thread = threading.Thread(target=target, name=f"failover-{name}-{self.role}", daemon=True)
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
        next_check = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + self.cfg.check_sec
                self.check()
            try:
                self.share_tick()
            except Exception as exc:  # раздача не должна ронять агента
                self.log(f"отказоустойчивость: раздача конфига: {type(exc).__name__}: {exc}")
            self._stop.wait(min(SHARE_TICK_SEC, self.cfg.check_sec))

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

    # --- автопереключение (auto.py) --------------------------------------------
    def _beat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.beat()
            except Exception as exc:  # сердцебиение не должно ронять агента
                self.log(f"отказоустойчивость: сердцебиение: {type(exc).__name__}: {exc}")
            self._stop.wait(self.cfg.heartbeat_sec)

    def _snapshot_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot_tick()
            except Exception as exc:  # копия state не должна ронять агента
                self.log(f"отказоустойчивость: копия state: {type(exc).__name__}: {exc}")
            self._stop.wait(self.cfg.check_sec)

    def beat(self) -> None:
        """Сердцебиение: статусы агентов своей роли, затем решение по держателю."""
        targets = [(node.name, node.agent(self.role)) for node in self.cfg.ordered]

        def fetch(target):
            try:
                return self.call(target[1], "GET", "/v1/status", timeout=min(PEER_TIMEOUT_SEC, self.cfg.heartbeat_sec))
            except wire.WireError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
            found = dict(zip((name for name, _ in targets), pool.map(fetch, targets)))
        for result in found.values():
            for role in ROLES:
                item = (result or {}).get("assign", {}).get(role)
                if isinstance(item, dict):
                    self.adopt(role, item)
        self._hear(found)
        self.auto_step()

    def auto_step(self) -> None:
        """Забрать роль у молчащего держателя, если кворум согласен и этот узел — следующий."""
        with self.lock:
            current = dict(self.assign[self.role])
            own = self._ages()
            fresh = set(auto.reachable(own, self.cfg.fence_sec))
            views = {name: view for name, view in self.views.items() if name in fresh}
            fit = {name: (view or {}).get("health", {}).get("fit") is not False for name, view in views.items()}
            fit[self.cfg.me] = self.health.get("fit") is not False
            elsewhere = any((view or {}).get("running") for name, view in views.items() if name != self.cfg.me)
        holder, epoch = current["holder"], current["epoch"]
        if not auto.takeover(self.cfg, holder, own, views, fit, elsewhere):
            return
        item = _assignment(self.cfg.me, epoch + 1, holder, carry=False, auto=True)
        with self.changed:
            if self.assign[self.role] != current:
                return   # назначение сменилось, пока решали
            self.assign[self.role] = item
            self._save()
            self.changed.notify_all()
            self.last_switch = {"role": self.role, "to": self.cfg.me, "from": holder, "at": self.clock(),
                                "epoch": epoch + 1, "ok": True, "auto": True}
        self.log(f"отказоустойчивость: держатель роли {self.role} {holder} молчит "
                 f"{own.get(holder, 0):.0f} с, кворум {len(fresh)} из {len(self.cfg.nodes)} — роль забирает "
                 f"этот узел (эпоха {epoch + 1})")
        # Рассылка — своим потоком: ответа замершего прежнего держателя ждать до
        # PEER_TIMEOUT_SEC, а сердцебиение за это время не должно «потерять» кворум.
        threading.Thread(target=self._broadcast, args=({"role": self.role, **item}, {self.own_url()}),
                         name=f"failover-assign-{self.role}", daemon=True).start()

    def snapshot_tick(self) -> None:
        """Копия state роли: держатель собирает её, запасной забирает новую себе."""
        if self.active() and self.running:
            try:
                data, sha = handover.snapshot(self.state_dir)
            except handover.HandoverError as exc:
                self.log(f"отказоустойчивость: копия state роли {self.role} не собрана ({exc})")
                return
            with self.lock:
                self.snapshot = (sha, data)
            return
        holder = self.holder()
        with self.lock:
            view = self.views.get(holder) if holder != self.cfg.me else None
            fresh = holder in auto.reachable(self._ages(), self.cfg.fence_sec)
        sha = (view or {}).get("snapshot") or ""
        if not fresh or not sha or sha == self.snapshot_applied:
            return
        node = self.cfg.node(holder)
        try:
            data = self.client.call(node.agent(self.role), "GET", f"/v1/snapshot?role={self.role}",
                                    timeout=HANDOVER_TIMEOUT_SEC, raw=True)
        except wire.WireError:
            return   # следующая попытка через check_sec
        with self.disk_lock:
            if self.running or self.holder() == self.cfg.me:
                return
            try:
                changed = handover.apply(data, self.state_dir)
            except handover.HandoverError as exc:
                self.log(f"отказоустойчивость: копия state роли {self.role} с узла {holder} не разобрана ({exc})")
                return
        self.snapshot_applied = sha
        if changed:
            self.log(f"отказоустойчивость: копия state роли {self.role} с узла {holder} (файлов изменилось {changed})")

    def snapshot_data(self, role: str) -> bytes | None:
        with self.lock:
            if role != self.role or self.snapshot is None or not self.running:
                return None
            return self.snapshot[1]

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
                                           bool(item.get("carry")), bool(item.get("auto")))
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
                               bool(item.get("carry")), bool(item.get("auto")))
        except (KeyError, TypeError, ValueError):
            return False
        if role not in ROLES or self.cfg.node(item["holder"]) is None:
            return False
        with self.lock:
            if not self._newer(item, self.assign[role]):
                return False
            taking = (role == self.role and item["holder"] == self.cfg.me
                      and self.assign[role]["holder"] != self.cfg.me)
        if taking and item["carry"] and item["prev"] != self.cfg.me:
            with self.disk_lock:
                if not self._take_state(item["prev"]):
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

    def _hear(self, found: dict[str, dict | None]) -> None:
        """Ответившие агенты своей роли — услышаны сейчас (сердцебиение и сверка)."""
        now = time.monotonic()
        with self.lock:
            for node, result in found.items():
                if result is not None and node != self.cfg.me:
                    self.heard[node] = now
                    self.views[node] = result

    def gossip(self, timeout: float = PEER_TIMEOUT_SEC) -> dict:
        found = self.statuses(timeout)
        for result in found.values():
            for role in ROLES:
                item = (result or {}).get("assign", {}).get(role)
                if isinstance(item, dict):
                    self.adopt(role, item)
        # Сначала назначения, потом «услышан»: кворум не должен вернуться раньше новой эпохи.
        self._hear({node: result for (node, role), result in found.items() if role == self.role})
        try:
            self.catch_up(found, timeout)
        except Exception as exc:  # сверка не должна ронять агента
            self.log(f"отказоустойчивость: сверка конфига: {type(exc).__name__}: {exc}")
        return found

    # --- раздаваемый конфиг (share.py) -----------------------------------------
    def share_tick(self) -> None:
        """Держатель роли: правку локального каталога — новой ревизией, настройки роли — соседям."""
        if self.local_dir is not None:
            self._publish_local()
        if share.ROLE_FILES.get(self.role) and self.active() and self.running:
            state = share.role_state(self.state_dir, self.role)
            if state["sha"] != self.role_sent:
                self.role_sent = state["sha"]
                self._send_all(self._role_peers(), "/v1/role-state", self._role_payload(state))

    def _publish_local(self) -> None:
        """Правка локального каталога узла — новой ревизией, если её можно раздать.

        Раздаётся только правка на держателе роли и только поверх текущей ревизии:
        каталог узла до правки совпадал с раздаваемым. Иначе (каталог запасного узла
        неполон или устарел) правка перетёрла бы настоящий конфиг — журнал объясняет,
        что сделать. Пока ревизий нет ни у кого, первую выпускает держатель роли.
        """
        files = share.read_files(self.local_dir, share.FILES)
        sha = share.digest(files)
        with self.disk_lock:
            seen = self.store.seen()
            if sha == seen:
                return
            self.store.mark_seen(sha)
            meta = self.store.meta()
            if sha == meta["sha"]:
                return
            if not self.active():
                if seen is not None:
                    self.log("отказоустойчивость: каталог конфига правили на узле наготове — "
                             "правка не раздаётся; правьте его на узле, который держит роль")
                return
            if meta["rev"] and seen != meta["sha"]:
                if seen is not None:
                    self.log(f"отказоустойчивость: каталог конфига этого узла не совпадал с раздаваемым "
                             f"(ревизия {meta['rev']}) — правка не раздаётся; сначала возьмите раздаваемые "
                             f"файлы из {self.store.root} (docs/failover.md)")
                return
            bundle = {"rev": meta["rev"] + 1, "origin": self.cfg.me, "sha": sha, "files": files}
            try:
                self.adopt_config(bundle)
            except share.ShareError as exc:
                self.log(f"отказоустойчивость: каталог конфига не раздаётся — на нём узел не запустится ({exc})")
                return
        self._send_all([url for _, _, url in self.agents() if url != self.own_url()], "/v1/config", bundle)

    def adopt_config(self, bundle: dict) -> bool:
        """Принять ревизию каталога конфига, если она новее своей. ShareError — не годится узлу."""
        with self.disk_lock:
            if not share.newer(bundle, self.store.meta(), self.cfg.rank):
                return False
            files = share.check_bundle(bundle, self.cfg.me, self.role)
            self.store.save(int(bundle["rev"]), str(bundle["origin"]), files)
        self.log(f"отказоустойчивость: конфиг — ревизия {bundle['rev']} с узла {bundle['origin']}")
        return True

    def receive_config(self, bundle: dict) -> dict:
        try:
            changed = self.adopt_config(bundle)
        except (share.ShareError, KeyError, TypeError, ValueError) as exc:
            self.log(f"отказоустойчивость: присланный конфиг не принят ({exc})")
            return {"ok": False, "error": "rejected", "config": self.store.meta()}
        return {"ok": True, "changed": changed, "config": self.store.meta()}

    def _role_peers(self) -> list[str]:
        return [node.agent(self.role) for node in self.cfg.ordered if node.name != self.cfg.me]

    def _role_payload(self, state: dict) -> dict:
        with self.lock:
            epoch = self.assign[self.role]["epoch"]
        return {"role": self.role, "holder": self.cfg.me, "epoch": epoch, **state}

    def role_state_data(self, role: str) -> dict | None:
        """Настройки роли для узла наготове — только с узла, где роль работает."""
        if role != self.role or not share.ROLE_FILES.get(role) or not self.active():
            return None
        return self._role_payload(share.role_state(self.state_dir, role))

    def receive_role_state(self, payload: dict) -> dict:
        """Настройки роли от её держателя; пишутся, только пока роль здесь не запущена."""
        if payload.get("role") != self.role or not share.ROLE_FILES.get(self.role):
            return {"ok": False, "error": "bad_role"}
        files = payload.get("files")
        if not isinstance(files, dict) or share.digest(files) != payload.get("sha"):
            return {"ok": False, "error": "rejected"}
        with self.disk_lock:
            with self.lock:
                holder = self.assign[self.role]["holder"]
                if payload.get("holder") != holder or holder == self.cfg.me or self.running:
                    return {"ok": False, "error": "stale", "holder": holder}
            if share.role_state(self.state_dir, self.role)["sha"] == payload["sha"]:
                return {"ok": True, "changed": False}
            try:
                share.save_role_state(self.state_dir, self.role, files)
            except share.ShareError as exc:
                self.log(f"отказоустойчивость: настройки роли {self.role} не приняты ({exc})")
                return {"ok": False, "error": "rejected"}
        self.log(f"отказоустойчивость: настройки роли {self.role} приняты с узла {holder}")
        return {"ok": True, "changed": True}

    def catch_up(self, found: dict, timeout: float = PEER_TIMEOUT_SEC) -> None:
        """Пропустил рассылку — догоняем по статусам соседей: конфиг и настройки роли."""
        urls = {(node, role): url for node, role, url in self.agents()}
        best, source = self.store.meta(), None
        for key, result in found.items():
            item = (result or {}).get("config")
            if isinstance(item, dict) and share.newer(item, best, self.cfg.rank):
                best, source = item, urls[key]
        if source is not None:
            try:
                self.receive_config(self.client.call(source, "GET", "/v1/config", timeout=timeout))
            except wire.WireError:
                pass
        holder = self.holder()
        view = found.get((holder, self.role)) or {}
        if (share.ROLE_FILES.get(self.role) and holder != self.cfg.me and not self.running
                and view.get("running") and view.get("role_state")
                and view["role_state"] != share.role_state(self.state_dir, self.role)["sha"]):
            try:
                self.receive_role_state(self.client.call(urls[(holder, self.role)], "GET",
                                                         f"/v1/role-state?role={self.role}", timeout=timeout))
            except wire.WireError:
                pass

    def _send_all(self, targets: list[str], path: str, payload: dict) -> None:
        def send(url):
            try:
                self.client.call(url, "POST", path, payload)
            except wire.WireError:
                pass  # догонит сверкой раз в check_sec

        if targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
                list(pool.map(send, targets))

    def status(self) -> dict:
        with self.lock:
            return {"node": self.cfg.me, "role": self.role, "version": __version__,
                    "assign": json.loads(json.dumps(self.assign)), "running": self.running,
                    "active": self.assign[self.role]["holder"] == self.cfg.me,
                    "health": dict(self.health), "now": self.clock(), "config": self.store.meta(),
                    "auto": self.cfg.auto, "heard": self._ages() if self.cfg.auto else {},
                    "fenced": (self.cfg.auto and self.assign[self.role]["holder"] == self.cfg.me
                               and not self.active()),
                    "snapshot": self.snapshot[0] if self.snapshot and self.running else "",
                    "role_state": (share.role_state(self.state_dir, self.role)["sha"]
                                   if share.ROLE_FILES.get(self.role) else "")}

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
            if path == "/v1/config":
                return self.send(200, agent.store.bundle())
            if path == "/v1/snapshot":
                data = agent.snapshot_data(dict(urllib.parse.parse_qsl(query)).get("role", ""))
                if data is None:
                    return self.send(409, {"ok": False, "error": "no_snapshot"})
                return self.send(200, data, "application/gzip")
            if path == "/v1/role-state":
                payload = agent.role_state_data(dict(urllib.parse.parse_qsl(query)).get("role", ""))
                if payload is None:
                    return self.send(409, {"ok": False, "error": "not_holder"})
                return self.send(200, payload)
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
            if self.path == "/v1/config":
                return self.send(200, agent.receive_config(payload))
            if self.path == "/v1/role-state":
                return self.send(200, agent.receive_role_state(payload))
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
