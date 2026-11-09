#!/usr/bin/env python3
"""«📷 Все камеры»: кнопка карты → одно фото-мозаика из кадров буфера движка.

Кадры бот берёт у моста запросом `GET /v1/cameras/<id>/frame` (кадр из
кольцевого буфера, см. tests/engine/test_latest_frame_20261109.py): заказов
снимка у камеры (`/v1/media-requests`) мозаика не делает. Сетка — под число
камер, подпись плитки — имя и время кадра, камера без кадра — плашка
«нет кадров», на паузе — «на паузе». Фото укладывается в лимиты Telegram.

Настоящие CctvBot, State и Bridge; подменены только Bot API и HTTP моста.
"""
from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import tempfile
import unittest
from unittest import mock

import cv2
import httpx
import numpy as np

from cctv.bot import mosaic
from cctv.bot.bot import CctvBot
from cctv.bot.bridge import Bridge
from cctv.bot.state import State
from test_cctv_contract_20260824 import BRIDGE, make_config
from test_cctv_flow_20260824 import OWNER
from test_map_card_20261009 import ScreenTelegram

FRAME_AT = "2026-11-09T10:00:05+00:00"  # 13:00:05 в поясе установки (Europe/Moscow)


def jpeg(color: tuple[int, int, int], width: int = 1920, height: int = 1080) -> bytes:
    image = np.full((height, width, 3), color[::-1], dtype=np.uint8)
    return cv2.imencode(".jpg", image)[1].tobytes()


def colour_of(camera_id: str) -> tuple[int, int, int]:
    number = int(camera_id[3:])
    return (40 + number * 7 % 200, 200 - number * 5 % 150, 90)


class Harness(unittest.IsolatedAsyncioTestCase):
    COUNT = 4

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = dataclasses.replace(make_config(self.tmp), owner_ids=frozenset({OWNER}),
                                       max_snapshot_bytes=8 * 1024 * 1024)
        self.state = State(":memory:")
        self.addCleanup(self.state.close)
        self.cameras = [{"camera_id": f"cam{i}", "title": f"Камера {i}", "site": "Дача"}
                        for i in range(1, self.COUNT + 1)]
        self.status = {c["camera_id"]: "online" for c in self.cameras}
        self.offline: set[str] = set()
        self.requests: list[str] = []

        def handler(request: httpx.Request):
            path = request.url.path
            self.requests.append(path)
            if path == "/v1/cameras":
                return httpx.Response(200, json={"cameras": [
                    dict(c, status=self.status[c["camera_id"]], last_frame_at=None,
                         motion={"state": "watching", "reason": "", "last_motion_at": None})
                    for c in self.cameras]})
            if path.endswith("/frame"):
                camera_id = path.split("/")[-2]
                if camera_id in self.offline:
                    return httpx.Response(503, json={"error": "camera_offline"})
                return httpx.Response(200, content=jpeg(colour_of(camera_id)),
                                      headers={"content-type": "image/jpeg", "x-captured-at": FRAME_AT})
            return httpx.Response(404, json={"error": "not_found"})

        client = httpx.Client(base_url=BRIDGE, transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        self.tg = ScreenTelegram()
        self.bot = CctvBot(self.cfg, self.state, Bridge(self.cfg, client=client), self.tg)
        self.bot.routes.set_preset("flat")

    async def map_message(self) -> dict:
        await self.bot.sync_registry()
        await self.bot.refresh_console()
        place = self.bot.routes.map_places()[0]
        screen = self.state.home_screen(place.chat_id, place.thread_id)
        found = None
        for number, (name, kwargs) in enumerate(self.tg.calls, start=1):
            if name == "send_message" and number == screen.message_id:
                found = kwargs
            if name == "edit_message_text" and kwargs["message_id"] == screen.message_id:
                found = kwargs
        return dict(found, place=place, message_id=screen.message_id)

    async def press_mosaic(self) -> tuple[str, list[dict]]:
        message = await self.map_message()
        data = next(b.callback_data for row in message["reply_markup"].inline_keyboard
                    for b in row if b.text == "📷 Все камеры")
        place = message["place"]
        toast = await self.bot.on_callback(OWNER, place.thread_id, data, chat_id=place.chat_id,
                                           message_id=message["message_id"])
        await self.bot._mosaic_task
        return toast, self.tg.of("send_photo")

    @staticmethod
    def image(photo: dict):
        return cv2.imdecode(np.frombuffer(photo["photo"], np.uint8), cv2.IMREAD_COLOR)


class MosaicFlow(Harness):
    COUNT = 4

    async def test_one_press_one_photo_from_the_buffer(self) -> None:
        toast, photos = await self.press_mosaic()
        self.assertEqual("Собираю кадры всех камер — фото придёт сюда.", toast)
        self.assertEqual(1, len(photos))
        photo = photos[0]
        place = self.bot.routes.map_places()[0]
        self.assertEqual(place.chat_id, photo["chat_id"])
        self.assertTrue(photo["caption"].startswith("📷 Все камеры: "), photo["caption"])
        # Кадры — из буфера движка, по одному на камеру; снимок у камеры не заказан.
        frames = [p for p in self.requests if p.endswith("/frame")]
        self.assertEqual(sorted(f"/v1/cameras/cam{i}/frame" for i in range(1, 5)), sorted(frames))
        self.assertNotIn("/v1/media-requests", self.requests)
        image = self.image(photo)
        width, height, tile_w, tile_h = mosaic.geometry(4)
        self.assertEqual((height, width), image.shape[:2])
        # Плитка 2×2 по порядку карты: центр каждой — цвет кадра своей камеры.
        for index in range(4):
            row, col = divmod(index, 2)
            y = row * (tile_h + mosaic.GAP) + tile_h // 3
            x = col * (tile_w + mosaic.GAP) + tile_w // 2
            b, g, r = (int(v) for v in image[y, x])
            want = colour_of(f"cam{index + 1}")
            self.assertTrue(all(abs(a - w) <= 12 for a, w in zip((r, g, b), want)), (index, (r, g, b), want))

    async def test_labels_title_and_frame_time_offline_and_paused_plates(self) -> None:
        self.offline.add("cam2")
        self.status["cam3"] = "paused"
        seen: list[list[mosaic.Tile]] = []
        real = mosaic.compose
        with mock.patch.object(mosaic, "compose", side_effect=lambda tiles: seen.append(tiles) or real(tiles)):
            _toast, photos = await self.press_mosaic()
        self.assertEqual(1, len(photos))
        tiles = {tile.title: tile for tile in seen[0]}
        self.assertEqual(["Камера 1", "Камера 2", "Камера 3", "Камера 4"], [t.title for t in seen[0]])
        self.assertEqual("13:00:05", tiles["Камера 1"].stamp)  # время кадра в поясе установки
        self.assertIsNotNone(tiles["Камера 1"].jpeg)
        self.assertIsNone(tiles["Камера 2"].jpeg)
        self.assertEqual("нет кадров", tiles["Камера 2"].note)
        self.assertEqual("", tiles["Камера 2"].stamp)
        self.assertEqual("на паузе", tiles["Камера 3"].note)
        self.assertNotIn("/v1/cameras/cam3/frame", self.requests)  # пауза — кадр не просим
        # Плашка видна на фото: плитка офлайн-камеры — серая, а не кадр.
        image = self.image(photos[0])
        _w, _h, tile_w, tile_h = mosaic.geometry(4)
        x = tile_w + mosaic.GAP + 6
        self.assertTrue(all(abs(int(v) - mosaic.EMPTY_FILL[0]) <= 6 for v in image[6, x]), image[6, x])

    async def test_second_press_while_collecting_is_not_a_second_mosaic(self) -> None:
        message = await self.map_message()
        data = next(b.callback_data for row in message["reply_markup"].inline_keyboard
                    for b in row if b.text == "📷 Все камеры")
        place = message["place"]
        first = await self.bot.on_callback(OWNER, place.thread_id, data, chat_id=place.chat_id)
        second = await self.bot.on_callback(OWNER, place.thread_id, data, chat_id=place.chat_id)
        await self.bot._mosaic_task
        await asyncio.sleep(0)
        self.assertEqual("Мозаика уже собирается — придёт через мгновение.", second)
        self.assertNotEqual(first, second)
        self.assertEqual(1, len(self.tg.of("send_photo")))
        third = await self.bot.on_callback(OWNER, place.thread_id, data, chat_id=place.chat_id)
        await self.bot._mosaic_task
        self.assertEqual(first, third)
        self.assertEqual(2, len(self.tg.of("send_photo")))

    async def test_bridge_down_is_said_not_silent(self) -> None:
        message = await self.map_message()
        data = next(b.callback_data for row in message["reply_markup"].inline_keyboard
                    for b in row if b.text == "📷 Все камеры")
        place = message["place"]
        with mock.patch.object(self.bot.bridge, "registry",
                               side_effect=__import__("cctv.bot.bridge", fromlist=["BridgeError"])
                               .BridgeError("unavailable")):
            await self.bot.on_callback(OWNER, place.thread_id, data, chat_id=place.chat_id)
            await self.bot._mosaic_task
        self.assertFalse(self.tg.of("send_photo"))
        self.assertTrue(self.tg.of("send_message")[-1]["text"].startswith("Мозаику собрать не удалось"))


class GridPerCount(Harness):
    """Сетка под число камер: 1, 4, 6, 9 и больше; больше 16 — несколько мозаик поровну."""

    async def run_count(self, count: int) -> list[dict]:
        self.cameras = [{"camera_id": f"cam{i}", "title": f"Камера {i}", "site": "Дача"}
                        for i in range(1, count + 1)]
        self.status = {c["camera_id"]: "online" for c in self.cameras}
        _toast, photos = await self.press_mosaic()
        return photos

    async def test_grids(self) -> None:
        for count, grids in ((1, [(1, 1)]), (4, [(2, 2)]), (6, [(3, 2)]), (9, [(3, 3)]),
                             (12, [(4, 3)]), (16, [(4, 4)]), (20, [(4, 3), (4, 3)])):
            with self.subTest(count=count):
                self.tg.calls.clear()
                photos = await self.run_count(count)
                self.assertEqual(len(grids), len(photos))
                sizes = [len(batch) for batch in mosaic.pages(list(range(count)))]
                for photo, batch, grid in zip(photos, sizes, grids):
                    self.assertEqual(grid, mosaic.grid(batch))
                    width, height, _tw, _th = mosaic.geometry(batch)
                    self.assertEqual((height, width), self.image(photo).shape[:2])
                    self.assertLessEqual(len(photo["photo"]), mosaic.PHOTO_MAX_BYTES)
                if len(photos) > 1:
                    self.assertEqual(["📷 Все камеры (1/2)", "📷 Все камеры (2/2)"],
                                     [p["caption"].split(":")[0] for p in photos])


class MosaicLimits(unittest.TestCase):
    def test_grid_table(self) -> None:
        table = {1: (1, 1), 2: (2, 1), 3: (2, 2), 4: (2, 2), 5: (3, 2), 6: (3, 2), 7: (3, 3),
                 9: (3, 3), 10: (4, 3), 12: (4, 3), 13: (4, 4), 16: (4, 4)}
        for count, want in table.items():
            with self.subTest(count=count):
                self.assertEqual(want, mosaic.grid(count))
                cols, rows = want
                self.assertGreaterEqual(cols * rows, count)

    def test_pages_split_evenly(self) -> None:
        self.assertEqual([16], [len(p) for p in mosaic.pages(list(range(16)))])
        self.assertEqual([9, 8], [len(p) for p in mosaic.pages(list(range(17)))])
        self.assertEqual([14, 14, 12], [len(p) for p in mosaic.pages(list(range(40)))])
        self.assertEqual([], mosaic.pages([]))

    def test_telegram_photo_limits_with_worst_case_frames(self) -> None:
        """Шум не сжимается: 16 плиток шума 1920×1080 — всё равно в лимите веса и сторон."""
        rng = np.random.default_rng(1)
        noise = [cv2.imencode(".jpg", rng.integers(0, 256, (1080, 1920, 3), dtype=np.uint8))[1].tobytes()
                 for _ in range(4)]
        for count in (1, 2, 9, 16):
            with self.subTest(count=count):
                tiles = [mosaic.Tile(f"Камера {i}", "13:00:05", noise[i % 4]) for i in range(count)]
                body = mosaic.compose(tiles)
                self.assertLessEqual(len(body), mosaic.PHOTO_MAX_BYTES)
                image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
                height, width = image.shape[:2]
                self.assertLessEqual(width, mosaic.MOSAIC_WIDTH)
                self.assertLessEqual(width + height, mosaic.PHOTO_SIDES_MAX)
                self.assertLessEqual(max(width, height) / min(width, height), 20)

    def test_broken_frame_and_long_title_do_not_break_the_tile(self) -> None:
        tiles = [mosaic.Tile("Очень длинное имя камеры, которое не влезает в плитку целиком " * 3,
                             "13:00:05", b"\xff\xd8not-a-jpeg"),
                 mosaic.Tile("ok", "", None, "нет кадров")]
        image = cv2.imdecode(np.frombuffer(mosaic.compose(tiles), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(mosaic.geometry(2)[1::-1], image.shape[:2])
        self.assertTrue(all(abs(int(v) - mosaic.EMPTY_FILL[0]) <= 6 for v in image[6, 6]))


if __name__ == "__main__":
    unittest.main()
