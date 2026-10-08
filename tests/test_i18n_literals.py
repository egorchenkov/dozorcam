"""Гейт CI: всё, что видит человек, — из каталогов i18n, а не русскими литералами в коде.

Проходит по исходникам `cctv/` разбором AST и ищет строковые литералы с
кириллицей. Не считаются: докстринги и комментарии (их AST не видит) и явный
allowlist ниже — у каждого пункта причина. Новый русский текст для человека
кладётся в `cctv/i18n/locales/{en,ru}.json` и берётся через `i18n.t`.
"""
from __future__ import annotations

import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "cctv"
CYRILLIC = re.compile("[А-Яа-яЁё]")

# Журнал процесса (docker logs / journald) — диагностика для разработчика, не
# интерфейс: строки пишутся как есть и грепаются при разборе инцидентов. Код
# владельца в журнале продублирован по-английски (main.announce_setup).
LOG_CALLS = {"log", "self.log", "log.info", "log.warning", "log.error", "log.debug",
             "log.exception", "self.server.log_line"}
# Данные, а не текст: таблица транслитерации имени камеры в camera_id
# (кириллица во входе, латиница на выходе).
DATA_TABLES = {("bot/bot.py", "TRANSLIT"), ("engine/camera_discovery.py", "TRANSLIT")}


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _allowed(node: ast.AST, parents: dict[int, ast.AST], rel: str) -> bool:
    """Литерал внутри вызова журнала или таблицы данных из allowlist."""
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.Call) and ast.unparse(current.func) in LOG_CALLS:
            return True
        if isinstance(current, ast.Assign):
            names = {ast.unparse(target) for target in current.targets}
            return any((rel, name) in DATA_TABLES for name in names)
        if isinstance(current, ast.stmt):
            return False
        current = parents.get(id(current))
    return False


def russian_literals() -> list[str]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstrings(tree)
        parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and CYRILLIC.search(node.value) and id(node) not in docstrings
                    and not _allowed(node, parents, rel)):
                found.append(f"cctv/{rel}:{node.lineno}: {node.value[:60]!r}")
    return found


class RussianLiteralGateTest(unittest.TestCase):
    def test_no_russian_literals_outside_locales(self):
        found = russian_literals()
        self.assertEqual([], found, "русский текст для человека — в cctv/i18n/locales, через i18n.t:\n"
                         + "\n".join(found))

    def test_gate_catches_a_literal(self):
        tree = ast.parse('def f():\n    """Док."""\n    log("журнал")\n    return "Привет"\n')
        parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        docs = _docstrings(tree)
        flagged = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                   and isinstance(n.value, str) and id(n) not in docs and not _allowed(n, parents, "x.py")]
        self.assertEqual(["Привет"], flagged)

    def test_allowlisted_tables_exist(self):
        for rel, name in DATA_TABLES:
            with self.subTest(table=name, file=rel):
                self.assertRegex((ROOT / rel).read_text(encoding="utf-8"), rf"(?m)^{name} = ")


if __name__ == "__main__":
    unittest.main()
