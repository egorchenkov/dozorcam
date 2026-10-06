"""Активация новых Hikvision из /add: протоколы V3 и legacy, пакет, частичный сбой.

Камеры — эмуляторы (`hik_emulator.py`) на адресах 127.0.0.x одного порта:
мост ходит к ним ровно теми запросами, какими пойдёт к живым. Что проверяется:
криптография по эталонным векторам, обе ветки протокола, правила пароля,
пароль на диске (0600) раньше сетевого вызова, однократная выдача пароля,
один логин новым паролем без повторов, ONVIF-пользователь, проба потока по
ONVIF/RTSP и запись в реестр.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cctv.engine import camera_discovery as discovery
from cctv.engine import hikvision_activation as hik
from hik_emulator import HikEmulator, free_port

_KEYS = hik.KeyCache()  # RSA 3072 на чистом Python — секунды; один на модуль тестов


def shared_keys() -> hik.KeyCache:
    return _KEYS


class CryptoTest(unittest.TestCase):
    def test_aes_known_answers(self) -> None:
        # FIPS-197 C.1 / C.3 и SP 800-38A F.2.1 (CBC-AES128).
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual("69c4e0d86a7b0430d8cdb78070b4c55a", hik.aes_encrypt(
            bytes.fromhex("000102030405060708090a0b0c0d0e0f"), plain, pad=False).hex())
        self.assertEqual("8ea2b7ca516745bfeafc49904b496089", hik.aes_encrypt(
            bytes(range(32)), plain, pad=False).hex())
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes(range(16))
        text = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
        cipher = hik.aes_encrypt(key, text, iv=iv, pad=False)
        self.assertEqual("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2",
                         cipher.hex())
        self.assertEqual(text, hik.aes_decrypt(key, cipher, iv=iv, pad=False))

    def test_password_roundtrip_both_modes(self) -> None:
        challenge = "00112233445566778899aabbccddeeff"
        for iv in (None, bytes(range(16))):
            encrypted = hik.encrypt_password(challenge, "Pa55-word", iv)
            # формат веб-клиента: base64 от hex-строки шифртекста
            self.assertRegex(base64.b64decode(encrypted).decode(), r"^[0-9a-f]+$")
            self.assertEqual("Pa55-word", hik.decrypt_password(challenge, encrypted, iv))

    def test_rsa_challenge_roundtrip(self) -> None:
        key = hik.RsaKey.generate(1024)
        self.assertEqual(1024, key.n.bit_length())
        public = base64.b64decode(key.public_b64).decode()
        self.assertEqual(key.n, int(public, 16))  # base64(hex(n)), как у веб-клиента
        cipher = hik.rsa_encrypt_pkcs1(key.n, b"0123456789abcdef0123456789abcdef")
        wire = base64.b64encode(format(cipher, "x").encode()).decode()
        self.assertEqual("0123456789abcdef0123456789abcdef", key.decrypt_challenge(wire))
        with self.assertRaises(hik.ActivationError):
            key.decrypt_challenge(base64.b64encode(b"zz").decode())


class PasswordRulesTest(unittest.TestCase):
    def test_rules(self) -> None:
        self.assertIsNone(hik.password_problem("Kamera2026"))
        self.assertEqual("password_length", hik.password_problem("Ab1"))
        self.assertEqual("password_length", hik.password_problem("Ab1" * 6))
        self.assertEqual("password_weak", hik.password_problem("abcdefghij"))
        self.assertEqual("password_charset", hik.password_problem("Abc 12345"))
        self.assertEqual("password_charset", hik.password_problem("Пароль12345"))
        self.assertEqual("password_has_user", hik.password_problem("Admin12345"))

    def test_generated_passwords_are_strong(self) -> None:
        seen = set()
        for _ in range(300):
            password = hik.generate_password()
            seen.add(password)
            self.assertIsNone(hik.password_problem(password))
            self.assertIsNone(hik.password_problem(password, hik.ONVIF_USER))
            self.assertEqual(hik.GENERATED_LENGTH, len(password))
            for group in ("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                          "0123456789", hik.GENERATED_SPECIALS):
                self.assertTrue(any(ch in group for ch in password), password)
        self.assertEqual(300, len(seen))


class SadpTest(unittest.TestCase):
    def test_parse_reply(self) -> None:
        reply = (b'<?xml version="1.0" encoding="UTF-8"?><ProbeMatch><Uuid>X</Uuid>'
                 b"<Types>inquiry</Types><DeviceType>139</DeviceType>"
                 b"<DeviceDescription>DS-2CD2543G2-IS</DeviceDescription>"
                 b"<IPv4Address>192.0.2.64</IPv4Address><Activated>false</Activated></ProbeMatch>")
        self.assertEqual({"host": "192.0.2.64", "activated": False, "model": "DS-2CD2543G2-IS",
                          "mac": ""}, hik.parse_sadp(reply))
        self.assertIsNone(hik.parse_sadp(reply.replace(b"192.0.2.64", b"8.8.8.8")))
        self.assertIsNone(hik.parse_sadp(b"<ProbeMatch><IPv4Address>192.0.2.64</IPv4Address>"
                                         b"</ProbeMatch>"))
        self.assertIsNone(hik.parse_sadp(b"not xml"))


class EmulatedCameras(unittest.TestCase):
    """Мост движка + эмуляторы на 127.0.0.2… с общим портом ISAPI."""

    def setUp(self) -> None:
        from cctv.engine import cctv_bridge

        self.module = cctv_bridge
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.registry = self.tmp / "state" / "cameras.json"
        env = {"CCTV_REGISTRY_FILE": str(self.registry),
               "CCTV_REGISTRY_SEED": str(self.tmp / "none.json"),
               "CCTV_STATE_DIR": str(self.tmp / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        pause = mock.patch.object(cctv_bridge, "ACTIVATION_PROBE_PAUSE_SEC", 0.1)
        pause.start()
        self.addCleanup(pause.stop)
        self.port = free_port()
        self.bridge = cctv_bridge.Bridge({"cameras": []}, self.tmp / "spool", "http://127.0.0.1:1")
        self.bridge.activation_port = self.port
        self.bridge.activation_keys = shared_keys

    def camera(self, last_octet: int, **kwargs) -> HikEmulator:
        emulator = HikEmulator(f"127.0.0.{last_octet}", self.port, **kwargs).start()
        self.addCleanup(emulator.stop)
        return emulator

    def run_job(self, hosts, **request) -> dict:
        started = self.bridge.start_activation({"hosts": hosts, **request})
        self.assertTrue(started["ok"], started)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            # Пока задание идёт, пароля в ответе нет.
            status = self.bridge.activation_status(started["activation_id"])
            if status["status"] == "done":
                return status
            self.assertNotIn("password", status)
            time.sleep(0.2)
        self.fail("задание активации не завершилось")

    def vault(self) -> dict:
        return json.loads(self.bridge.activation_vault.read_text())["hosts"]


class ProtocolTest(EmulatedCameras):
    def test_status_without_password(self) -> None:
        v3, legacy = self.camera(2), self.camera(3, protocol="legacy")
        self.assertEqual(hik.Status(False, "v3"), hik.activation_status(v3.base))
        self.assertEqual(hik.Status(False, "legacy"), hik.activation_status(legacy.base))
        self.assertIsNone(hik.activation_status(f"http://127.0.0.9:{self.port}", timeout=1).activated)

    def test_scan_marks_inactive_hikvision(self) -> None:
        emulator = self.camera(2)
        # identify спрашивает :80/:8080 — у эмулятора свой порт, подставляем его.
        with mock.patch.object(discovery, "ACTIVATION_WEB_PORTS", (self.port,)), \
                mock.patch.object(discovery, "ONVIF_PORTS", ()):
            candidate = discovery.identify(
                discovery.Candidate(host=emulator.ip, ports=[self.port]), timeout=2)
        self.assertEqual("hikvision", candidate.vendor)
        self.assertIs(False, candidate.activated)
        self.assertEqual("v3", candidate.activation)
        self.assertIs(False, candidate.as_dict()["activated"])

    def test_v3_activation(self) -> None:
        emulator = self.camera(2)
        hik.activate(emulator.base, "Kamera-2026", "v3", keys=shared_keys())
        self.assertTrue(emulator.activated)
        self.assertEqual("Kamera-2026", emulator.admin_password)
        self.assertEqual({"model": emulator.model, "serial": emulator.model + "EMU0001"},
                         hik.verify_login(emulator.base, "admin", "Kamera-2026"))

    def test_legacy_activation(self) -> None:
        emulator = self.camera(3, protocol="legacy")
        hik.activate(emulator.base, "Kamera-2026", "legacy", keys=shared_keys())
        self.assertEqual("Kamera-2026", emulator.admin_password)
        self.assertIn("PUT /ISAPI/System/activate", emulator.log)

    def test_v3_camera_refuses_legacy_put(self) -> None:
        emulator = self.camera(2)
        with self.assertRaises(hik.ActivationError) as caught:
            hik.activate(emulator.base, "Kamera-2026", "legacy", keys=shared_keys())
        self.assertEqual("challenge_refused", caught.exception.code)
        self.assertFalse(emulator.activated)

    def test_weak_password_never_reaches_camera(self) -> None:
        emulator = self.camera(2)
        with self.assertRaises(hik.ActivationError) as caught:
            hik.activate(emulator.base, "password", "v3", keys=shared_keys())
        self.assertEqual("password_weak", caught.exception.code)
        self.assertEqual([], emulator.log)

    def test_wrong_password_is_tried_once(self) -> None:
        emulator = self.camera(2)
        hik.activate(emulator.base, "Kamera-2026", "v3", keys=shared_keys())
        with self.assertRaises(hik.ActivationError):
            hik.verify_login(emulator.base, "admin", "Other-2026")
        self.assertEqual(1, emulator.failed_logins)  # одна попытка, не пять

    def test_onvif_enable_keeps_live_body(self) -> None:
        emulator = self.camera(2)
        hik.activate(emulator.base, "Kamera-2026", "v3", keys=shared_keys())
        admin = ("admin", "Kamera-2026")
        hik.enable_onvif(emulator.base, admin)
        self.assertTrue(emulator.onvif_enabled)
        puts = emulator.log.count("PUT /ISAPI/System/Network/Integrate")
        hik.enable_onvif(emulator.base, admin)  # повтор — без лишнего PUT
        self.assertEqual(puts, emulator.log.count("PUT /ISAPI/System/Network/Integrate"))
        hik.ensure_onvif_user(emulator.base, admin, "dozorcam", "Onvif-2026x")
        hik.ensure_onvif_user(emulator.base, admin, "dozorcam", "Onvif-2026y")
        self.assertEqual({"dozorcam": ("1", "Onvif-2026y")}, emulator.onvif_users)


class ActivationJobTest(EmulatedCameras):
    def test_single_v3_camera_end_to_end_into_registry(self) -> None:
        emulator = self.camera(2)
        result = self.run_job([emulator.ip], password="Kamera-2026")
        self.assertEqual("Kamera-2026", result["password"])
        [camera] = result["results"]
        self.assertEqual("activated", camera["outcome"])
        self.assertTrue(camera["onvif"] and camera["stream"], camera)
        self.assertIn(hik.ONVIF_USER, emulator.onvif_users)
        self.assertEqual(0, emulator.failed_logins)
        # Сводка — без паролей; поток переписан на реальный адрес камеры.
        summary = camera["summary"]
        self.assertNotIn("Kamera-2026", json.dumps(result["results"]))
        self.assertIn(f"rtsp://***@{emulator.ip}:{emulator.rtsp_port}/Streaming/Channels/101",
                      summary["main_url"])
        # Повторное чтение пароль уже не отдаёт.
        again = self.bridge.activation_status(result["activation_id"])
        self.assertNotIn("password", again)
        added = self.bridge.add_camera({"camera_id": "hik-2", "title": "Hik 2",
                                        "probe_token": camera["probe_token"]})
        self.assertTrue(added["ok"], added)
        record = json.loads(self.registry.read_text())["cameras"][0]
        onvif_password = emulator.onvif_users[hik.ONVIF_USER][1]
        self.assertTrue(record["rtsp_url"].startswith(f"rtsp://{hik.ONVIF_USER}:"))
        self.assertIn("profile=Profile_1", record["rtsp_url"])
        self.assertIn("profile=Profile_2", record["detect_rtsp_url"])
        self.assertTrue(discovery.rtsp_ok(record["rtsp_url"]))
        self.assertEqual(onvif_password, record["snapshot_password"])
        # Хранилище: 0600, пароль admin и ONVIF, итоговое состояние.
        self.assertEqual(0o600, self.bridge.activation_vault.stat().st_mode & 0o777)
        entry = self.vault()[emulator.ip]
        self.assertEqual(("Kamera-2026", onvif_password, "onvif_ready"),
                         (entry["admin_password"], entry["onvif_password"], entry["state"]))

    def test_legacy_camera_job(self) -> None:
        emulator = self.camera(3, protocol="legacy")
        result = self.run_job([emulator.ip], generate=True)
        [camera] = result["results"]
        self.assertEqual(("activated", "legacy", True), (camera["outcome"], camera["protocol"],
                                                         camera["stream"]))
        self.assertEqual(emulator.admin_password, result["password"])
        self.assertIsNone(hik.password_problem(result["password"]))
        self.assertTrue(result["generated"])

    def test_batch_with_partial_failure_keeps_password(self) -> None:
        good = self.camera(2)
        legacy = self.camera(3, protocol="legacy")
        rejecting = self.camera(4, fail="reject")
        lost = self.camera(5, fail="lost_reply")
        wrong = self.camera(6, fail="wrong_password")
        no_onvif = self.camera(7, fail="no_onvif")
        active = self.camera(8)
        active.activated, active.admin_password = True, "Already-2026"
        hosts = [c.ip for c in (good, legacy, rejecting, lost, wrong, no_onvif, active)]
        hosts.append("127.0.0.9")  # не отвечает
        result = self.run_job(hosts, generate=True)
        password = result["password"]
        by_host = {r["host"]: r for r in result["results"]}
        self.assertEqual(hosts, [r["host"] for r in result["results"]])  # итог по каждой
        self.assertEqual(("activated", True), (by_host[good.ip]["outcome"], by_host[good.ip]["stream"]))
        self.assertEqual("activated", by_host[legacy.ip]["outcome"])
        self.assertEqual(("failed", "activate_refused"),
                         (by_host[rejecting.ip]["outcome"], by_host[rejecting.ip]["stage"]))
        # Ответ потерян, но камера активирована нашим паролем — это успех,
        # проверенный входом, а не «сбой» с потерянным паролем.
        self.assertEqual("activated", by_host[lost.ip]["outcome"])
        self.assertTrue(by_host[lost.ip]["stream"])
        self.assertEqual(("unverified", "login_refused"),
                         (by_host[wrong.ip]["outcome"], by_host[wrong.ip]["stage"]))
        self.assertEqual(1, wrong.failed_logins)  # ни одного повтора входа
        self.assertEqual(("activated", False, "onvif_enable"),
                         (by_host[no_onvif.ip]["outcome"], by_host[no_onvif.ip]["onvif"],
                          by_host[no_onvif.ip]["stage"]))
        self.assertEqual("already_active", by_host[active.ip]["outcome"])
        self.assertEqual("Already-2026", active.admin_password)  # чужую камеру не тронули
        self.assertEqual(("failed", "not_hikvision"),
                         (by_host["127.0.0.9"]["outcome"], by_host["127.0.0.9"]["stage"]))
        # Один пароль на весь пакет — у всех активированных он один и тот же.
        for camera in (good, legacy, lost, no_onvif):
            self.assertEqual(password, camera.admin_password)
        for camera in (good, legacy, rejecting, lost, wrong, no_onvif):
            self.assertLessEqual(camera.activation_calls, 1)
            self.assertFalse(camera.locked)
        vault = self.vault()
        self.assertEqual({good.ip, legacy.ip, rejecting.ip, lost.ip, wrong.ip, no_onvif.ip}, set(vault))
        self.assertEqual("not_activated", vault[rejecting.ip]["state"])
        self.assertEqual("unverified", vault[wrong.ip]["state"])
        self.assertEqual("activated", vault[no_onvif.ip]["state"])
        self.assertTrue(all(v["admin_password"] == password for v in vault.values()))

    def test_nothing_activated_means_no_password(self) -> None:
        rejecting = self.camera(4, fail="reject")
        result = self.run_job([rejecting.ip], generate=True)
        self.assertNotIn("password", result)
        self.assertEqual("failed", result["results"][0]["outcome"])

    def test_password_is_on_disk_before_the_network_call(self) -> None:
        emulator = self.camera(2)
        seen = {}
        original = hik.activate

        def spy(base, password, protocol, **kwargs):
            seen["vault"] = json.loads(self.bridge.activation_vault.read_text())["hosts"][emulator.ip]
            seen["calls"] = list(emulator.log)
            return original(base, password, protocol, **kwargs)

        with mock.patch.object(hik, "activate", spy):
            self.run_job([emulator.ip], password="Kamera-2026")
        self.assertEqual(("Kamera-2026", "activating"),
                         (seen["vault"]["admin_password"], seen["vault"]["state"]))
        self.assertEqual(["GET /SDK/activateStatus"], seen["calls"])

    def test_request_validation(self) -> None:
        start = self.bridge.start_activation
        self.assertEqual("bad_hosts", start({"hosts": ["8.8.8.8"], "generate": True})["error_code"])
        self.assertEqual("bad_hosts", start({"hosts": "127.0.0.2", "generate": True})["error_code"])
        self.assertEqual("bad_hosts", start({"hosts": [], "generate": True})["error_code"])
        self.assertEqual("password_weak", start({"hosts": ["127.0.0.2"], "password": "aaaaaaaaaa"})
                         ["error_code"])
        self.assertEqual("password_length", start({"hosts": ["127.0.0.2"]})["error_code"])

    def test_one_batch_at_a_time(self) -> None:
        self.camera(2)
        first = self.bridge.start_activation({"hosts": ["127.0.0.2"], "generate": True})
        second = self.bridge.start_activation({"hosts": ["127.0.0.2"], "generate": True})
        self.assertTrue(first["ok"])
        self.assertEqual("busy", second["error_code"])
        deadline = time.monotonic() + 60
        while self.bridge.activation_status(first["activation_id"])["status"] != "done":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.2)


class BotThroughEngineTest(EmulatedCameras, unittest.IsolatedAsyncioTestCase):
    """Сквозной путь: кнопка в боте → HTTP моста → эмуляторы камер → реестр."""

    def setUp(self) -> None:
        import sys
        import threading

        import httpx

        super().setUp()
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bot"))
        self.addCleanup(sys.path.remove, sys.path[0])
        from test_cctv_contract_20260824 import make_config
        from test_cctv_flow_20260824 import OWNER
        from test_cctv_provisioning_20260904 import DeletingTelegram
        from cctv.bot import bot as bot_module
        from cctv.bot.bot import CctvBot
        from cctv.bot.bridge import Bridge as BotBridge
        from cctv.bot.state import State

        self.owner, self.bot_module = OWNER, bot_module
        port = free_port()
        server = self.module.build_server(self.bridge, env={"CCTV_BIND": "127.0.0.1",
                                                            "CCTV_PORT": str(port)})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = make_config(self.tmp)
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30)
        self.addCleanup(client.close)
        state = State(":memory:")
        self.addCleanup(state.close)
        self.tg = DeletingTelegram()
        self.bot = CctvBot(cfg, state, BotBridge(cfg, client=client), self.tg)
        for name, value in (("RESTART_WAIT_SEC", 600), ("ACTIVATION_POLL_SEC", 0.2)):
            self.addCleanup(setattr, bot_module, name, getattr(bot_module, name))
            setattr(bot_module, name, value)

    async def test_activate_all_from_chat(self) -> None:
        good, legacy, rejecting = self.camera(2), self.camera(3, protocol="legacy"), \
            self.camera(4, fail="reject")
        hosts = ",".join(c.ip for c in (good, legacy, rejecting))
        thread = await self.bot.ensure_console()
        token = self.bot.state.issue_callback(self.bot_module.CONSOLE_CAMERA, "actall", 600, hosts)
        await self.bot.on_callback(self.owner, thread, f"cv:actall:{token}")
        await self.bot.on_text(self.owner, thread, "активировать", None, 10)
        await self.bot.on_text(self.owner, thread, "сгенерировать", None, 11)
        self.assertEqual([11], self.tg.deleted)
        await self.bot._activation_task
        password = good.admin_password
        self.assertIsNone(hik.password_problem(password))
        self.assertEqual(password, legacy.admin_password)
        self.assertFalse(rejecting.activated)
        dms = [kw["text"] for name, kw in self.tg.calls
               if name == "send_message" and kw.get("chat_id") == self.owner]
        self.assertEqual(1, len(dms))
        self.assertIn(password, dms[0])
        group = json.dumps([kw for _, kw in self.tg.calls if kw.get("chat_id") != self.owner],
                           ensure_ascii=False, default=str)
        self.assertNotIn(password, group)
        summary = self.tg.of("send_message")[-1]["text"]
        self.assertIn("готово 2 из 3", summary)
        registry = json.loads(self.registry.read_text())["cameras"]
        self.assertEqual(["hik-127-0-0-2", "hik-127-0-0-3"], [c["camera_id"] for c in registry])
        for record in registry:
            self.assertTrue(discovery.rtsp_ok(record["rtsp_url"]), record["camera_id"])
        self.assertEqual(0, good.failed_logins + legacy.failed_logins)


if __name__ == "__main__":
    unittest.main()
