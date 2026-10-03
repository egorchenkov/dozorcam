"""Процесс №1 контейнера: супервизор роли (engine | bot) и её healthcheck.

В контейнере нет systemd, поэтому то, что на хосте делали юниты, делает он:

* engine — RTSP-прокси, затем мост и конвейер (порядок и runtime-файл прокси,
  как Requires=/After= юнитов); упавший процесс перезапускается с нарастающей
  паузой, а не в тугом цикле; перед (пере)запуском моста и конвейера —
  retention-guard check (бывший ExecStartPre); правка файлов конфиг-каталога —
  перезапуск всей цепочки (в контейнере так работает «применить конфиг»).
* bot — сам бот; до запуска конфиг проверяется целиком, а токен — запросом getMe.

Пустой конфиг — не авария. Без cameras.json движок стартует с пустым реестром
(мост отвечает, бот покажет «камер нет»); без токена или с отклонённым токеном
бот не запускается, а ждёт правки конфига: в журнал — одна понятная строка
«нет конфига: …», healthcheck — unhealthy с той же причиной. Процесс при этом
жив, поэтому контейнер не уходит в цикл рестартов.

Состояние супервизора — JSON в CCTV_SUPERVISOR_STATUS (/run/cctv, tmpfs);
``python -m cctv.container health`` читает его и проверяет живость роли.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import settings

STATUS_PATH = pathlib.Path(os.environ.get("CCTV_SUPERVISOR_STATUS", "/run/cctv/supervisor.json"))
RUN_DIR = STATUS_PATH.parent
TICK_SEC = 2.0
CONFIG_POLL_SEC = 10.0
STATUS_STALE_SEC = 30.0
PROXY_READY_SEC = 15.0
BACKOFF_START_SEC = 5.0
BACKOFF_MAX_SEC = 300.0
# Процесс, проживший столько, считается здоровым: следующая пауза снова короткая.
BACKOFF_RESET_SEC = 600.0
STOP_GRACE_SEC = 15.0
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
TELEGRAM_API = os.environ.get("CCTV_TELEGRAM_API", "https://api.telegram.org")


def log(message: str) -> None:
    print(f"[supervisor] {message}", flush=True)


def write_status(state: str, detail: str = "", **extra) -> None:
    payload = {"state": state, "detail": detail, "updated_at": time.time(), "pid": os.getpid(), **extra}
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(STATUS_PATH)


def config_fingerprint(env, extra: tuple[pathlib.Path, ...] = ()) -> tuple:
    """mtime/размер файлов конфиг-каталога: изменился — цепочку перезапускаем."""
    root = settings.config_dir(env)
    result = []
    for path in (*(root / name for name in (settings.CONFIG_FILE, settings.SECRETS_FILE,
                                             settings.CAMERAS_FILE)), *extra):
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


def registry_file(env) -> pathlib.Path:
    """Реестр, который ведёт бот (через мост): в state, а не в каталоге конфига.

    Каталог конфига в контейнере смонтирован только на чтение, а камеру заводят
    из чата. Поэтому после первой правки реестр живёт в томе state движка, а
    cameras.json каталога конфига остаётся исходником (seed) для пустого state.
    """
    return settings.state_dir(env) / settings.CAMERAS_FILE


class Child:
    """Один процесс роли с нарастающей паузой между перезапусками."""

    def __init__(self, name: str, argv: list[str]) -> None:
        self.name, self.argv = name, argv
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.backoff = BACKOFF_START_SEC
        self.next_start = 0.0

    def start(self, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(self.argv, env=env)
        self.started_at = time.monotonic()
        log(f"{self.name}: запущен (pid {self.proc.pid})")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def reap(self) -> int | None:
        """Код выхода, если процесс только что завершился; пауза до следующего старта."""
        if self.proc is None or self.proc.poll() is None:
            return None
        code = self.proc.returncode
        lived = time.monotonic() - self.started_at
        if lived >= BACKOFF_RESET_SEC:
            self.backoff = BACKOFF_START_SEC
        self.next_start = time.monotonic() + self.backoff
        log(f"{self.name}: завершился с кодом {code} через {lived:.0f} с; "
            f"перезапуск через {self.backoff:.0f} с")
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_SEC)
        self.proc = None
        return code

    def stop(self) -> None:
        if not self.alive():
            self.proc = None
            return
        self.proc.terminate()
        try:
            self.proc.wait(STOP_GRACE_SEC)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        log(f"{self.name}: остановлен")
        self.proc = None


class Supervisor:
    role = ""

    def __init__(self) -> None:
        # Пустая CCTV_* (compose подставляет "" для незаполненной строки .env) — то же,
        # что не заданная: иначе она перекрыла бы значение из config.toml.
        self.base_env = {key: value for key, value in os.environ.items()
                         if value or not key.startswith("CCTV_")}
        self.children: list[Child] = []
        self.stopping = False
        self.last_problem = ""

    # --- общее ---------------------------------------------------------
    def on_signal(self, signum, _frame) -> None:
        log(f"сигнал {signal.Signals(signum).name}: останавливаю роль {self.role}")
        self.stopping = True

    def stop_all(self) -> None:
        for child in reversed(self.children):
            child.stop()

    def problem(self, state: str, message: str) -> None:
        """Понятная причина простоя: в журнал один раз, в статус — каждый тик."""
        if message != self.last_problem:
            log(f"{'нет конфига' if state == 'no_config' else state}: {message}")
            self.last_problem = message
        write_status(state, message, role=self.role)

    def fingerprint(self) -> tuple:
        return config_fingerprint(self.base_env)

    def wait_config_change(self, fingerprint: tuple, state: str, message: str) -> None:
        while not self.stopping and self.fingerprint() == fingerprint:
            self.problem(state, message)
            self.sleep(CONFIG_POLL_SEC)

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(TICK_SEC, max(deadline - time.monotonic(), 0)))

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        while not self.stopping:
            fingerprint = self.fingerprint()
            prepared = self.prepare()
            if isinstance(prepared, tuple):
                self.wait_config_change(fingerprint, *prepared)
                continue
            self.last_problem = ""
            self.supervise(prepared, fingerprint)
            self.stop_all()
        write_status("stopped", role=self.role)
        return 0

    def supervise(self, env: dict[str, str], fingerprint: tuple) -> None:
        next_config_check = time.monotonic() + CONFIG_POLL_SEC
        for child in self.children:
            child.next_start = 0.0
        while not self.stopping:
            for child in self.children:
                child.reap()
            for child in self.children:
                if not child.alive() and time.monotonic() >= child.next_start:
                    if not self.before_start(child, env):
                        break
                    child.start(env)
            write_status("running", "", role=self.role,
                         children={c.name: c.alive() for c in self.children})
            if time.monotonic() >= next_config_check:
                next_config_check = time.monotonic() + CONFIG_POLL_SEC
                if self.fingerprint() != fingerprint:
                    log("конфиг-каталог или реестр изменился: перезапуск роли")
                    return
            time.sleep(TICK_SEC)

    # --- переопределяется ролями ---------------------------------------
    def prepare(self):
        """Окружение детей, либо (state, сообщение) — запускать нечего, ждать конфиг."""
        raise NotImplementedError

    def before_start(self, child: Child, env: dict[str, str]) -> bool:
        return True


class EngineSupervisor(Supervisor):
    role = "engine"
    proxy_config = RUN_DIR / "cameras.proxy.json"
    empty_config = RUN_DIR / "cameras.empty.json"

    def __init__(self) -> None:
        super().__init__()
        python = sys.executable
        self.children = [Child("rtsp-proxy", [python, "-m", "cctv", "rtsp-proxy"]),
                         Child("bridge", [python, "-m", "cctv", "bridge"]),
                         Child("pipeline", [python, "-m", "cctv", "pipeline"])]
        self.guard = self.base_env.get("CCTV_RETENTION_GUARD", "")

    def fingerprint(self) -> tuple:
        return config_fingerprint(self.base_env, (registry_file(self.base_env),))

    def prepare(self):
        env = dict(self.base_env)
        try:
            resolved = settings.apply("engine", dict(env))
        except settings.SettingsError as exc:
            return "no_config", str(exc)
        seed = pathlib.Path(resolved["CCTV_CAMERA_CONFIG"])
        managed = registry_file(resolved)
        # Мост пишет реестр сам (без root-сокета писаря) — в файл state.
        env["CCTV_REGISTRY_FILE"] = str(managed)
        env["CCTV_REGISTRY_SEED"] = str(seed)
        cameras = managed if managed.is_file() else seed
        env["CCTV_CAMERA_CONFIG"] = str(cameras)
        try:
            data = json.loads(cameras.read_text())
            if not isinstance(data.get("cameras", []), list):
                raise ValueError("ключ cameras должен быть списком")
            count = len(data.get("cameras", []))
        except FileNotFoundError:
            self.empty_config.write_text('{"cameras": []}\n')
            env["CCTV_CAMERA_CONFIG"] = str(self.empty_config)
            log(f"нет конфига: {cameras} не найден — движок запущен без камер "
                "(положите cameras.json в каталог конфига, цепочка перезапустится сама)")
            count = 0
        except (OSError, ValueError, AttributeError) as exc:
            return "no_config", f"{cameras}: {exc}"
        else:
            log(f"реестр {cameras}: камер {count}")
        env["CCTV_PROXY_CAMERA_CONFIG"] = str(self.proxy_config)
        env["CCTV_RUNTIME_CAMERA_CONFIG"] = str(self.proxy_config)
        self.proxy_config.unlink(missing_ok=True)
        return env

    def before_start(self, child: Child, env: dict[str, str]) -> bool:
        if child.name == "rtsp-proxy":
            return True
        # Мост и конвейер читают runtime-реестр прокси: без него стартовать рано.
        deadline = time.monotonic() + PROXY_READY_SEC
        while not self.proxy_config.exists():
            if self.stopping or time.monotonic() >= deadline or not self.children[0].alive():
                log(f"{child.name}: runtime-реестр прокси не появился, жду")
                child.next_start = time.monotonic() + BACKOFF_START_SEC
                return False
            time.sleep(0.2)
        if self.guard and os.access(self.guard, os.X_OK):
            check = subprocess.run([self.guard, "check"], env=env, capture_output=True, text=True)
            if check.returncode != 0:
                reason = (check.stderr or check.stdout).strip() or f"код {check.returncode}"
                log(f"{child.name}: retention-guard check не прошёл: {reason}")
                child.next_start = time.monotonic() + child.backoff
                child.backoff = min(child.backoff * 2, BACKOFF_MAX_SEC)
                return False
        return True


class BotSupervisor(Supervisor):
    role = "bot"

    def __init__(self) -> None:
        super().__init__()
        self.children = [Child("bot", [sys.executable, "-m", "cctv", "bot"])]

    def prepare(self):
        from .bot import config as bot_config

        env = dict(self.base_env)
        try:
            resolved = settings.apply("bot", dict(env))
            cfg = bot_config.load(resolved)
        except (settings.SettingsError, bot_config.ConfigError, ValueError) as exc:
            return "no_config", str(exc)
        if not TOKEN_RE.match(cfg.bot_token):
            return "no_config", "CCTV_BOT_TOKEN не похож на токен Bot API (ожидается <id>:<ключ>)"
        rejected = token_rejected(cfg.bot_token)
        if rejected:
            return "no_config", rejected
        return env


def token_rejected(token: str) -> str:
    """Причина, если Telegram отверг токен; сетевой сбой — не повод не запускаться."""
    try:
        with urllib.request.urlopen(f"{TELEGRAM_API}/bot{token}/getMe", timeout=10):
            return ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 404):
            return f"Telegram отклонил CCTV_BOT_TOKEN (HTTP {exc.code}) — проверьте токен в .env"
        log(f"getMe: HTTP {exc.code}, запускаю бота, он повторит сам")
        return ""
    except (urllib.error.URLError, OSError) as exc:
        log(f"getMe недоступен ({exc.__class__.__name__}), запускаю бота, он повторит сам")
        return ""


def health() -> int:
    """Код 0 — роль работает; иначе причина в stdout (её покажет docker inspect)."""
    try:
        status = json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        print("супервизор ещё не записал статус")
        return 1
    age = time.time() - status.get("updated_at", 0)
    if age > STATUS_STALE_SEC:
        print(f"статус супервизора устарел на {age:.0f} с")
        return 1
    state = status.get("state")
    if state != "running":
        prefix = "нет конфига" if state == "no_config" else state
        print(f"{prefix}: {status.get('detail', '')}")
        return 1
    dead = [name for name, alive in status.get("children", {}).items() if not alive]
    if dead:
        print("не работают: " + ", ".join(dead))
        return 1
    role = status.get("role")
    if role == "engine":
        port = os.environ.get("CCTV_PORT") or str(settings.DEFAULT_BRIDGE_PORT)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/cameras", timeout=5) as reply:
                cameras = len(json.loads(reply.read()).get("cameras", []))
        except (OSError, ValueError) as exc:
            print(f"мост на :{port} не отвечает: {exc}")
            return 1
        print(f"ok: мост :{port}, камер {cameras}")
    elif role == "bot" and not settings.internal_tls():
        # С mTLS приёмник есть только при заданных сертификатах — тогда хватает живости процесса.
        port = int(os.environ.get("CCTV_EVENTS_PORT") or settings.DEFAULT_EVENTS_PORT)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=5).close()
        except OSError as exc:
            print(f"приёмник событий :{port} не слушает: {exc}")
            return 1
        print(f"ok: бот, приёмник событий :{port}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command == "engine":
        return EngineSupervisor().run()
    if command == "bot":
        return BotSupervisor().run()
    if command == "health":
        return health()
    if command in ("bridge", "pipeline", "provision", "rtsp-proxy", "notify"):
        from . import cli
        return cli.run(command, argv[1:])
    print("usage: python -m cctv.container engine|bot|health", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
