"""Отказоустойчивость, шаг 2: один конфиг на всех.

Интеграция: два узла (a — главный, b — запасной) на loopback, у каждого свой
каталог конфига (у b — только раздел [failover] и устаревшие значения), свой state
у каждой роли; агенты говорят по mTLS с CA установки. Супервизоры движка и бота —
настоящие (run), процессы ролей подменены. Проверяется: первая ревизия и правка
каталога конфига на активном доезжают до b без ручных действий; так же — настройки
из чата (реестр, пауза, пароли активации); после переключения роли b работает на
полученном конфиге, а не на своём каталоге, и своим устаревшим каталогом его не
перетирает.

Предохранители: новая настройка, которая не попала бы в раздаваемый конфиг (новый
файл каталога конфига, новый файл настроек в state, чтение окружения мимо CCTV_*,
новый узловой ключ без описания), валит тест.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

from cctv import container, settings
from cctv.failover import share

from tests.test_failover_armed import TOKEN, SECRET, FakeProc, free_port

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "cctv"


@unittest.skipUnless(shutil.which("openssl"), "нужен openssl для тестовой PKI")
class ConfigDistributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = pathlib.Path(self.tmp.name)
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.make_pki(root / "pki")
        tls = "".join(f'{key} = "{root / "pki" / name}"\n'
                      for key, name in (("tls_cert", "node.crt"), ("tls_key", "node.key"), ("tls_ca", "ca.crt")))
        nodes = "".join(
            f'[[failover.nodes]]\nname = "{name}"\npriority = {index}\n'
            f'engine_agent = "https://127.0.0.1:{free_port()}"\nbot_agent = "https://127.0.0.1:{free_port()}"\n'
            for index, name in enumerate(("a", "b"), start=1))
        self.failover = "[failover]\ncheck_sec = 5\n" + tls + nodes
        self.cfg = {"a": root / "a" / "config", "b": root / "b" / "config"}
        for path in self.cfg.values():
            path.mkdir(parents=True)
        # a — полный каталог установки; b — только связь с соседями и устаревшее значение.
        self.write_config("a", '[engine]\nhuman_gate_mode = "enforce"\n\n'
                               '[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n\n')
        self.write_secrets("a", f'[bot]\nbot_token = "{TOKEN}"\n\n')
        (self.cfg["a"] / "cameras.json").write_text(json.dumps({"cameras": [
            {"camera_id": "yard", "title": "Yard", "rtsp_url": "rtsp://viewer:pw@192.0.2.10:554/main"}]}))
        self.write_config("b", '[engine]\nhuman_gate_mode = "shadow"\n\n')
        self.write_secrets("b", "")

        self.started: list[FakeProc] = []

        class Proc(FakeProc):
            def __init__(self, argv, env=None):
                super().__init__(argv, env)
                self.env = dict(env or {})
                self.node = self.env.get("CCTV_NODE", "?")

        def popen(argv, env=None, **_kw):
            proc = Proc(argv, env)
            self.started.append(proc)
            return proc

        for patcher in (mock.patch.object(container, "STATUS_PATH", self.run_dir / "supervisor.json"),
                        mock.patch.object(container, "RUN_DIR", self.run_dir),
                        mock.patch.object(container.subprocess, "Popen", side_effect=popen),
                        mock.patch.object(container.signal, "signal"),
                        mock.patch.object(container, "TICK_SEC", 0.1),
                        mock.patch.object(container, "CONFIG_POLL_SEC", 0.5),
                        mock.patch.object(container, "write_status"),
                        mock.patch.object(container, "token_rejected", return_value=""),
                        mock.patch("cctv.failover.agent.SHARE_TICK_SEC", 0.2)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sups = {(node, cls.role): self.launch(cls, node)
                     for node in ("a", "b") for cls in (container.EngineSupervisor, container.BotSupervisor)}

    # --- обвязка -------------------------------------------------------------
    def make_pki(self, pki: pathlib.Path) -> None:
        pki.mkdir()

        def run(*args):
            subprocess.run(["openssl", *args], cwd=pki, check=True, capture_output=True)

        ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
        run("req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=dozorcam-test-ca", "-days", "1")
        (pki / "san.ext").write_text("subjectAltName=IP:127.0.0.1\n")
        run("req", *ec, "-keyout", "node.key", "-out", "node.csr", "-subj", "/CN=node")
        run("x509", "-req", "-in", "node.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "node.crt", "-days", "1", "-extfile", "san.ext")

    def write_config(self, node: str, body: str) -> None:
        (self.cfg[node] / "config.toml").write_text(body + self.failover)

    def write_secrets(self, node: str, body: str) -> None:
        path = self.cfg[node] / "secrets.toml"
        path.write_text(body + f'[failover]\nsecret = "{SECRET}"\n')
        path.chmod(0o600)

    def state(self, node: str, role: str) -> pathlib.Path:
        return self.root / node / role / "state"

    def node_env(self, node: str, role: str) -> dict:
        # У b в .env — устаревшее значение настройки (как compose подставляет умолчание)
        # и свой узловой адрес Bot API.
        return {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(self.cfg[node]), "CCTV_NODE": node,
                "CCTV_STATE_DIR": str(self.state(node, role)), "CCTV_HUMAN_GATE_MODE": "shadow",
                "CCTV_TELEGRAM_API": f"http://127.0.0.1:{9000 + ord(node)}"}

    def launch(self, cls, node: str):
        with mock.patch.dict(os.environ, self.node_env(node, cls.role), clear=True):
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

    def alive(self, node: str) -> list[FakeProc]:
        return [proc for proc in self.started
                if proc.node == node and "bot" not in proc.argv and proc.poll() is None]

    def store(self, node: str, role: str) -> share.Store:
        return share.Store(self.state(node, role))

    def resolved(self, node: str, role: str) -> dict:
        """Что роль узла получила бы при запуске: окружение узла поверх его копии конфига."""
        return settings.apply(role, share.effective_env(role, self.node_env(node, role)))

    def same_as_local(self, node: str, source: str) -> bool:
        local = share.read_files(self.cfg[source], share.FILES)
        return all(self.store(node, role).files() == local for role in ("engine", "bot"))

    # --- сценарий ----------------------------------------------------------------
    def test_active_node_distributes_config_and_new_active_runs_on_it(self) -> None:
        engine_a, engine_b = self.sups[("a", "engine")], self.sups[("b", "engine")]
        self.wait_for(lambda: len(self.alive("a")) == 3 and engine_a.layer.running, what="движок на a")
        self.wait_for(lambda: engine_b.layer is not None and not engine_b.layer.active(), what="b наготове")

        # 1. Первая ревизия: каталог a доезжает до обеих ролей b.
        self.wait_for(lambda: self.same_as_local("b", "a"), what="первая ревизия на b")
        first = self.store("b", "engine").meta()
        self.assertEqual((1, "a"), (first["rev"], first["origin"]))
        engine_env, bot_env = self.resolved("b", "engine"), self.resolved("b", "bot")
        # Раздаваемый конфиг сильнее и своего каталога b, и устаревшего .env b ...
        self.assertEqual("enforce", engine_env["CCTV_HUMAN_GATE_MODE"])
        self.assertEqual(TOKEN, bot_env["CCTV_BOT_TOKEN"])
        self.assertEqual("-100777", bot_env["CCTV_CHAT_ID"])
        # ... а узловые ключи остаются своими.
        self.assertEqual(self.node_env("b", "bot")["CCTV_TELEGRAM_API"], bot_env["CCTV_TELEGRAM_API"])
        self.assertEqual(0, len(self.alive("b")), "запасной узел запустил роль")

        # 2. Правка каталога на активном a — у b без ручных действий.
        self.write_config("a", '[common]\ntz = "Europe/Berlin"\n\n[engine]\nhuman_gate_mode = "enforce"\n\n'
                               '[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n\n')
        cameras = {"cameras": [{"camera_id": "yard", "title": "Yard", "rtsp_url": "rtsp://viewer:pw@192.0.2.10:554/main"},
                               {"camera_id": "gate", "title": "Gate", "rtsp_url": "rtsp://viewer:pw@192.0.2.11:554/main"}]}
        (self.cfg["a"] / "cameras.json").write_text(json.dumps(cameras))
        started = time.monotonic()
        self.wait_for(lambda: self.same_as_local("b", "a"), what="правка каталога на b")
        took = time.monotonic() - started
        self.assertLess(took, 5.0, "правка шла сверкой check_sec, а не рассылкой")
        self.assertEqual("Europe/Berlin", self.resolved("b", "bot")["CCTV_TZ"])
        rev = self.store("b", "engine").meta()["rev"]
        self.assertGreater(rev, first["rev"])

        # 3. Настройки из чата на a (реестр, пауза камеры, пароль активации) — у b в state.
        chat = {"cameras.json": json.dumps(cameras),
                "overrides.json": json.dumps({"gate": {"status": "paused"}}),
                "activation-vault.json": json.dumps({"192.0.2.11": {"state": "activated"}})}
        share.write_files(self.state("a", "engine"), chat)
        self.wait_for(lambda: share.read_files(self.state("b", "engine"), chat) == chat, what="настройки из чата на b")
        self.assertEqual(0, len(self.alive("b")))

        # 4. Канал — mTLS: без сертификата установки агент b не отвечает.
        anonymous = ssl.create_default_context(cafile=str(self.root / "pki" / "ca.crt"))
        anonymous.check_hostname = False
        url = engine_b.layer.own_url() + "/v1/config"
        with self.assertRaises(OSError):
            urllib.request.urlopen(url, timeout=5, context=anonymous).read()

        # 5. Роль переезжает на b — b работает на полученном конфиге, а не на своём каталоге.
        self.assertTrue(engine_a.layer.switch("engine", "b")["ok"])
        self.wait_for(lambda: len(self.alive("b")) == 3 and not self.alive("a"), what="движок на b")
        proc = self.alive("b")[0]
        self.assertEqual(str(self.store("b", "engine").root), proc.env["CCTV_CONFIG_DIR"])
        running = settings.apply("engine", dict(proc.env))
        self.assertEqual("enforce", running["CCTV_HUMAN_GATE_MODE"])
        self.assertEqual("Europe/Berlin", running["CCTV_TZ"])
        registry = json.loads(pathlib.Path(running["CCTV_CAMERA_CONFIG"]).read_text())
        self.assertEqual(["yard", "gate"], [camera["camera_id"] for camera in registry["cameras"]])
        self.assertIn('human_gate_mode = "shadow"', (self.cfg["b"] / "config.toml").read_text())
        # Свой устаревший каталог b не раздал: у a та же ревизия.
        time.sleep(1.0)
        self.assertEqual(rev, self.store("a", "engine").meta()["rev"])
        self.assertTrue(self.same_as_local("a", "a"))

        # 6. Правка устаревшего каталога b не раздаётся; поверх раздаваемых файлов — раздаётся.
        self.write_config("b", '[engine]\nhuman_gate_mode = "shadow"\n\n[bot]\nevent_merge_sec = 5\n\n')
        time.sleep(1.0)
        self.assertEqual(rev, self.store("a", "engine").meta()["rev"])
        current = self.store("b", "engine").files()
        share.write_files(self.cfg["b"], current)
        time.sleep(0.5)
        edited = current["config.toml"].replace("[bot]\n", "[bot]\nevent_merge_sec = 30\n", 1)
        (self.cfg["b"] / "config.toml").write_text(edited)
        self.wait_for(lambda: self.store("a", "engine").files()["config.toml"] == edited
                      and self.store("a", "bot").files()["config.toml"] == edited, what="правка с b на a")
        self.assertEqual((rev + 1, "b"), (self.store("a", "bot").meta()["rev"], self.store("a", "bot").meta()["origin"]))
        self.assertEqual("30", self.resolved("a", "bot")["CCTV_EVENT_MERGE_SEC"])
        # Ни разу роль движка не работала на двух узлах сразу и бот b не запускался.
        self.assertFalse([proc for proc in self.started if proc.node == "b" and "bot" in proc.argv])

    def test_sites_travel_with_the_distributed_config(self) -> None:
        """Шаг 3: площадки — ключ раздела [bot] раздаваемого config.toml, а не узла."""
        from cctv.bot import config as bot_config

        self.wait_for(lambda: self.same_as_local("b", "a"), what="первая ревизия на b")
        self.write_config("a", '[engine]\nhuman_gate_mode = "enforce"\n\n'
                               '[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n'
                               'sites = ["dacha", "city=http://127.0.0.1:18790"]\n\n')
        self.wait_for(lambda: self.same_as_local("b", "a"), what="площадки на b")
        # Устаревший список площадок в .env запасного узла не сильнее раздаваемого.
        stale = {**self.node_env("b", "bot"), "CCTV_SITES": "old=http://127.0.0.1:18799"}
        resolved = settings.apply("bot", share.effective_env("bot", stale))
        self.assertEqual("dacha city=http://127.0.0.1:18790", resolved["CCTV_SITES"])
        cfg = bot_config.load(resolved)
        self.assertEqual([("dacha", "http://127.0.0.1:8780"), ("city", "http://127.0.0.1:18790")],
                         [(site.name, site.bridge_url) for site in cfg.sites])
        self.assertNotIn("CCTV_SITES", share.NODE_LOCAL)
        self.assertEqual(0, len(self.alive("b")), "запасной узел запустил роль")


# --- предохранители -------------------------------------------------------------
STATE_NAME = re.compile(r"[\w{}.-]+\.(?:json|sqlite3|db)")
# Имена, которые сканер находит в коде движка и бота, но это не файлы state роли.
NOT_STATE = {
    "{x}.json": "media/ и events/pending/ — в хранилище буфера моста, не в state",
    "cameras.{x}.json": "копии реестра внутри registry-backups/",
    "summary-{x}.json": "сводки внутри diag/",
}


def config_file_constants(source: str) -> set[str]:
    """Файлы каталога конфига, которые знает settings.py (константы *_FILE)."""
    return set(re.findall(r'^[A-Z_]+_FILE = "([^"]+)"', source, re.M))


def state_names(paths) -> set[str]:
    """Имена файлов state в коде: строки и f-строки вида x.json, {camera}.still.json."""
    found = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(part.value if isinstance(part, ast.Constant) else "{x}" for part in node.values)
            else:
                continue
            if STATE_NAME.fullmatch(text) and not text.startswith("."):
                found.add(text)
    return found


def unclassified_state(names: set[str]) -> set[str]:
    def norm(name: str) -> str:
        return re.sub(r"\{[^}]*\}", "{x}", name)

    known = {norm(name) for role in share.ROLE_FILES for name in (*share.ROLE_FILES[role], *share.ROLE_KEPT[role])}
    return {name for name in names if norm(name) not in known | set(NOT_STATE)}


def product_code() -> list[pathlib.Path]:
    return [path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts]


class DistributedConfigGuardTest(unittest.TestCase):
    """Новая настройка обязана попасть в раздаваемый конфиг — иначе тест падает."""

    def test_every_config_file_is_distributed(self) -> None:
        source = (PACKAGE / "settings.py").read_text()
        self.assertEqual(set(share.FILES), config_file_constants(source))
        # Предохранитель срабатывает: новый файл каталога конфига вне раздачи виден.
        mutated = source + '\nZONES_FILE = "zones.toml"\n'
        self.assertEqual({"zones.toml"}, config_file_constants(mutated) - set(share.FILES))
        # И мимо констант в каталог конфига никто не ходит.
        joins = re.compile(r"config_dir\([^)]*\)\s*/\s*[\"']")
        self.assertEqual([], [str(path.relative_to(ROOT)) for path in product_code()
                              if joins.search(path.read_text())])

    def test_every_state_file_of_a_role_is_classified(self) -> None:
        code = [path for sub in ("engine", "bot") for path in (PACKAGE / sub).glob("*.py")]
        names = state_names(code)
        self.assertIn("overrides.json", names)          # сканер видит настоящие имена
        self.assertEqual(set(), unclassified_state(names),
                         "новый файл в state роли: раздавать (share.ROLE_FILES) или оставить (share.ROLE_KEPT)?")
        self.assertEqual({"zones.json"}, unclassified_state(names | {"zones.json"}))

    def test_settings_come_only_through_cctv_variables(self) -> None:
        """Настройка — только CCTV_*: их раздаваемый config.toml задаёт все до одной."""
        reads = re.compile(r"(?:environ\.get|getenv|environ\[|env\.get|env\[)\(?\s*[\"']([A-Za-z][A-Za-z0-9_]*)[\"']")
        foreign = {(str(path.relative_to(ROOT)), name) for path in product_code()
                   for name in reads.findall(path.read_text()) if not name.startswith("CCTV_")}
        self.assertEqual(set(), foreign)

    def test_every_setting_follows_the_distributed_config(self) -> None:
        names = set()
        for path in product_code():
            names.update(re.findall(r"[\"'](CCTV_[A-Z0-9_]+)[\"']", path.read_text()))
        self.assertGreater(len(names), 100)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            local, state = root / "config", root / "state"
            local.mkdir()
            failover = ('[failover]\n[[failover.nodes]]\nname = "a"\nengine_agent = "http://127.0.0.1:18782"\n'
                        'bot_agent = "http://127.0.0.1:18783"\n')
            (local / "config.toml").write_text(failover)
            (local / "secrets.toml").write_text(f'[failover]\nsecret = "{SECRET}"\n')
            common = "".join(f'{name} = "distributed"\n' for name in sorted(names))
            files = {"config.toml": "[common]\n" + common + failover,
                     "secrets.toml": f'[failover]\nsecret = "{SECRET}"\n', "cameras.json": None}
            share.Store(state).save(1, "a", files)
            env = {"CCTV_CONFIG_DIR": str(local), "CCTV_STATE_DIR": str(state), "CCTV_NODE": "a",
                   **{name: "stale" for name in names if name not in ("CCTV_CONFIG_DIR", "CCTV_STATE_DIR", "CCTV_NODE")}}
            for role in settings.COMPONENTS:
                resolved = settings.apply(role, share.effective_env(role, env))
                wrong = {name: resolved.get(name) for name in names - share.NODE_LOCAL
                         if resolved.get(name) != "distributed"}
                self.assertEqual({}, wrong, f"{role}: настройка не следует раздаваемому конфигу")
                kept = {name for name in share.NODE_LOCAL & set(env) - {"CCTV_CONFIG_DIR", "CCTV_STATE_DIR", "CCTV_NODE"}
                        if resolved.get(name) != "stale"}
                self.assertEqual(set(), kept, f"{role}: узловой ключ перетёрт раздаваемым")

    def test_node_local_keys_and_role_files_are_documented(self) -> None:
        names = set(share.NODE_LOCAL) | {name for role in share.ROLE_FILES for name in share.ROLE_FILES[role]}
        for doc in ("failover.md", "failover.ru.md"):
            text = (ROOT / "docs" / doc).read_text()
            self.assertEqual([], sorted(name for name in names if f"`{name}`" not in text), doc)

    def test_single_node_install_is_untouched(self) -> None:
        """Без раздела [failover] копия конфига не действует, даже если осталась в state."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "config.toml").write_text('[engine]\nhuman_gate_mode = "shadow"\n')
            share.Store(root / "state").save(3, "a", {"config.toml": '[engine]\nhuman_gate_mode = "enforce"\n',
                                                      "secrets.toml": None, "cameras.json": None})
            env = {"CCTV_CONFIG_DIR": str(root / "config"), "CCTV_STATE_DIR": str(root / "state"),
                   "CCTV_HUMAN_GATE_MODE": "off"}
            self.assertEqual(env, share.effective_env("engine", env))


class BundleCheckTest(unittest.TestCase):
    """Присланная ревизия, на которой узел не запустится, не принимается."""

    def bundle(self, config: str) -> dict:
        files = {"config.toml": config, "secrets.toml": f'[failover]\nsecret = "{SECRET}"\n', "cameras.json": None}
        return {"rev": 2, "origin": "a", "sha": share.digest(files), "files": files}

    NODES = ('[failover]\n[[failover.nodes]]\nname = "a"\nengine_agent = "http://127.0.0.1:18782"\n'
             'bot_agent = "http://127.0.0.1:18783"\n')

    def test_rejects_config_without_this_node_or_broken(self) -> None:
        share.check_bundle(self.bundle(self.NODES), "a", "engine")
        for config in (self.NODES, "[failover\n", "[mystery]\nx = 1\n" + self.NODES, ""):
            with self.subTest(config=config[:20]), self.assertRaises(share.ShareError):
                share.check_bundle(self.bundle(config), "b" if config == self.NODES else "a", "engine")
        tampered = self.bundle(self.NODES)
        tampered["files"]["config.toml"] += "# changed\n"
        with self.assertRaises(share.ShareError):
            share.check_bundle(tampered, "a", "engine")

    def test_newer_revision_wins_ties_go_to_priority(self) -> None:
        rank = {"a": 0, "b": 1}.get
        self.assertTrue(share.newer({"rev": 3, "sha": "x", "origin": "b"}, {"rev": 2, "sha": "y", "origin": "a"}, rank))
        self.assertFalse(share.newer({"rev": 2, "sha": "x", "origin": "b"}, {"rev": 2, "sha": "y", "origin": "a"}, rank))
        self.assertTrue(share.newer({"rev": 2, "sha": "x", "origin": "a"}, {"rev": 2, "sha": "y", "origin": "b"}, rank))
        self.assertFalse(share.newer({"rev": 2, "sha": "x", "origin": "a"}, {"rev": 2, "sha": "x", "origin": "a"}, rank))


if __name__ == "__main__":
    unittest.main()
