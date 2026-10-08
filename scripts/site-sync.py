#!/usr/bin/env python3
"""Сайт и оба README говорят ту же версию, что и релиз, и одно имя платформы.

Значения стоят в текстах между метками <!--site:KEY-->…<!--/site--> (HTML-комментарий:
на сайте и на GitHub не виден). Источник один: версия — cctv/__init__.py, остальное —
docs/site/site.json. Смена имени платформы — правка site.json и запуск без флагов.

    python3 scripts/site-sync.py            # переписать значения в метках из источника
    python3 scripts/site-sync.py --check    # только проверить; расхождение — код 1
    python3 scripts/site-sync.py --print-version

--check гоняют CI и export-public.sh: поднятая версия без обновлённых сайта и README,
забытая метка или релиз без раздела в CHANGELOG роняют проверку.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = re.compile(r"<!--site:([\w.]+)-->(.*?)<!--/site-->", re.S)
# Файл → ключи, которые в нём обязаны встретиться хотя бы раз (иначе проверка пуста).
FILES = {
    "docs/site/index.html": {"version", "platform.en"},
    "docs/site/ru/index.html": {"version", "platform.ru", "platform.ru_in"},
    "README.md": {"version", "platform.en"},
    "README.ru.md": {"version", "platform.ru", "platform.ru_in"},
}


def version(root):
    text = (root / "cctv" / "__init__.py").read_text()
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        sys.exit("cctv/__init__.py: __version__ not found")
    return m.group(1)


def values(root):
    data = json.loads((root / "docs" / "site" / "site.json").read_text())
    vals = {k: v for k, v in data.items() if not k.startswith("_")}
    vals["version"] = version(root)
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="verify only, exit 1 on mismatch")
    ap.add_argument("--print-version", action="store_true")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    args = ap.parse_args()
    root = args.root.resolve()
    vals = values(root)
    if args.print_version:
        print(vals["version"])
        return 0

    problems, changed = [], []
    for rel, required in FILES.items():
        path = root / rel
        text = path.read_text()
        seen = set()

        def sub(m, rel=rel, seen=seen):
            key, cur = m.group(1), m.group(2)
            seen.add(key)
            if key not in vals:
                problems.append(f"{rel}: unknown key site:{key}")
                return m.group(0)
            if cur != vals[key]:
                problems.append(f"{rel}: site:{key} is {cur!r}, expected {vals[key]!r}")
            return f"<!--site:{key}-->{vals[key]}<!--/site-->"

        new = MARK.sub(sub, text)
        for key in sorted(required - seen):
            problems.append(f"{rel}: no <!--site:{key}--> mark")
        if new != text and not args.check:
            path.write_text(new)
            changed.append(rel)

    # Релизная версия (без .devN/rcN) обязана иметь свой раздел в CHANGELOG.
    ver = vals["version"]
    if re.fullmatch(r"\d+\.\d+\.\d+", ver):
        if not re.search(rf"^## \[{re.escape(ver)}\]", (root / "CHANGELOG.md").read_text(), re.M):
            problems.append(f"CHANGELOG.md: no '## [{ver}]' section")

    if args.check:
        if problems:
            print("site/README do not match the release (run scripts/site-sync.py, then review the text):",
                  file=sys.stderr)
            for p in dict.fromkeys(problems):  # одна строка на файл и ключ
                print("  " + p, file=sys.stderr)
            return 1
        print(f"site and READMEs match version {ver}")
        return 0
    for rel in changed:
        print(f"updated {rel}")
    hard = [p for p in problems if "unknown key" in p or "no <!--site" in p or "CHANGELOG" in p]
    for p in hard:
        print(p, file=sys.stderr)
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
