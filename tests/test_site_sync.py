"""Сайт и оба README говорят версию релиза: scripts/site-sync.py --check."""
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
    root = copy_tree(tmp_path)
    site = root / "docs/site/site.json"
    site.write_text(site.read_text().replace('"Artel"', '"Guild"'))
    assert run(root, "--check").returncode == 1
    assert run(root).returncode == 0
    for rel in ("docs/site/index.html", "README.md"):
        text = (root / rel).read_text()
        assert "<!--site:platform.en-->Guild<!--/site-->" in text and ">Artel<" not in text, rel


def test_missing_mark_fails(tmp_path):
    root = copy_tree(tmp_path)
    readme = root / "README.ru.md"
    readme.write_text(re.sub(r"<!--site:version-->(.*?)<!--/site-->", r"\1", readme.read_text()))
    res = run(root, "--check")
    assert res.returncode == 1 and "README.ru.md: no <!--site:version--> mark" in res.stderr
