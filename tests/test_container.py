"""Супервизор контейнера: пустой конфиг — понятное «нет конфига», а не цикл рестартов.

Сеть не нужна: getMe подменяется, все пути — во временном каталоге.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

from cctv import container

TOKEN = "1234567:AAFakeTokenForUnitTestsOnly000000000"


class ContainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.config = root / "config"
        self.run_dir = root / "run"
        self.config.mkdir()
        self.run_dir.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "CCTV_CONFIG_DIR": str(self.config),
               "CCTV_STATE_DIR": str(root / "state")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        status = mock.patch.object(container, "STATUS_PATH", self.run_dir / "supervisor.json")
        status.start()
        self.addCleanup(status.stop)

    def engine(self) -> container.EngineSupervisor:
        sup = container.EngineSupervisor()
        sup.proxy_config = self.run_dir / "cameras.proxy.json"
        sup.empty_config = self.run_dir / "cameras.empty.json"
        return sup

    def test_engine_without_cameras_starts_with_empty_registry(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            env = self.engine().prepare()
        self.assertIsInstance(env, dict)
        self.assertEqual(json.loads(pathlib.Path(env["CCTV_CAMERA_CONFIG"]).read_text()), {"cameras": []})
        self.assertEqual(env["CCTV_RUNTIME_CAMERA_CONFIG"], str(self.run_dir / "cameras.proxy.json"))
        self.assertIn("no config", out.getvalue())

    def test_supervisor_speaks_the_configured_language(self) -> None:
        """lang из config.toml ([common]) — и журнал супервизора, и healthcheck по-русски."""
        (self.config / "config.toml").write_text('[common]\nlang = "ru"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.engine().prepare()
        self.assertIn("нет конфига", out.getvalue())
        self.assertIn("движок запущен без камер", out.getvalue())
        container.write_status("no_config", "x", role="engine")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(container.health(), 1)
        self.assertIn("нет конфига: x", out.getvalue())

    def test_bot_config_error_in_configured_language(self) -> None:
        (self.config / "config.toml").write_text('[bot]\nlang = "ru"\nchat_id = -100777\nallowed_user_ids = [7]\n')
        state, message = container.BotSupervisor().prepare()
        self.assertEqual(("no_config", "не задана обязательная переменная CCTV_BOT_TOKEN"), (state, message))
        os.environ["CCTV_LANG"] = "en"  # окружение сильнее файла
        state, message = container.BotSupervisor().prepare()
        self.assertEqual("required variable CCTV_BOT_TOKEN is not set", message)

    def test_engine_prefers_registry_written_from_chat(self) -> None:
        """Э4: после первой камеры из чата реестр живёт в state, а не в каталоге конфига."""
        (self.config / "cameras.json").write_text('{"cameras": []}')
        sup = self.engine()
        with contextlib.redirect_stdout(io.StringIO()):
            env = sup.prepare()
        state = pathlib.Path(os.environ["CCTV_STATE_DIR"])
        self.assertEqual(env["CCTV_REGISTRY_FILE"], str(state / "cameras.json"))
        self.assertEqual(env["CCTV_REGISTRY_SEED"], str(self.config / "cameras.json"))
        self.assertEqual(env["CCTV_CAMERA_CONFIG"], str(self.config / "cameras.json"))
        before = sup.fingerprint()
        state.mkdir(parents=True, exist_ok=True)
        (state / "cameras.json").write_text('{"cameras": []}\n')
        self.assertNotEqual(before, sup.fingerprint())  # запись из чата перезапускает цепочку
        with contextlib.redirect_stdout(io.StringIO()):
            env = sup.prepare()
        self.assertEqual(env["CCTV_CAMERA_CONFIG"], str(state / "cameras.json"))

    def test_engine_with_broken_cameras_waits_for_config(self) -> None:
        (self.config / "cameras.json").write_text("{not json")
        state, message = self.engine().prepare()
        self.assertEqual(state, "no_config")
        self.assertIn("cameras.json", message)

    def test_bot_without_token_waits_for_config(self) -> None:
        os.environ.update({"CCTV_CHAT_ID": "-100777", "CCTV_ALLOWED_USER_IDS": "7"})
        state, message = container.BotSupervisor().prepare()
        self.assertEqual(state, "no_config")
        self.assertIn("CCTV_BOT_TOKEN", message)

    def test_bot_placeholder_token_is_not_sent_to_telegram(self) -> None:
        os.environ.update({"CCTV_BOT_TOKEN": "123456:replace-me", "CCTV_CHAT_ID": "-100777",
                           "CCTV_ALLOWED_USER_IDS": "7"})
        with mock.patch.object(container, "token_rejected") as getme:
            state, _ = container.BotSupervisor().prepare()
        self.assertEqual(state, "no_config")
        getme.assert_not_called()

    def test_bot_rejected_token_waits_for_config(self) -> None:
        os.environ.update({"CCTV_BOT_TOKEN": TOKEN, "CCTV_CHAT_ID": "-100777", "CCTV_ALLOWED_USER_IDS": "7"})
        with mock.patch.object(container, "token_rejected", return_value="Telegram отклонил"):
            self.assertEqual(container.BotSupervisor().prepare(), ("no_config", "Telegram отклонил"))

    def test_empty_env_line_does_not_shadow_config_file(self) -> None:
        # compose подставляет "" для пустой строки .env — значение из config.toml должно победить.
        (self.config / "config.toml").write_text("[bot]\nchat_id = -100777\nallowed_user_ids = [7]\n")
        os.environ.update({"CCTV_BOT_TOKEN": TOKEN, "CCTV_CHAT_ID": "", "CCTV_ALLOWED_USER_IDS": ""})
        with mock.patch.object(container, "token_rejected", return_value=""):
            env = container.BotSupervisor().prepare()
        self.assertIsInstance(env, dict)
        self.assertNotIn("CCTV_CHAT_ID", env)

    def test_health_reports_no_config_reason(self) -> None:
        container.write_status("no_config", "required variable CCTV_BOT_TOKEN is not set", role="bot")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(container.health(), 1)
        self.assertIn("no config: required variable CCTV_BOT_TOKEN is not set", out.getvalue())

    def test_health_rejects_stale_status_and_dead_children(self) -> None:
        container.write_status("running", role="engine", children={"bridge": False})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(container.health(), 1)
        self.assertIn("bridge", out.getvalue())
        payload = json.loads(container.STATUS_PATH.read_text())
        payload["updated_at"] = time.time() - 3600
        container.STATUS_PATH.write_text(json.dumps(payload))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(container.health(), 1)

    def test_health_engine_with_internal_tls_from_config_checks_port_not_http(self) -> None:
        """internal_tls из config.toml: проверка — порт моста, а не http к mTLS-серверу."""
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        (self.config / "config.toml").write_text(f"[common]\ninternal_tls = true\n\n[engine]\nport = {port}\n")
        container.write_status("running", role="engine", children={"bridge": True})
        with mock.patch.object(container.urllib.request, "urlopen", side_effect=AssertionError("http")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(container.health(), 0)
        self.assertIn("mTLS", out.getvalue())
        listener.close()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(container.health(), 1)
        self.assertIn("is not listening", out.getvalue())

    def test_crashing_child_backs_off_instead_of_tight_loop(self) -> None:
        child = container.Child("crash", [sys.executable, "-c", "raise SystemExit(3)"])
        pauses = []
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                child.start(dict(os.environ))
                child.proc.wait()
                before = time.monotonic()
                self.assertEqual(child.reap(), 3)
                pauses.append(round(child.next_start - before))
        self.assertEqual(pauses, [5, 10, 20])


if __name__ == "__main__":
    unittest.main()
