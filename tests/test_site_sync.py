"""Сайт и оба README говорят версию релиза: scripts/site-sync.py --check."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "site-sync.py"
FILES = ["cctv/__init__.py", "CHANGELOG.md", "README.md", "README.ru.md",
         "docs/site/site.json", "docs/site/index.html", "docs/site/ru/index.html"]


def run(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True)


def copy_tree(tmp_path):
    for rel in FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    return tmp_path


def test_repo_matches_release_version():
    res = run(ROOT, "--check")
    assert res.returncode == 0, res.stderr


def test_version_bump_without_site_fails(tmp_path):
    root = copy_tree(tmp_path)
    init = root / "cctv/__init__.py"
    old = run(root, "--print-version").stdout.strip()
    init.write_text(init.read_text().replace(f'"{old}"', '"9.9.9"'))
    res = run(root, "--check")
    assert res.returncode == 1
    assert "expected '9.9.9'" in res.stderr
    assert "no '## [9.9.9]' section" in res.stderr
    # Синхронизация переписывает метки во всех четырёх файлах; раздел CHANGELOG — руками.
    run(root)
    for rel in FILES[2:4] + FILES[5:]:
        assert "<!--site:version-->9.9.9<!--/site-->" in (root / rel).read_text(), rel
    changelog = root / "CHANGELOG.md"
    changelog.write_text(changelog.read_text() + "\n## [9.9.9] — 2099-01-01\n")
    assert run(root, "--check").returncode == 0


def test_platform_name_is_one_edit(tmp_path):
    # Имени у платформы пока нет (в site.json — описание); имя потом — одна правка site.json.
    root = copy_tree(tmp_path)
    site = root / "docs/site/site.json"
    data = json.loads(site.read_text())
    described = data["platform.en"]
    data.update({"platform.en": "Guild", "platform.ru": "Гильдия", "platform.ru_in": "Гильдии"})
    site.write_text(json.dumps(data, ensure_ascii=False))
    assert run(root, "--check").returncode == 1
    assert run(root).returncode == 0
    assert run(root, "--check").returncode == 0
    for rel in ("docs/site/index.html", "README.md"):
        text = (root / rel).read_text()
        assert "Built on <!--site:platform.en-->Guild<!--/site-->" in text, rel
        assert described not in text, rel
    for rel in ("docs/site/ru/index.html", "README.ru.md"):
        text = (root / rel).read_text()
        assert "Создано на <!--site:platform.ru_in-->Гильдии<!--/site-->" in text, rel


def test_no_working_platform_name():
    # Рабочее имя платформы снято 09.10.2026 (решение владельца): имени у неё пока нет, только описание.
    for rel in FILES[2:]:
        text = (ROOT / rel).read_text()
        assert not re.search(r"art[e]l|арт[е]л", text, re.I), rel


def test_missing_mark_fails(tmp_path):
    root = copy_tree(tmp_path)
    readme = root / "README.ru.md"
    readme.write_text(re.sub(r"<!--site:version-->(.*?)<!--/site-->", r"\1", readme.read_text()))
    res = run(root, "--check")
    assert res.returncode == 1 and "README.ru.md: no <!--site:version--> mark" in res.stderr


def test_release_needs_russian_changelog_section(tmp_path):
    # С 0.3.1 список изменений и по-русски: релиз без раздела в CHANGELOG.ru.md не проходит.
    root = copy_tree(tmp_path)
    shutil.copy(ROOT / "CHANGELOG.ru.md", root / "CHANGELOG.ru.md")
    init = root / "cctv/__init__.py"
    old = run(root, "--print-version").stdout.strip()
    init.write_text(init.read_text().replace(f'"{old}"', '"9.9.9"'))
    run(root)
    changelog = root / "CHANGELOG.md"
    changelog.write_text(changelog.read_text() + "\n## [9.9.9] — 2099-01-01\n")
    res = run(root, "--check")
    assert res.returncode == 1 and "CHANGELOG.ru.md: no '## [9.9.9]' section" in res.stderr
    ru = root / "CHANGELOG.ru.md"
    ru.write_text(ru.read_text() + "\n## [9.9.9] — 2099-01-01\n")
    assert run(root, "--check").returncode == 0
