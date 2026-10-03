"""Приёмка приложения без камер и Telegram: границы бота, изоляция юнитов, автономность.

Перенесено из внешнего скрипта приёмки, который гонял тесты чужим интерпретатором;
теперь это часть обычного pytest. Живые проверки (камера, бот, группа) — на стенде.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock

from cctv.bot import config, main as bot_main

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "cctv" / "bot"
UNITS = ROOT / "deploy" / "systemd"

# Привязки к внешней платформе, которых в автономном приложении быть не должно.
# Шаблоны собираются из частей, чтобы этот файл сам не совпадал с проверкой.
FORBIDDEN = ("/srv/" + "platform", "/opt/" + "claude-bot", "cctv_" + "notify", "acceptance-" + "cctv")


def tracked_files() -> list[pathlib.Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / name for name in out if (ROOT / name).is_file()]


class AutonomyTest(unittest.TestCase):
    def test_no_platform_bindings_in_tree(self):
        hits = []
        for path in tracked_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for needle in FORBIDDEN:
                if needle in text:
                    hits.append(f"{path.relative_to(ROOT)}: {needle}")
        self.assertEqual([], hits)

    def test_units_use_own_venv_and_config_dir(self):
        for unit in UNITS.glob("*.service"):
            text = unit.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    self.assertIn("/opt/cctv/venv/bin/cctv-", line, unit.name)
            self.assertIn("Environment=CCTV_CONFIG_DIR=/etc/cctv", text, unit.name)
            self.assertNotIn("EnvironmentFile=", text, unit.name)


class BotBoundaryTest(unittest.TestCase):
    """Бот детерминированный: ни LLM, ни доступа к камерам, ни зашитых секретов."""

    runtime = sorted(BOT_DIR.glob("*.py"))

    def test_no_llm_client_or_keys(self):
        pattern = re.compile(r"^\s*(import|from)\s+(anthropic|openai|litellm|langchain)"
                             r"|(Anthropic|OpenAI)\(|environ[^)]*(ANTHROPIC|OPENAI)_API_KEY", re.M)
        for path in self.runtime:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)

    def test_bot_never_talks_to_cameras(self):
        """Слова RTSP/ONVIF бот показывать может (мастер камер), ходить к камерам — нет:
        ни адресов протоколов камер, ни клиентов ONVIF/видео, ни подпроцессов."""
        pattern = re.compile(r"rtsp://|/ISAPI/|^\s*(import|from)\s+(onvif|zeep|cv2|subprocess)\b",
                             re.I | re.M)
        for path in self.runtime:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)

    def test_bot_does_not_import_engine(self):
        pattern = re.compile(r"^\s*(from\s+(\.\.engine|cctv\.engine)|import\s+cctv\.engine)", re.M)
        for path in self.runtime:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), path.name)

    def test_no_hardcoded_secrets(self):
        pattern = re.compile(r"(BOT_TOKEN|CA)\s*=\s*[\"']([^\"']{12,})")
        for path in sorted((ROOT / "cctv").rglob("*.py")):
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))

    def test_secrets_are_not_tracked(self):
        names = [path.name for path in tracked_files()]
        self.assertNotIn("secrets.toml", names)


class BotUnitIsolationTest(unittest.TestCase):
    unit = (UNITS / "cctv-tg-bot.service").read_text(encoding="utf-8")

    def test_hardening_directives(self):
        for directive in ("User=cctvbot", "NoNewPrivileges=yes", "PrivateTmp=yes",
                          "ProtectSystem=strict", "ProtectHome=yes", "CapabilityBoundingSet=",
                          "MemoryDenyWriteExecute=yes", "RuntimeDirectory=cctv-tg-bot"):
            self.assertIn(directive, self.unit)

    def test_writes_only_own_state(self):
        self.assertRegex(self.unit, r"(?m)^ReadWritePaths=/var/lib/cctv/state/bot /run/cctv-tg-bot$")


class StartGuardsTest(unittest.TestCase):
    def test_foreign_secrets_block_start(self):
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY", "TELEGRAM_BOT_TOKEN"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "x"}):
                with self.assertRaises(config.ConfigError):
                    bot_main._forbid_foreign_secrets(None)

    def test_incomplete_environment_refuses_start(self):
        with self.assertRaises(config.ConfigError):
            config.load({"CCTV_CHAT_ID": "-1", "CCTV_ALLOWED_USER_IDS": "1"})
        # Без группы и allow-list бот стартует: их задаёт мастер первого запуска.
        cfg = config.load({"CCTV_BOT_TOKEN": "0:t"})
        self.assertIsNone(cfg.chat_id)
        self.assertEqual(frozenset(), cfg.allowed_user_ids)

    def test_bot_entry_point_fails_fast_on_empty_config(self):
        env = {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(ROOT / "no-such-dir"),
               "PYTHONPATH": str(ROOT)}
        done = subprocess.run([sys.executable, "-m", "cctv", "bot"], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(2, done.returncode, done.stderr)
        self.assertIn("FAIL: не задана обязательная переменная", done.stderr)


if __name__ == "__main__":
    unittest.main()
