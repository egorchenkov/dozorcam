#!/usr/bin/env python3
"""Заведение, правка и удаление камеры из чата.

Главное обязательство этого пути одно: пароль камеры существует ровно в двух
местах — в сообщении, которое бот стирает сразу, и в теле одного запроса к
мосту. Ни в ответах в чат, ни в журнале, ни в состоянии бота его быть не может.
Здесь это и проверяется, вместе с тем, что кнопка не может завести камеру мимо
подтверждения человеком.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


import httpx  # noqa: E402

from cctv import i18n  # noqa: E402
from cctv.bot import bot as bot_module  # noqa: E402
from cctv.bot.bot import CctvBot, slugify, topic_icon, unique_camera_id  # noqa: E402
from cctv.bot.bridge import Bridge  # noqa: E402
from cctv.bot.state import State  # noqa: E402
from test_cctv_contract_20260824 import BRIDGE, make_config  # noqa: E402
from test_cctv_flow_20260824 import FakeTelegram, OWNER  # noqa: E402

PASSWORD = "s3cr3t-pa$$"
SUMMARY = {
    "host": "192.0.2.11", "vendor": "Hikvision", "model": "DS-2CD2523G0-IS",
    "source": "onvif", "verified": True,
    "main_url": "rtsp://***@192.0.2.11:554/Streaming/Channels/101",
    "sub_url": "rtsp://***@192.0.2.11:554/Streaming/Channels/102",
    "snapshot_url": "http://192.0.2.11/ISAPI/Streaming/channels/101/picture",
    "profiles": [{"name": "MainStream", "encoding": "H264", "width": 1920, "height": 1080,
                  "fps": 25},
                 {"name": "SubStream", "encoding": "H264", "width": 640, "height": 360,
                  "fps": 12}],
}


class DeletingTelegram(FakeTelegram):
    """Bot API плюс удаление сообщения: без него пароль остался бы в чате."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[int] = []

    async def delete_message(self, **kwargs):
        self._record("delete_message", kwargs)
        self.deleted.append(kwargs.get("message_id"))
        return True


class ProvisioningTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.cameras = [{"camera_id": "city", "title": "Город",
                         "site": "Город", "status": "online",
                         "last_frame_at": "2026-09-04T10:00:00Z",
                         "motion": {"state": "watching", "reason": "", "last_motion_at": None}}]
        self.scan_status = {"ok": True, "scan_id": "s1", "status": "done",
                            "networks": ["192.0.2.0/24"],
                            "candidates": [{"host": "192.0.2.11", "ports": [80, 554],
                                            "vendor": "Hikvision", "model": "DS-2CD",
                                            "onvif": True,
                                            "label": "192.0.2.11 · Hikvision DS-2CD"}]}
        self.probe_reply = {"ok": True, "probe_token": "tok-1", "summary": SUMMARY}
        self.add_reply = {"ok": True, "action": "added", "camera_id": "dvor"}
        self.config_reply = {"ok": True, "camera": {
            "camera_id": "city", "title": "Город", "host": "192.0.2.10",
            "username": "admin", "rtsp_url": "rtsp://***@192.0.2.10:554/Streaming/Channels/101",
            "detect_rtsp_url": "rtsp://***@192.0.2.10:554/Streaming/Channels/102",
            "snapshot_url": "http://192.0.2.10/onvif-http/snapshot", "person_detection": True}}
        self.sent: list[tuple[str, dict]] = []

        def handler(request: httpx.Request):
            path = request.url.path
            body = json.loads(request.content) if request.content else {}
            self.sent.append((path, body))
            if path == "/v1/cameras" and request.method == "GET":
                return httpx.Response(200, json={"cameras": self.cameras})
            if path == "/v1/discovery/scans" and request.method == "POST":
                return httpx.Response(200, json={"ok": True, "scan_id": "s1",
                                                 "status": "running",
                                                 "networks": ["192.0.2.0/24"]})
            if path.startswith("/v1/discovery/scans/"):
                return httpx.Response(200, json=self.scan_status)
            if path == "/v1/discovery/probes":
                return httpx.Response(200, json=self.probe_reply)
            if path == "/v1/cameras" and request.method == "POST":
                return httpx.Response(200, json=self.add_reply)
            if path.endswith("/config") and request.method == "GET":
                return httpx.Response(200, json=self.config_reply)
            if path.endswith("/config"):
                return httpx.Response(200, json={"ok": True, "action": "updated"})
            if path.endswith("/delete"):
                return httpx.Response(200, json={"ok": True, "action": "deleted"})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = DeletingTelegram()
        self.logged: list[str] = []
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg,
                           log=self.logged.append)
        self.original_wait = bot_module.RESTART_WAIT_SEC
        self.original_poll = bot_module.SCAN_POLL_SEC
        bot_module.RESTART_WAIT_SEC = 600  # сверку тем после перезапуска в тесте не ждём
        bot_module.SCAN_POLL_SEC = 0
        self.addCleanup(self.restore)

    def restore(self) -> None:
        bot_module.RESTART_WAIT_SEC = self.original_wait
        bot_module.SCAN_POLL_SEC = self.original_poll

    def button(self, camera_id: str, action: str, payload: str | None = None) -> str:
        return f"cv:{action}:{self.state.issue_callback(camera_id, action, 600, payload)}"

    async def console(self) -> int:
        return await self.bot.ensure_console()

    def texts(self) -> str:
        """Всё, что бот сказал в чат: тут пароля быть не может ни в каком виде."""
        return json.dumps(self.tg.calls, ensure_ascii=False, default=str)

    # --- заведение ---------------------------------------------------------
    async def test_full_add_flow_from_scan_to_registry(self) -> None:
        thread = await self.console()
        found = await self.bot.on_callback(OWNER, thread,
                                           self.button(bot_module.CONSOLE_CAMERA, "add"))
        self.assertIn("Ищу камеры", found)
        await self.bot._discover()
        listed = self.tg.of("send_message")[-1]
        self.assertIn("Найдено новых камер: 1", listed["text"])

        # Кандидат выбран — бот просит логин и пароль, но ничего ещё не пишет.
        markup = listed["reply_markup"].inline_keyboard
        chosen = markup[0][0].callback_data
        asked = await self.bot.on_callback(OWNER, thread, chosen)
        self.assertIn("192.0.2.11", asked)
        self.assertIn("удалю", asked)

        answer = await self.bot.on_text(OWNER, thread, f"admin {PASSWORD}", None, 4242)
        self.assertEqual([4242], self.tg.deleted)  # сообщение с паролем стёрто
        self.assertIn("Hikvision", answer)
        self.assertIn("1920×1080", answer)
        self.assertIn("Пришлите имя камеры", answer)
        probe = next(body for path, body in self.sent if path == "/v1/discovery/probes")
        self.assertEqual({"host": "192.0.2.11", "username": "admin", "password": PASSWORD},
                         probe)

        created = await self.bot.on_text(OWNER, thread, "Двор у ворот", None, 4243)
        self.assertIn("dvor-u-vorot", created)
        add = next(body for path, body in self.sent
                   if path == "/v1/cameras" and "probe_token" in body)
        self.assertEqual({"camera_id": "dvor-u-vorot", "title": "Двор у ворот",
                          "site": "Двор у ворот", "probe_token": "tok-1"}, add)
        # Пароль не ушёл ни в один текст чата и ни в один журнальный вызов.
        self.assertNotIn(PASSWORD, self.texts())
        self.assertNotIn(PASSWORD, " ".join(self.logged))
        self.assertNotIn(PASSWORD, json.dumps(self.sent, ensure_ascii=False).replace(
            json.dumps(probe, ensure_ascii=False), ""))

    async def test_password_message_is_deleted_even_when_it_is_malformed(self) -> None:
        """Одно слово вместо пары — всё равно возможный пароль: стираем и переспрашиваем."""
        thread = await self.console()
        await self.bot.on_callback(OWNER, thread,
                                   self.button(bot_module.CONSOLE_CAMERA, "cand", "192.0.2.11"))
        answer = await self.bot.on_text(OWNER, thread, PASSWORD, None, 77)
        self.assertEqual([77], self.tg.deleted)
        self.assertNotIn(PASSWORD, answer)
        self.assertIn("логин пароль", answer)
        self.assertEqual([], [p for p, _ in self.sent if p == "/v1/discovery/probes"])
        # Ожидание ввода перевыставлено: следующая попытка не потеряет контекст.
        pending = self.state.take_input(OWNER)
        self.assertEqual("creds|192.0.2.11||", pending[1])

    async def test_rejected_credentials_do_not_create_a_camera(self) -> None:
        thread = await self.console()
        self.probe_reply = {"ok": False, "error": "the camera did not accept the login or password",
                            "error_key": "discovery.auth_failed", "error_params": {}}
        await self.bot.on_callback(OWNER, thread,
                                   self.button(bot_module.CONSOLE_CAMERA, "cand", "192.0.2.11"))
        answer = await self.bot.on_text(OWNER, thread, f"admin {PASSWORD}", None, 5)
        self.assertIn("не приняла логин", answer)
        self.assertEqual([], [p for p, b in self.sent if p == "/v1/cameras" and b])
        self.assertNotIn(PASSWORD, self.texts())

    async def test_empty_scan_says_so_instead_of_offering_nothing(self) -> None:
        self.scan_status = {"ok": True, "scan_id": "s1", "status": "done", "candidates": []}
        await self.console()
        await self.bot._discover()
        self.assertIn("Камер в сети не нашлось", self.tg.of("send_message")[-1]["text"])

    async def test_registered_camera_is_not_offered_as_new(self) -> None:
        """Автопоиск видит уже заведённые камеры: кнопки «добавить» у них быть не
        может, иначе повторный поиск предлагал бы завести дубль (04.09 — так
        бот предложил городскую камеру 192.0.2.10)."""
        self.scan_status["candidates"].insert(0, {
            "host": "192.0.2.10", "ports": [80, 554], "vendor": "Hikvision",
            "model": "DS-2CD2523G0-IS", "onvif": True, "registered_camera_id": "city",
            "label": "192.0.2.10 · Hikvision DS-2CD2523G0-IS"})
        await self.console()
        await self.bot._discover()
        listed = self.tg.of("send_message")[-1]
        self.assertIn("Найдено новых камер: 1", listed["text"])
        self.assertIn("Пропущено как уже заведённые: city (192.0.2.10)",
                      listed["text"])
        markup = listed["reply_markup"].inline_keyboard
        hosts = [b.callback_data for row in markup for b in row]
        self.assertEqual(3, len(hosts))  # одна новая камера + «искать снова» + «ввести адрес»
        asked = await self.bot.on_callback(OWNER, await self.console(), hosts[0])
        self.assertIn("192.0.2.11", asked)  # кнопка ведёт к новой, не к заведённой

    async def test_scan_with_only_registered_cameras_reports_no_new(self) -> None:
        self.scan_status["candidates"] = [{
            "host": "192.0.2.10", "ports": [80, 554], "onvif": True,
            "registered_camera_id": "city", "label": "192.0.2.10"}]
        await self.console()
        await self.bot._discover()
        text = self.tg.of("send_message")[-1]["text"]
        self.assertIn("Новых камер не нашлось", text)
        self.assertIn("city (192.0.2.10)", text)

    # --- правка и удаление --------------------------------------------------
    async def test_setup_card_shows_registry_without_password(self) -> None:
        await self.bot.ensure_topic("city", "Город", "Город")
        topic = self.state.topic_for("city")
        answer = await self.bot.on_callback(OWNER, topic.thread_id,
                                            self.button("city", "setup"))
        self.assertIn("Карточка настройки", answer)
        card = self.tg.of("send_message")[-1]["text"]
        self.assertIn("Логин: admin", card)
        self.assertIn("rtsp://***@192.0.2.10", card)
        self.assertIn("Детекция людей: включена", card)

    async def test_password_change_of_an_existing_camera(self) -> None:
        await self.bot.ensure_topic("city", "Город", "Город")
        topic = self.state.topic_for("city")
        await self.bot.on_callback(OWNER, topic.thread_id,
                                   self.button("city", "cand", "192.0.2.10"))
        answer = await self.bot.on_text(OWNER, topic.thread_id, f"admin {PASSWORD}", None, 9)
        self.assertEqual([9], self.tg.deleted)
        self.assertIn("обновлены", answer)
        update = next(body for path, body in self.sent
                      if path.endswith("/config") and body.get("probe_token"))
        self.assertEqual({"probe_token": "tok-1"}, update)
        self.assertNotIn(PASSWORD, self.texts())

    async def test_detection_toggle_goes_to_the_registry(self) -> None:
        await self.bot.ensure_topic("city", "Город", "Город")
        topic = self.state.topic_for("city")
        answer = await self.bot.on_callback(OWNER, topic.thread_id,
                                            self.button("city", "detect", "0"))
        self.assertIn("выключена", answer)
        update = next(body for path, body in self.sent
                      if path.endswith("/config") and "person_detection" in body)
        self.assertEqual({"person_detection": False}, update)

    async def test_deletion_needs_the_confirmation_word(self) -> None:
        await self.bot.ensure_topic("city", "Город", "Город")
        topic = self.state.topic_for("city")
        asked = await self.bot.on_callback(OWNER, topic.thread_id,
                                           self.button("city", "drop"))
        self.assertIn("удалить", asked)
        declined = await self.bot.on_text(OWNER, topic.thread_id, "нет", None, 1)
        self.assertEqual("Удаление отменено.", declined)
        self.assertEqual([], [p for p, _ in self.sent if p.endswith("/delete")])

        await self.bot.on_callback(OWNER, topic.thread_id, self.button("city", "drop"))
        done = await self.bot.on_text(OWNER, topic.thread_id, "удалить", None, 2)
        self.assertIn("удалена из реестра", done)
        self.assertEqual(1, len([p for p, _ in self.sent if p.endswith("/delete")]))
        self.assertEqual("retired", self.state.topic_for("city").status)

    async def test_stranger_cannot_start_provisioning(self) -> None:
        thread = await self.console()
        answer = await self.bot.on_callback(OWNER + 1, thread,
                                            self.button(bot_module.CONSOLE_CAMERA, "add"))
        self.assertEqual("Нет доступа.", answer)
        answer = await self.bot.on_text(OWNER + 1, thread, "admin пароль", None, 3)
        self.assertEqual(i18n.t(bot_module.UNKNOWN_KEY, "ru"), answer)
        self.assertEqual([], self.tg.deleted)

    async def test_provisioning_buttons_do_not_work_outside_their_topic(self) -> None:
        await self.bot.ensure_topic("city", "Город", "Город")
        topic = self.state.topic_for("city")
        answer = await self.bot.on_callback(OWNER, topic.thread_id,
                                            self.button(bot_module.CONSOLE_CAMERA, "add"))
        self.assertEqual("Кнопка не относится к этой теме.", answer)


class CameraIdTest(unittest.TestCase):
    def test_slug_and_uniqueness(self) -> None:
        self.assertEqual("gorod", slugify("Город"))
        self.assertEqual("dvor-u-vorot", slugify("Двор у ворот"))
        self.assertEqual("", slugify("!!!"))
        self.assertEqual("dvor", unique_camera_id("dvor", set()))
        self.assertEqual("dvor-2", unique_camera_id("dvor", {"dvor"}))
        self.assertEqual("dvor-3", unique_camera_id("dvor", {"dvor", "dvor-2"}))


class TopicIconTest(unittest.TestCase):
    """Иконка темы — по площадке: на даче камер будет несколько."""

    def test_site_decides_the_icon(self) -> None:
        house, city = bot_module.SITE_ICONS[0][1], bot_module.SITE_ICONS[-1][1]
        self.assertEqual(city, topic_icon("Город", "Город", "city"))
        self.assertEqual(house, topic_icon("Дача", "Дача", "dacha"))
        self.assertEqual(house, topic_icon("Ворота", "Дача", "dacha-gate"))
        self.assertEqual(house, topic_icon("Front door", "Country house", "front"))
        self.assertEqual(bot_module.TOPIC_ICON_CAMERA, topic_icon("Новая", "", "cam-1"))


if __name__ == "__main__":
    unittest.main()
