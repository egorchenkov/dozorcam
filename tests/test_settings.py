"""Единый конфиг-каталог: /etc/cctv по умолчанию, всё переопределяется переменными.

На сервере с продом /etc/cctv не создаётся: каждый тест работает во временном
каталоге и в копии окружения.
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cctv import cli, settings
from cctv.bot import config as bot_config

CONFIG = """
[common]
storage_budget_bytes = 1024

[engine]
human_gate_mode = "enforce"
events_cert = "/engine/client.crt"
port = 18780

[bot]
chat_id = -100777
allowed_user_ids = [7, 8]
events_cert = "/bot/server.crt"
"""


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "config.toml").write_text(CONFIG, encoding="utf-8")
        secrets = self.dir / "secrets.toml"
        secrets.write_text('[bot]\nbot_token = "0:secret"\n', encoding="utf-8")
        secrets.chmod(0o600)
        self.env = {"CCTV_CONFIG_DIR": str(self.dir)}

    def test_defaults_point_to_etc_cctv_and_var_lib_cctv(self):
        env: dict[str, str] = {}
        self.assertEqual(pathlib.Path("/etc/cctv"), settings.config_dir(env))
        self.assertEqual(pathlib.Path("/var/lib/cctv/state"), settings.state_dir(env))
        self.assertEqual(pathlib.Path("/var/lib/cctv/buffer"), settings.buffer_dir(env))

    def test_paths_are_overridable(self):
        env = {"CCTV_CONFIG_DIR": "/x/conf", "CCTV_STATE_DIR": "/x/state", "CCTV_BUFFER_DIR": "/x/buf"}
        self.assertEqual(pathlib.Path("/x/conf"), settings.config_dir(env))
        self.assertEqual(pathlib.Path("/x/state"), settings.state_dir(env))
        self.assertEqual(pathlib.Path("/x/buf"), settings.buffer_dir(env))

    def test_component_gets_common_and_own_section_only(self):
        """CCTV_EVENTS_CERT у моста и у бота — разные файлы: разделы не смешиваются."""
        engine = settings.apply("engine", dict(self.env))
        bot = settings.apply("bot", dict(self.env))
        self.assertEqual("/engine/client.crt", engine["CCTV_EVENTS_CERT"])
        self.assertEqual("/bot/server.crt", bot["CCTV_EVENTS_CERT"])
        self.assertEqual("enforce", engine["CCTV_HUMAN_GATE_MODE"])
        self.assertNotIn("CCTV_HUMAN_GATE_MODE", bot)
        self.assertNotIn("CCTV_BOT_TOKEN", engine)
        self.assertEqual("1024", engine["CCTV_STORAGE_BUDGET_BYTES"])
        self.assertEqual("1024", bot["CCTV_STORAGE_BUDGET_BYTES"])
        self.assertEqual("7 8", bot["CCTV_ALLOWED_USER_IDS"])
        self.assertEqual("0:secret", bot["CCTV_BOT_TOKEN"])

    def test_environment_beats_file(self):
        env = settings.apply("engine", {**self.env, "CCTV_HUMAN_GATE_MODE": "off"})
        self.assertEqual("off", env["CCTV_HUMAN_GATE_MODE"])

    def test_cameras_state_buffer_follow_layout(self):
        env = settings.apply("engine", dict(self.env))
        self.assertEqual(str(self.dir / "cameras.json"), env["CCTV_CAMERA_CONFIG"])
        self.assertEqual("/var/lib/cctv/state", env["CCTV_STATE_DIR"])
        self.assertEqual("/var/lib/cctv/buffer", env["CCTV_BUFFER_DIR"])
        moved = settings.apply("engine", {**self.env, "CCTV_STATE_DIR": "/srv/s", "CCTV_BUFFER_DIR": "/dev/shm/b"})
        self.assertEqual("/srv/s", moved["CCTV_STATE_DIR"])
        self.assertEqual("/dev/shm/b", moved["CCTV_BUFFER_DIR"])

    def test_engine_dirs_follow_overrides(self):
        storage = self.dir / "store"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(storage / "state", settings.engine_state(storage))
            self.assertEqual(storage / "buffer", settings.engine_buffer(storage))
        with mock.patch.dict(os.environ, {"CCTV_STATE_DIR": "/s", "CCTV_BUFFER_DIR": "/b"}, clear=True):
            self.assertEqual(pathlib.Path("/s"), settings.engine_state(storage))
            self.assertEqual(pathlib.Path("/b"), settings.engine_buffer(storage))

    def test_default_link_is_loopback_without_tls(self):
        engine = settings.apply("engine", dict(self.env))
        bot = settings.apply("bot", dict(self.env))
        self.assertEqual("127.0.0.1", engine["CCTV_BIND"])
        self.assertEqual("http://127.0.0.1:18780", engine["CCTV_PUBLIC_URL"])
        self.assertTrue(engine["CCTV_EVENTS_URL"].startswith("http://127.0.0.1:"))
        self.assertTrue(bot["CCTV_BRIDGE_URL"].startswith("http://127.0.0.1:"))
        cfg = bot_config.load(bot)
        self.assertFalse(cfg.internal_tls)
        self.assertIsNone(cfg.bridge_client_cert)
        self.assertTrue(cfg.events_enabled)
        self.assertEqual(frozenset({7, 8}), cfg.owner_ids)

    def test_tls_switch_changes_defaults(self):
        engine = settings.apply("engine", {**self.env, "CCTV_INTERNAL_TLS": "1"})
        self.assertTrue(engine["CCTV_PUBLIC_URL"].startswith("https://"))
        self.assertNotIn("CCTV_EVENTS_URL", engine)  # адрес бота при TLS задаётся явно
        (self.dir / "config.toml").write_text(CONFIG.replace("[common]", "[common]\ninternal_tls = true"),
                                              encoding="utf-8")
        bot = settings.apply("bot", dict(self.env))
        self.assertEqual("1", bot["CCTV_INTERNAL_TLS"])
        self.assertTrue(bot["CCTV_BRIDGE_URL"].startswith("https://"))

    def test_missing_config_dir_is_not_an_error(self):
        env = settings.apply("engine", {"CCTV_CONFIG_DIR": str(self.dir / "absent")})
        self.assertEqual(str(self.dir / "absent" / "cameras.json"), env["CCTV_CAMERA_CONFIG"])

    def test_unknown_section_and_bad_toml_refuse_start(self):
        (self.dir / "config.toml").write_text("[camera]\nx = 1\n", encoding="utf-8")
        with self.assertRaises(settings.SettingsError):
            settings.load("engine", self.env)
        (self.dir / "config.toml").write_text("[engine\n", encoding="utf-8")
        with self.assertRaises(settings.SettingsError):
            settings.load("engine", self.env)

    def test_entry_point_reports_bad_config_with_code_2(self):
        (self.dir / "config.toml").write_text("[engine\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(2, cli.run("bridge"))

    def test_entry_point_usage(self):
        self.assertEqual(64, cli.main([]))
        self.assertEqual(64, cli.main(["rm"]))


if __name__ == "__main__":
    unittest.main()
