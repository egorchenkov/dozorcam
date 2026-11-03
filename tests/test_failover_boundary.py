"""Граница слоя отказоустойчивости: основной код о соседях не знает.

* Движок, бот и общие модули не импортируют cctv.failover — слой виден только
  супервизору контейнера (cctv/container.py).
* Супервизор говорит со слоем узким набором вызовов: «активна ли роль» (active),
  «процессы запущены/остановлены» (report), сон до смены назначения (wait) и то,
  что слой подставляет процессам роли в окружение (адрес соседней роли, панель).
  Новая связь супервизора со слоем — осознанная правка этого списка, а не случайность.
* Процессы роли получают от слоя только переменные окружения; бот — адрес и ключ
  панели /nodes, ни имени узла, ни списка соседей. Раздаваемый конфиг (share.py)
  доходит до роли тоже окружением — каталогом конфига CCTV_CONFIG_DIR.
* Настройки слоя — только из раздаваемого каталога конфига (config.toml,
  secrets.toml); из окружения узла — одно его имя CCTV_NODE. Новый ключ раздела,
  не внесённый в схему, конфиг отвергает, а каждый ключ схемы описан в docs.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from cctv import failover
from cctv.failover import config as fo_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "cctv"
LAYER = PACKAGE / "failover"
SUPERVISOR = PACKAGE / "container.py"

# Что супервизор берёт у агента слоя. active/report/wait — «включись/замри»;
# holder/cfg — для журнала и статуса; wiring/panel_env — окружение процессов роли;
# stop — остановка самого агента.
SUPERVISOR_USES = {"active", "report", "wait", "holder", "cfg", "wiring", "panel_env", "stop"}
# Что слой кладёт в окружение процессов роли.
CHILD_ENV = {"CCTV_BRIDGE_URL", "CCTV_EVENTS_URL", "CCTV_NODES_URL", "CCTV_NODES_TOKEN"}


def imports_of(path: pathlib.Path) -> set[str]:
    """Абсолютные имена модулей, которые импортирует файл пакета cctv."""
    tree = ast.parse(path.read_text(), str(path))
    package = path.relative_to(ROOT).with_suffix("").parts[:-1]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - node.level + 1]
                module = ".".join((*base, node.module)) if node.module else ".".join(base)
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def main_code() -> list[pathlib.Path]:
    return [path for path in PACKAGE.rglob("*.py")
            if LAYER not in path.parents and path != SUPERVISOR and "__pycache__" not in path.parts]


class BoundaryTest(unittest.TestCase):
    def test_engine_bot_and_common_code_do_not_import_the_layer(self) -> None:
        files = main_code()
        self.assertTrue(any("engine" in path.parts for path in files))
        self.assertTrue(any("bot" in path.parts for path in files))
        offenders = {str(path.relative_to(ROOT)): sorted(name for name in imports_of(path)
                                                          if name == "cctv.failover"
                                                          or name.startswith("cctv.failover."))
                     for path in files}
        self.assertEqual({}, {path: names for path, names in offenders.items() if names})

    def test_main_code_does_not_know_nodes(self) -> None:
        """Ни имени узла, ни раздела [failover] в движке и боте — только панель от супервизора."""
        for path in main_code():
            if path.name == "settings.py":
                continue  # общий загрузчик пропускает раздел [failover] своему слою
            text = path.read_text()
            self.assertNotIn("CCTV_NODE\"", text, path)
            self.assertNotIn("CCTV_NODE'", text, path)
            self.assertIsNone(re.search(r"\[failover\]|[\"']failover[\"']", text), path)

    def test_supervisor_uses_only_the_narrow_switch(self) -> None:
        tree = ast.parse(SUPERVISOR.read_text())
        used = {node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "layer" and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"}
        self.assertTrue({"active", "report", "wait"} <= used)
        self.assertEqual(set(), used - SUPERVISOR_USES, "супервизор берёт у слоя больше, чем «включись/замри»")

    def test_layer_gives_role_processes_only_environment(self) -> None:
        from cctv.failover import share

        names = set()
        for path in LAYER.glob("*.py"):
            found = set(re.findall(r"\"(CCTV_[A-Z_]+)\"", path.read_text()))
            if path.name == "share.py":
                # Здесь только классификация узловых ключей; роли модуль подменяет
                # одно — каталог конфига (effective_env).
                self.assertLessEqual(found, share.NODE_LOCAL)
                self.assertIn("CCTV_CONFIG_DIR", found)
                continue
            names.update(found)
        self.assertEqual(CHILD_ENV | {"CCTV_NODE"}, names)
        bot_names = set(re.findall(r"CCTV_NODES?_[A-Z]+", (PACKAGE / "bot" / "config.py").read_text()))
        self.assertEqual({"CCTV_NODES_URL", "CCTV_NODES_TOKEN"}, bot_names)


class DistributedConfigTest(unittest.TestCase):
    """Новая настройка слоя обязана жить в раздаваемом конфиге, а не в окружении узла."""

    def write(self, root: pathlib.Path, section: str, secret: str = "unit-test-failover-secret-0123456789") -> None:
        (root / "config.toml").write_text(section)
        (root / "secrets.toml").write_text(f'[failover]\nsecret = "{secret}"\n')
        (root / "secrets.toml").chmod(0o600)

    NODES = ('[failover]\n{top}[[failover.nodes]]\nname = "a"\naddress = "127.0.0.1"\n'
             '[[failover.nodes]]\nname = "b"\nengine_agent = "http://127.0.0.1:18782"\n'
             'bot_agent = "http://127.0.0.1:18783"\npriority = 5\n')

    def test_only_node_name_comes_from_environment(self) -> None:
        source = (LAYER / "config.py").read_text()
        self.assertEqual({"CCTV_NODE"}, set(re.findall(r"env\.get\(\"([A-Z_]+)\"", source)))
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, self.NODES.format(top=""))
            base = {"CCTV_CONFIG_DIR": tmp, "CCTV_NODE": "b"}
            cfg = failover.load(base)
            # Посторонние CCTV_* окружения узла на слой не влияют: всё — из каталога конфига.
            noisy = {**base, "CCTV_FAILOVER_CHECK_SEC": "5", "CCTV_CHECK_SEC": "5", "CCTV_PRIORITY": "0",
                     "CCTV_FAILOVER_SECRET": "x" * 40}
            self.assertEqual(cfg, failover.load(noisy))
            self.assertEqual(("a", "b"), tuple(node.name for node in cfg.ordered))
            self.assertEqual(fo_config.DEFAULT_CHECK_SEC, cfg.check_sec)
            self.assertEqual("b", cfg.me)
            self.assertEqual("a", failover.load({**base, "CCTV_NODE": "a"}).me)

    def test_compose_passes_node_name_to_both_roles(self) -> None:
        """CCTV_NODE из .env узла доходит до обоих контейнеров — иначе [failover] не стартует."""
        text = (ROOT / "compose.yml").read_text()
        services = re.split(r"\n  (?=engine:|bot:)", text.split("\nservices:\n", 1)[1])
        for service in services:
            self.assertIn("CCTV_NODE: ${CCTV_NODE:-}", service, service.splitlines()[0])
        self.assertEqual(2, len(services))
        self.assertIn("CCTV_NODE=", (ROOT / ".env.example").read_text())

    def test_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for section, key in ((self.NODES.format(top="check_every = 30\n"), "failover.check_every"),
                                 (self.NODES.format(top="") + "weight = 2\n",
                                  "failover.nodes[1].weight")):
                self.write(root, section)
                with self.assertRaises(failover.FailoverError) as caught:
                    failover.load({"CCTV_CONFIG_DIR": tmp, "CCTV_NODE": "a"})
                self.assertEqual("unknown_key", caught.exception.code)
                self.assertEqual(key, caught.exception.params["key"])

    def test_every_schema_key_is_documented(self) -> None:
        keys = set(fo_config.SECTION_KEYS) | set(fo_config.NODE_KEYS) | set(fo_config.SECRET_KEYS)
        for doc in ("failover.md", "failover.ru.md"):
            text = (ROOT / "docs" / doc).read_text()
            missing = sorted(key for key in keys if f"`{key}`" not in text and f"{key} =" not in text)
            self.assertEqual([], missing, doc)
            self.assertIn("CCTV_NODE", text)


class MemoryNotCarriedTest(unittest.TestCase):
    """С ролью переезжает только каталог state; файлы слоя и журнал узла остаются."""

    def test_handover_moves_role_disk_only(self) -> None:
        from cctv.failover import handover

        with tempfile.TemporaryDirectory() as tmp:
            old, new = pathlib.Path(tmp) / "old", pathlib.Path(tmp) / "new"
            (old / "diag").mkdir(parents=True)
            (old / "bot.sqlite3").write_text("db")
            (old / "calibration").mkdir()
            (old / "calibration" / "yard.json").write_text("{}")
            (old / "failover.json").write_text('{"engine": "old"}')
            (old / "diag" / "today.log").write_text("old node")
            new.mkdir()
            (new / "failover.json").write_text('{"engine": "new"}')
            (new / "stale.txt").write_text("left from before")
            self.assertEqual(2, handover.unpack(handover.pack(old), new))
            self.assertEqual("db", (new / "bot.sqlite3").read_text())
            self.assertTrue((new / "calibration" / "yard.json").is_file())
            self.assertEqual('{"engine": "new"}', (new / "failover.json").read_text())
            self.assertFalse((new / "diag").exists())
            self.assertFalse((new / "stale.txt").exists())


if __name__ == "__main__":
    unittest.main()
