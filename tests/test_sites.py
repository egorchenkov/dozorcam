"""Отказоустойчивость, шаг 3: площадки — один бот, несколько движков.

Интеграция: настоящие мосты движков (HTTP-серверы cctv_bridge) и приёмник
событий бота говорят по mTLS с CA установки; ядро бота настоящее, подменён
только Bot API. Сначала установка одномоторная — один движок за обе камеры,
бот заводит им темы. Потом камеры разъезжаются по двум площадкам, у каждой
свой движок; бот перезапускается на той же базе с CCTV_SITES. Проверяется:
новых тем нет, события обоих движков приходят в прежние темы своих камер,
медиа бот забирает с моста той площадки, откуда пришло событие, а управление
камерой уходит мосту её площадки. Отдельно — обратная совместимость: конфиг
без площадок и с одной площадкой работает как прежде.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx

from cctv.bot import config as bot_config
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge as BotBridge, BridgeError
from cctv.bot.events import EventServer
from cctv.bot.sites import UNREACHABLE, Sites, connect
from cctv.bot.state import State
from cctv.engine import cctv_bridge as engine

from tests.test_internal_link import LinkMixin, free_port

sys.path.insert(0, str(pathlib.Path(__file__).parent / "bot"))
from test_cctv_flow_20260824 import FakeTelegram  # noqa: E402

OWNER = 7
PORCH = {"camera_id": "porch", "title": "Porch", "site": "", "rtsp_url": "rtsp://192.0.2.1/porch"}
GATE = {"camera_id": "gate", "title": "Gate", "site": "", "rtsp_url": "rtsp://192.0.2.2/gate"}
FRAME = {"porch": b"\xff\xd8 porch frame", "gate": b"\xff\xd8 gate frame"}


@unittest.skipUnless(shutil.which("openssl"), "нужен openssl для тестовой PKI")
class TwoEnginesOneBotTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        LinkMixin.make_pki(self)
        self.events_port = free_port()
        self.db = str(self.dir / "bot.sqlite3")
        (self.dir / "bot-tmp").mkdir()
        self.servers: dict = {}

    pem = LinkMixin.pem

    def start_engine(self, name: str, cameras: list[dict], events: str = "") -> tuple[engine.Bridge, str]:
        """Мост движка площадки: свой порт, свой state, события — в приёмник бота по mTLS."""
        port = free_port()
        env = {
            "CCTV_INTERNAL_TLS": "1", "CCTV_BIND": "127.0.0.1", "CCTV_PORT": str(port),
            "CCTV_SERVER_CERT": self.pem("server.crt"), "CCTV_SERVER_KEY": self.pem("server.key"),
            "CCTV_CLIENT_CA": self.pem("ca.crt"),
            "CCTV_EVENTS_URL": events or f"https://127.0.0.1:{self.events_port}/v1/events",
            "CCTV_EVENTS_CERT": self.pem("client.crt"), "CCTV_EVENTS_KEY": self.pem("client.key"),
            "CCTV_EVENTS_CA": self.pem("ca.crt"),
            "CCTV_STATE_DIR": str(self.dir / name / "state"), "CCTV_BUFFER_DIR": str(self.dir / name / "buffer"),
        }
        url = f"https://127.0.0.1:{port}"
        with mock.patch.dict(os.environ, env):
            bridge = engine.Bridge({"cameras": cameras}, self.dir / name / "store", url)
            server = engine.build_server(bridge, env)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.servers[name] = server
        return bridge, url

    def bot_cfg(self, bridge_url: str, sites: str = "") -> bot_config.Config:
        env = {
            "CCTV_BOT_TOKEN": "0:test", "CCTV_CHAT_ID": "-100500", "CCTV_ALLOWED_USER_IDS": str(OWNER),
            "CCTV_LANG": "en", "CCTV_INTERNAL_TLS": "1", "CCTV_BRIDGE_URL": bridge_url,
            "CCTV_BRIDGE_CLIENT_CERT": self.pem("client.crt"), "CCTV_BRIDGE_CLIENT_KEY": self.pem("client.key"),
            "CCTV_BRIDGE_CA": self.pem("ca.crt"),
            "CCTV_EVENTS_HOST": "127.0.0.1", "CCTV_EVENTS_PORT": str(self.events_port),
            "CCTV_EVENTS_CERT": self.pem("server.crt"), "CCTV_EVENTS_KEY": self.pem("server.key"),
            "CCTV_EVENTS_CLIENT_CA": self.pem("ca.crt"),
            "CCTV_STATE_DIR": str(self.dir), "CCTV_RUNTIME_DIR": str(self.dir / "bot-tmp"),
            # Каждое событие — отдельным постом: склейка за минуту спрятала бы второе в подпись первого.
            "CCTV_EVENT_MERGE_SEC": "0",
        }
        if sites:
            env["CCTV_SITES"] = sites
        return bot_config.load(env)

    def start_bot(self, cfg) -> tuple[CctvBot, FakeTelegram]:
        """Бот как в main: клиент мостов по конфигу, приёмник событий отдаёт их ядру."""
        state = State(self.db)
        bridge = connect(cfg)
        tg = FakeTelegram()
        bot = CctvBot(cfg, state, bridge, tg)
        loop = asyncio.get_running_loop()
        receiver = EventServer(cfg, lambda event: asyncio.run_coroutine_threadsafe(bot.on_event(event), loop))
        receiver.serve_in_thread()

        def stop():
            receiver.shutdown()
            receiver.server_close()
            bridge.close()
            state.close()

        self.stops.append(stop)
        return bot, tg

    async def asyncSetUp(self) -> None:
        self.stops: list = []

    async def asyncTearDown(self) -> None:
        for stop in reversed(self.stops):
            stop()

    def stop_bot(self) -> None:
        self.stops.pop()()

    async def wait_for(self, predicate, what: str, timeout: float = 15.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.05)
        self.fail(f"не дождался: {what}")

    async def motion(self, bridge: engine.Bridge, camera_id: str) -> None:
        # Событие шлёт сам мост движка — тем же путём, что и детектор в проде.
        await asyncio.to_thread(bridge.motion, bridge.cameras[camera_id], snapshot_body=FRAME[camera_id])

    async def test_bot_serves_two_engines_and_keeps_the_topics(self) -> None:
        # 1. Одномоторная установка: один движок за обе камеры, бот заводит им темы.
        single, single_url = self.start_engine("single", [PORCH, GATE])
        bot, tg = self.start_bot(self.bot_cfg(single_url))
        self.assertIsInstance(bot.bridge, BotBridge)
        await bot.sync_registry()
        topics = {camera_id: bot.routes.camera_topic(camera_id).thread_id for camera_id in ("porch", "gate")}
        self.assertEqual(2, len(set(topics.values())))
        await self.motion(single, "gate")
        await self.wait_for(lambda: tg.of("send_photo"), "событие одномоторной установки")
        self.assertEqual(topics["gate"], tg.of("send_photo")[0]["message_thread_id"])
        self.stop_bot()

        # 2. Камеры разъехались по площадкам: у каждой свой движок со своим набором камер.
        dacha, dacha_url = self.start_engine("dacha", [PORCH])
        city, city_url = self.start_engine("city", [GATE])
        cfg = self.bot_cfg(dacha_url, f"dacha city={city_url}")
        self.assertEqual([("dacha", dacha_url), ("city", city_url)],
                         [(site.name, site.bridge_url) for site in cfg.sites])
        bot, tg = self.start_bot(cfg)
        self.assertIsInstance(bot.bridge, Sites)
        await bot.sync_registry()
        self.assertEqual([], tg.of("create_forum_topic"), "площадки завели новые темы")
        self.assertEqual({"porch": "dacha", "gate": "city"},
                         {camera_id: bot.bridge.site_of(camera_id) for camera_id in topics})

        # 3. Оба движка одновременно шлют события — каждое в прежнюю тему своей камеры.
        await asyncio.gather(self.motion(dacha, "porch"), self.motion(city, "gate"))
        await self.wait_for(lambda: len(tg.of("send_photo")) == 2, "события двух движков")
        got = {call["message_thread_id"] for call in tg.of("send_photo")}
        self.assertEqual(set(topics.values()), got)
        self.assertEqual([], tg.of("create_forum_topic"))

        # 4. Управление камерой уходит мосту её площадки, соседняя площадка не трогается.
        await asyncio.to_thread(bot.bridge.set_camera_state, "gate", "pause")
        status = {camera.camera_id: camera.status for camera in await asyncio.to_thread(bot.bridge.cameras)}
        self.assertEqual("paused", status["gate"])
        self.assertNotEqual("paused", status["porch"])
        self.assertIn("gate", city.overrides())
        self.assertEqual({}, dacha.overrides())

        # 5. Камеры без своей локации — в разделе карты своей площадки.
        sections = dict(bot._sections(await asyncio.to_thread(bot.bridge.cameras)))
        self.assertEqual({"dacha": ["porch"], "city": ["gate"]},
                         {name: [camera.camera_id for camera in cameras] for name, cameras in sections.items()})

        # 6. /add спрашивает площадку; выбранная получает поиск камер.
        reply = await bot.on_add(OWNER, [], chat_id=-100500)
        self.assertEqual(bot._t("add.site_menu_sent"), reply)
        menu = tg.of("send_message")[-1]
        buttons = [button for row in menu["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(["dacha", "city"], [button.text for button in buttons])
        with mock.patch.object(bot, "_discover", new=mock.AsyncMock()) as discover:
            answer = await bot.on_callback(OWNER, menu.get("message_thread_id"), buttons[1].callback_data)
            await asyncio.sleep(0)
        self.assertEqual(bot._t("add.searching_site", site="city"), answer)
        self.assertEqual("city", bot.bridge.add_site)
        discover.assert_called_once()

    async def test_silent_site_keeps_its_cameras_and_does_not_stop_the_other(self) -> None:
        dacha, dacha_url = self.start_engine("dacha", [PORCH])
        city, city_url = self.start_engine("city", [GATE])
        bot, tg = self.start_bot(self.bot_cfg(dacha_url, f"dacha city={city_url}"))
        await bot.sync_registry()
        # Движок city замолчал: его мост больше не отвечает.
        self.servers["city"].shutdown()
        self.servers["city"].server_close()
        cameras = {camera.camera_id: camera.status for camera in await asyncio.to_thread(bot.bridge.cameras)}
        self.assertEqual(UNREACHABLE, cameras["gate"])
        self.assertNotEqual(UNREACHABLE, cameras["porch"])
        await bot.sync_registry()
        self.assertEqual([], tg.of("close_forum_topic"), "молчащая площадка сняла свои камеры")
        # Событие живой площадки доходит, пока соседняя молчит.
        await self.motion(dacha, "porch")
        await self.wait_for(lambda: tg.of("send_photo"), "событие живой площадки")
        self.assertEqual(bot.routes.camera_topic("porch").thread_id, tg.of("send_photo")[0]["message_thread_id"])

    async def test_site_engine_reaches_the_bot_wherever_it_runs(self) -> None:
        """Движок площадки вне слоя знает список приёмников: событие берёт тот бот, что запущен."""
        armed = f"https://127.0.0.1:{free_port()}/v1/events"      # бот этого узла наготове: порт закрыт
        running = f"https://127.0.0.1:{self.events_port}/v1/events"
        city, city_url = self.start_engine("city", [GATE], events=f"{armed} {running}")
        engine.check_events_url(city)
        bot, tg = self.start_bot(self.bot_cfg(city_url, f"city={city_url}"))
        await bot.sync_registry()
        await self.motion(city, "gate")
        await self.wait_for(lambda: tg.of("send_photo"), "событие через второй приёмник")
        self.assertEqual(bot.routes.camera_topic("gate").thread_id, tg.of("send_photo")[0]["message_thread_id"])
        # Каждый адрес списка проверяется: открытый http за пределы узла — отказ старта.
        leaky, _ = self.start_engine("leaky", [GATE], events=f"{running} http://192.0.2.5:8781/v1/events")
        with self.assertRaises(SystemExit):
            engine.check_events_url(leaky)


class OneSiteCompatibilityTest(unittest.IsolatedAsyncioTestCase):
    """Конфиг без площадок и с одной площадкой — как в одномоторной установке."""

    def env(self, **extra) -> dict:
        return {"CCTV_BOT_TOKEN": "0:test", "CCTV_CHAT_ID": "-100500", "CCTV_ALLOWED_USER_IDS": str(OWNER),
                "CCTV_STATE_DIR": "/tmp/cctv-sites-test", "CCTV_RUNTIME_DIR": "/tmp/cctv-sites-test",
                "CCTV_LANG": "en", **extra}

    def test_config_without_sites_uses_the_single_bridge(self) -> None:
        cfg = bot_config.load(self.env())
        self.assertEqual((), cfg.sites)
        client = connect(cfg)
        self.addCleanup(client.close)
        self.assertIs(BotBridge, type(client))
        self.assertEqual(bot_config.DEFAULT_BRIDGE_URL, client.cfg.bridge_base_url)

    def test_site_list_parsing_and_refusals(self) -> None:
        cfg = bot_config.load(self.env(CCTV_SITES="dacha, city=http://127.0.0.1:8790/"))
        self.assertEqual([("dacha", bot_config.DEFAULT_BRIDGE_URL), ("city", "http://127.0.0.1:8790")],
                         [(site.name, site.bridge_url) for site in cfg.sites])
        for raw, code in (("Dacha", "bad_site"), ("dacha=", "bad_site"), ("dacha dacha=http://127.0.0.1:8790", "site_clash"),
                          ("dacha city", "site_clash"), ("city=http://192.0.2.20:8780", "bridge_not_loopback"),
                          ("city=https://127.0.0.1:8790", "bridge_needs_http")):
            with self.subTest(raw=raw), self.assertRaises(bot_config.ConfigError) as caught:
                bot_config.load(self.env(CCTV_SITES=raw))
            self.assertEqual(code, caught.exception.code)

    async def test_one_site_behaves_like_the_single_bridge(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request):
            calls.append(f"{request.method} {request.url.path}")
            if request.url.path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    {"camera_id": "porch", "title": "Porch", "site": "", "status": "online"}]})
            if request.url.path == "/v1/discovery/scans":
                return httpx.Response(200, json={"ok": True, "scan_id": "s1"})
            return httpx.Response(200, json={"ok": True, "status": "accepted"})

        cfg = bot_config.load(self.env(CCTV_SITES="home"))
        client = httpx.Client(base_url=cfg.bridge_base_url, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        sites = Sites(cfg, {"home": BotBridge(cfg, client=client)})
        self.assertFalse(sites.multi)
        # Камера без локации не получает имя площадки: карта и темы — как прежде.
        self.assertEqual([""], [camera.site for camera in sites.cameras()])
        # Незнакомая камера идёт единственному мосту, а не отказом «не найдена».
        sites.request_media("r1", "unknown-cam", "snapshot", "2026-11-04T00:00:00Z")
        self.assertIn("POST /v1/media-requests", calls)
        # /add без выбора площадки — сразу поиск.
        with tempfile.TemporaryDirectory() as tmp:
            state = State(str(pathlib.Path(tmp) / "bot.sqlite3"))
            self.addCleanup(state.close)
            tg = FakeTelegram()
            bot = CctvBot(cfg, state, sites, tg)
            with mock.patch.object(bot, "_discover", new=mock.AsyncMock()) as discover:
                reply = await bot.on_add(OWNER, [], chat_id=-100500)
                await asyncio.sleep(0)
            self.assertEqual(bot._t("add.searching"), reply)
            discover.assert_called_once()
            self.assertFalse([call for call in tg.of("send_message") if "site" in str(call.get("text", "")).lower()])


class SitesRoutingTest(unittest.TestCase):
    """Каждый вызов камеры — мосту её площадки; заявки заведения — мосту, который их принял."""

    def setUp(self) -> None:
        self.calls: list[tuple[str, str]] = []
        cfg = bot_config.load({"CCTV_BOT_TOKEN": "0:test", "CCTV_STATE_DIR": "/tmp/cctv-sites-test",
                               "CCTV_RUNTIME_DIR": "/tmp/cctv-sites-test",
                               "CCTV_SITES": "dacha city=http://127.0.0.1:8790"})
        bridges = {}
        for site in cfg.sites:
            owned = {"dacha": ["porch", "shared"], "city": ["gate", "shared"]}[site.name]
            bridges[site.name] = BotBridge(dataclasses.replace(cfg, bridge_base_url=site.bridge_url), client=httpx.Client(
                base_url=site.bridge_url, transport=httpx.MockTransport(self.handler(site.name, owned))))
        self.sites = Sites(cfg, bridges)
        self.addCleanup(self.sites.close)

    def handler(self, name: str, owned: list[str]):
        def handle(request: httpx.Request):
            self.calls.append((name, f"{request.method} {request.url.path}"))
            path = request.url.path
            if path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    {"camera_id": camera_id, "title": camera_id, "status": "online"} for camera_id in owned],
                    "storage": {"used_bytes": 1, "budget_bytes": 10, "free_bytes": 100 if name == "dacha" else 5}})
            if path == "/v1/discovery/scans":
                return httpx.Response(200, json={"ok": True, "scan_id": f"scan-{name}"})
            if path.startswith("/v1/discovery/scans/"):
                return httpx.Response(200, json={"status": "done"})
            if path == "/v1/discovery/probes":
                return httpx.Response(200, json={"ok": True, "probe_token": f"probe-{name}"})
            if path == "/v1/detector/thresholds":
                return httpx.Response(200, json={"model": "m", "cameras": [
                    {"camera_id": camera_id, "threshold": 0.3} for camera_id in owned]})
            return httpx.Response(200, json={"ok": True, "status": "accepted", "camera": {}})
        return handle

    def site_calls(self, path: str) -> list[str]:
        return [name for name, call in self.calls if call.endswith(path)]

    def test_camera_calls_go_to_the_owner_site(self) -> None:
        cameras, storage = self.sites.registry()
        self.assertEqual(["porch", "shared", "gate"], [camera.camera_id for camera in cameras])
        self.assertEqual(5, storage.free_bytes)            # сторож видит самый тесный диск
        self.sites.request_media("r1", "gate", "snapshot", "2026-11-04T00:00:00Z")
        self.sites.set_camera_state("porch", "pause")
        self.sites.update_camera("gate", title="Gate")
        self.sites.set_detector_threshold("gate", 0.4)
        self.assertEqual(["city"], self.site_calls("/v1/media-requests"))
        self.assertEqual(["dacha"], self.site_calls("/v1/cameras/porch/state"))
        self.assertEqual(["city"], self.site_calls("/v1/cameras/gate/config"))
        # Камера с тем же id на второй площадке — у первой, порог не задваивается.
        self.assertEqual(["porch", "shared", "gate"],
                         [item["camera_id"] for item in self.sites.detector_thresholds()["cameras"]])
        with self.assertRaises(BridgeError) as caught:
            self.sites.camera_config("nowhere")
        self.assertEqual("not_found", caught.exception.code)

    def test_add_flow_stays_on_the_site_that_started_it(self) -> None:
        self.assertTrue(self.sites.choose("city"))
        self.assertFalse(self.sites.choose("mars"))
        scan = self.sites.scan_start()
        probe = self.sites.probe("192.0.2.30", "admin", "pw")
        self.sites.choose("dacha")                     # выбор сменили посреди мастера
        self.sites.scan_status(scan["scan_id"])
        self.sites.add_camera("cam3", "Cam 3", "Cam 3", probe["probe_token"])
        self.assertEqual(["city"], self.site_calls("/v1/discovery/scans/scan-city"))
        self.assertEqual(["city"], [name for name, call in self.calls if call == "POST /v1/cameras"])

    def test_media_url_must_belong_to_a_site(self) -> None:
        self.assertTrue(self.sites.is_bridge_url("http://127.0.0.1:8790/v1/media/x"))
        self.assertFalse(self.sites.is_bridge_url("http://127.0.0.1:9999/v1/media/x"))
        with self.assertRaises(BridgeError):
            self.sites.download("http://127.0.0.1:9999/v1/media/x", kind="snapshot", expected_sha256="")


if __name__ == "__main__":
    unittest.main()
