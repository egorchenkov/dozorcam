#!/usr/bin/env python3
"""Подпись узлов в сообщениях: откуда кадр и клип (узел движка) и кто отвечает (узел бота).

При нескольких узлах ([failover]) супервизор кладёт обеим ролям CCTV_ORIGIN — имя
своего узла. Движок ставит его в каждое событие и в реестр, бот — в подписи
событий, «Кадра», клипов, в карточку камеры («Статус») и в /nodes. Без [failover]
узел один: переменной нет, строки нет, сообщения прежние. После перехвата роли
подпись называет новый узел: её несёт каждое событие и задаёт запуск процесса бота.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import httpx

from cctv import failover
from cctv.bot import config as bot_config
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.events import normalize_event
from cctv.bot.state import State
from cctv.engine import cctv_bridge
from cctv.failover import panel
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER, PHOTO, PHOTO_SHA, FakeTelegram

ROOT = pathlib.Path(__file__).resolve().parents[2]


class AttributionFlow(unittest.IsolatedAsyncioTestCase):
    """Настоящие CctvBot, State и Bridge бота; подменены Bot API и HTTP моста."""

    bot_origin = ""
    lang = "ru"
    merge_sec = 0  # каждое движение — свой пост; склейку проверяет отдельный тест

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.registry_origin = ""
        self.media_requests: list[dict] = []
        self.make_bot(self.bot_origin)

    def make_bot(self, origin: str) -> None:
        """Процесс бота на узле `origin` (пусто — узел один); state общий, как у копии state."""
        self.cfg = dataclasses.replace(make_config(self.tmp), origin=origin, lang=self.lang,
                                       event_merge_sec=self.merge_sec)
        if not hasattr(self, "state"):
            self.state = State(":memory:")
            self.addCleanup(self.state.close)

        def handler(request: httpx.Request):
            if request.url.path == "/v1/cameras":
                payload = {"cameras": [{"camera_id": "city", "title": "Город", "site": "city",
                                        "status": "online", "last_frame_at": "2026-11-04T10:00:00Z",
                                        "motion": {"state": "watching", "reason": "", "last_motion_at": None}}]}
                if self.registry_origin:
                    payload["origin"] = self.registry_origin
                return httpx.Response(200, json=payload)
            if request.url.path == "/v1/media-requests":
                body = json.loads(request.content)
                self.media_requests.append(body)
                return httpx.Response(202, json={"request_id": body["request_id"], "status": "accepted"})
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = FakeTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)

    # --- события, как их шлёт движок ------------------------------------------
    @staticmethod
    def with_origin(payload: dict, origin: str) -> dict:
        return {**payload, "origin": origin} if origin else payload

    async def register(self) -> None:
        await self.bot.on_event(normalize_event({"event_id": "e-reg", "type": "camera.registered",
                                                 "camera_id": "city", "title": "Город", "site": "city",
                                                 "occurred_at": "2026-11-04T10:00:00Z"}))

    async def motion(self, event_id: str, origin: str = "", at: str = "2026-11-04T10:01:00Z") -> None:
        await self.bot.on_event(normalize_event(self.with_origin(
            {"event_id": event_id, "type": "motion.detected", "camera_id": "city", "occurred_at": at,
             "snapshot": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}},
            origin)))

    async def clip(self, source_event_id: str, origin: str = "") -> None:
        await self.bot.on_event(normalize_event(self.with_origin(
            {"event_id": f"clip-{source_event_id}", "type": "media.ready", "camera_id": "city",
             "request_id": source_event_id, "source_event_id": source_event_id, "kind": "clip",
             "occurred_at": "2026-11-04T10:01:00Z", "captured_at": "2026-11-04T10:01:00Z",
             "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO),
                          "content_type": "video/mp4"}}, origin)))

    async def frame(self, origin: str = "") -> None:
        """Кнопка «Кадр» и ответ движка на заказ."""
        data = self.tg.of("send_message")[0]["reply_markup"].inline_keyboard[0][0].callback_data
        await self.bot.on_callback(OWNER, 101, data)
        request_id = self.media_requests[-1]["request_id"]
        await self.bot.on_event(normalize_event(self.with_origin(
            {"event_id": f"ready-{request_id}", "type": "media.ready", "camera_id": "city",
             "request_id": request_id, "kind": "snapshot", "captured_at": "2026-11-04T10:02:00Z",
             "download": {"url": f"{BRIDGE}/v1/media/opaque", "sha256": PHOTO_SHA, "bytes": len(PHOTO)}},
            origin)))

    def captions(self) -> list[str]:
        return [call["caption"] for call in self.tg.of("send_photo") + self.tg.of("send_video")]


class SingleNodeTest(AttributionFlow):
    """Без [failover]: ни у бота, ни у движка нет CCTV_ORIGIN — подписи прежние."""

    async def test_no_node_line_anywhere(self) -> None:
        await self.register()
        await self.motion("m1")
        await self.clip("m1")
        await self.frame()
        status = await self.bot.on_text(OWNER, 101, "🔄 Статус")
        self.assertEqual(3, len(self.captions()))
        for text in [*self.captions(), status, *[call["text"] for call in self.tg.of("send_message")]]:
            self.assertNotIn("Узел", text)
            self.assertNotIn("Узлы", text)

    def test_single_node_start_has_no_origin(self) -> None:
        """Одноузловой старт: compose не передаёт CCTV_ORIGIN, бот и мост без подписи."""
        resolved = bot_config.load({"CCTV_BOT_TOKEN": "0:test", "CCTV_CHAT_ID": "-100500",
                                    "CCTV_ALLOWED_USER_IDS": "7", "CCTV_STATE_DIR": str(self.tmp)})
        self.assertEqual("", resolved.origin)
        self.assertNotIn("CCTV_ORIGIN", (ROOT / "compose.yml").read_text())
        with mock.patch.dict(os.environ, {"CCTV_ORIGIN": ""}):
            bridge = cctv_bridge.Bridge({"cameras": []}, self.tmp / "spool", "http://127.0.0.1:1")
        self.assertNotIn("origin", bridge.registry())


class SameNodeTest(AttributionFlow):
    """Движок и бот на одном узле: одна строка «Узел: a»."""

    bot_origin = "a"

    async def test_motion_clip_frame_and_status_name_the_node(self) -> None:
        self.registry_origin = "a"
        await self.register()
        await self.motion("m1", origin="a")
        await self.clip("m1", origin="a")
        await self.frame(origin="a")
        captions = self.captions()
        self.assertEqual(3, len(captions))
        for caption in captions:
            self.assertIn("\nУзел: a", caption)
        self.assertIn("Узел: a", await self.bot.on_text(OWNER, 101, "🔄 Статус"))


class SplitNodesTest(AttributionFlow):
    """Движок на одном узле, бот — на другом; и перехват каждой роли."""

    bot_origin = "a"

    async def test_engine_and_bot_nodes_differ(self) -> None:
        self.registry_origin = "b"
        await self.register()
        await self.motion("m1", origin="b")
        await self.clip("m1", origin="b")
        await self.frame(origin="b")
        for caption in self.captions():
            self.assertIn("Узлы: движок b · бот a", caption)
        self.assertIn("Узлы: движок b · бот a", await self.bot.on_text(OWNER, 101, "🔄 Статус"))

    async def test_engine_takeover_names_the_new_engine_node(self) -> None:
        self.registry_origin = "a"
        await self.register()
        await self.motion("m1", origin="a")
        self.assertIn("Узел: a", self.captions()[-1])
        # Роль engine перехватил узел c: события и реестр идут уже от его движка.
        self.registry_origin = "c"
        await self.motion("m2", origin="c", at="2026-11-04T10:09:00Z")
        self.assertIn("Узлы: движок c · бот a", self.captions()[-1])
        self.assertIn("Узлы: движок c · бот a", await self.bot.on_text(OWNER, 101, "🔄 Статус"))

    async def test_bot_takeover_names_the_new_bot_node(self) -> None:
        self.registry_origin = "a"
        await self.register()
        await self.motion("m1", origin="a")
        self.assertIn("Узел: a", self.captions()[-1])
        # Роль bot перехватил узел c: новый процесс бота с его CCTV_ORIGIN, тот же state.
        self.make_bot("c")
        await self.motion("m2", origin="a", at="2026-11-04T10:09:00Z")
        self.assertIn("Узлы: движок a · бот c", self.captions()[-1])

    async def test_merged_motion_keeps_the_node_line(self) -> None:
        self.merge_sec = 60
        self.make_bot("a")
        await self.register()
        await self.motion("m1", origin="b")
        await self.motion("m2", origin="b", at="2026-11-04T10:01:20Z")
        edits = self.tg.of("edit_message_media")
        self.assertEqual(1, len(edits), "второе движение минуты вклеивается в пост")
        self.assertIn("Узлы: движок b · бот a", edits[0]["media"].caption)

    async def test_engine_outside_the_layer_shows_only_the_bot(self) -> None:
        """Движок площадки вне [failover] не подписывает события — видно только узел бота."""
        await self.register()
        await self.motion("m1")
        self.assertIn("Узел: бот a", self.captions()[-1])


class EnglishTest(AttributionFlow):
    bot_origin = "a"
    lang = "en"

    async def test_english_lines(self) -> None:
        await self.register()
        await self.motion("m1", origin="b")
        self.assertIn("Nodes: engine b · bot a", self.captions()[-1])
        await self.motion("m2", origin="a", at="2026-11-04T10:09:00Z")
        self.assertIn("Node: a", self.captions()[-1])


class EngineSignsEventsTest(unittest.TestCase):
    """Движок с CCTV_ORIGIN ставит его в каждое событие и в реестр."""

    def test_every_event_and_registry_carry_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CCTV_ORIGIN": "b", "CCTV_EVENTS_URL": "http://127.0.0.1:9/v1/events"}
            with mock.patch.dict(os.environ, env):
                bridge = cctv_bridge.Bridge({"cameras": []}, pathlib.Path(tmp), "http://127.0.0.1:1")
            sent = []

            class Reply:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

            def urlopen(request, **_kwargs):
                sent.append(json.loads(request.data))
                return Reply()

            with mock.patch.object(cctv_bridge.urllib.request, "urlopen", urlopen):
                for kind in ("motion.detected", "media.ready", "media.failed"):
                    bridge.push_event({"event_id": kind, "type": kind, "camera_id": "city"})
            self.assertEqual(["b"] * 3, [event["origin"] for event in sent])
            self.assertEqual("b", bridge.registry()["origin"])
            self.assertEqual("b", normalize_event({**sent[0], "occurred_at": "2026-11-04T10:00:00Z"}).origin)


class LayerGivesOriginTest(unittest.TestCase):
    """Слой кладёт обеим ролям имя своего узла; /nodes называет узел отвечающего бота."""

    NODES = ('[failover]\n[[failover.nodes]]\nname = "a"\nengine_agent = "http://127.0.0.1:18782"\n'
             'bot_agent = "http://127.0.0.1:18783"\n[[failover.nodes]]\nname = "b"\n'
             'engine_agent = "http://127.0.0.1:18792"\nbot_agent = "http://127.0.0.1:18793"\n')

    def load(self, me: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config.toml").write_text(self.NODES)
            (root / "secrets.toml").write_text('[failover]\nsecret = "unit-test-failover-secret-0123456789"\n')
            (root / "secrets.toml").chmod(0o600)
            return failover.load({"CCTV_CONFIG_DIR": tmp, "CCTV_NODE": me})

    def test_both_roles_get_their_node_name(self) -> None:
        for me in ("a", "b"):
            cfg = self.load(me)
            for role in ("engine", "bot"):
                with tempfile.TemporaryDirectory() as state:
                    agent = failover.Agent(cfg, role, pathlib.Path(state), log=lambda _line: None)
                    self.assertEqual(me, agent.panel_env()["CCTV_ORIGIN"])

    def test_nodes_panel_names_the_answering_bot(self) -> None:
        import threading
        from types import SimpleNamespace

        cfg = self.load("b")
        agent = SimpleNamespace(cfg=cfg, holder={"engine": "a", "bot": "b"}.get, switching=None,
                                last_switch=None, lock=threading.Lock(), assign={})
        self.assertIn("The bot answers from node b.", panel.view(agent, "en", statuses={})["text"])
        self.assertIn("Сейчас отвечает бот узла b.", panel.view(agent, "ru", statuses={})["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
