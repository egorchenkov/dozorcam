"""Файлы релиза (scripts/release-assets.sh) — те, что GitHub отдаёт под тем же именем.

GitHub переименовывает ассет с точкой в начале: «.env.example» → «default.env.example», и
install.sh с `dozorcam update` не нашли бы его в релизе (найдено при выпуске 0.3.0).
"""
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(tmp_path):
    subprocess.run(["sh", str(ROOT / "scripts" / "release-assets.sh"), str(tmp_path)], check=True)
    return sorted(p.name for p in tmp_path.iterdir())


def test_asset_names_survive_github_upload(tmp_path):
    names = build(tmp_path)
    assert names == ["CHANGELOG.md", "SHA256SUMS", "compose.yml", "dozorcam", "env.example", "install.sh"]
    for name in names:
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name), name


def test_sha256sums_cover_every_asset(tmp_path):
    names = build(tmp_path)
    sums = dict(reversed(line.split()) for line in (tmp_path / "SHA256SUMS").read_text().splitlines())
    assert sorted(sums) == sorted(set(names) - {"SHA256SUMS"})
    for name, digest in sums.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest, name
    assert (tmp_path / "env.example").read_bytes() == (ROOT / ".env.example").read_bytes()


def test_installer_and_helper_fetch_the_same_names():
    for script, var in (("install.sh", "FILES"), ("dozorcam", "RELEASE_FILES")):
        text = (ROOT / "scripts" / script).read_text()
        files = re.search(rf'^{var}="([^"]+)"', text, re.M).group(1).split()
        assert "env.example" in files and ".env.example" not in files, script
