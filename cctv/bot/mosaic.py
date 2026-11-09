"""Мозаика «📷 Все камеры»: одно фото из последних кадров всех камер.

Кадры приходят из буфера движка (Bridge.latest_frame) — камеры ради мозаики
повторно не опрашиваются. Сетка подбирается под число камер, у каждой плитки
подпись «имя камеры · время кадра», камера без кадра — плашка вместо картинки.
Текст рисует шрифт, встроенный в OpenCV 5: в образе нет ни Pillow, ни шрифтов,
а имена камер пишут на любом языке.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Telegram ужимает фото до 2560 px по длинной стороне: шире собирать незачем.
MOSAIC_WIDTH = 2560
TILE_WIDTH_MAX = 1280  # одна-две камеры — не растягивать кадр на 2560
TILE_ASPECT = 16 / 9
GAP = 4
# Камер больше — несколько мозаик поровну: плитка 4×4 (≈640 px) ещё читается на
# телефоне, 5×5 — уже нет.
MOSAIC_TILES_MAX = 16
# Лимиты sendPhoto: 10 МБ, сумма сторон до 10000, стороны не дальше 1:20.
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_TARGET_BYTES = 5 * 1024 * 1024
PHOTO_SIDES_MAX = 10000
JPEG_QUALITY = (85, 75, 65, 55, 45)
BACKGROUND = (0, 0, 0)
EMPTY_FILL = (48, 48, 48)
EMPTY_TEXT = (200, 200, 200)
LABEL_TEXT = (255, 255, 255)
LABEL_SHADE = 0.45  # подложка подписи: доля яркости кадра под ней


@dataclass(frozen=True)
class Tile:
    """Плитка мозаики: имя камеры, готовая подпись времени и кадр (None — плашка `note`)."""
    title: str
    stamp: str = ""
    jpeg: bytes | None = None
    note: str = ""


def grid(count: int) -> tuple[int, int]:
    """Столбцы и строки под число камер: 1×1, 2×1, 2×2, 3×2, 3×3, 4×3, 4×4…"""
    if count <= 0:
        return 0, 0
    if count == 2:
        return 2, 1
    cols = math.ceil(math.sqrt(count))
    return cols, math.ceil(count / cols)


def pages(items: list, limit: int = MOSAIC_TILES_MAX) -> list[list]:
    """Разбить камеры на мозаики не больше `limit` плиток — поровну, а не 16+1."""
    if not items:
        return []
    count = math.ceil(len(items) / limit)
    size = math.ceil(len(items) / count)
    return [items[i:i + size] for i in range(0, len(items), size)]


def geometry(count: int) -> tuple[int, int, int, int]:
    """(ширина, высота) мозаики и (ширина, высота) плитки."""
    cols, rows = grid(count)
    tile_w = min(TILE_WIDTH_MAX, (MOSAIC_WIDTH - (cols - 1) * GAP) // cols) // 2 * 2
    tile_h = round(tile_w / TILE_ASPECT) // 2 * 2
    return cols * tile_w + (cols - 1) * GAP, rows * tile_h + (rows - 1) * GAP, tile_w, tile_h


_FONT = None


def _font():
    global _FONT
    if _FONT is None:
        import cv2

        _FONT = cv2.FontFace("sans")
    return _FONT


def _text_width(text: str, size: int) -> int:
    import cv2

    return int(cv2.getTextSize((0, 0), text, (0, size), _font(), size, 600)[2])


def _fit(text: str, size: int, room: int) -> str:
    """Обрезать текст с «…», чтобы влез в `room` пикселей."""
    if room <= 0:
        return ""
    if _text_width(text, size) <= room:
        return text
    while text and _text_width(text + "…", size) > room:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


def _put(canvas, text: str, x: int, baseline: int, size: int, color) -> None:
    import cv2

    if text:
        cv2.putText(canvas, text, (x, baseline), color, _font(), size, 600)


def _frame(jpeg: bytes | None, width: int, height: int):
    """Кадр, вписанный в плитку с полями; битый или пустой JPEG — None."""
    import cv2
    import numpy as np

    if not jpeg:
        return None
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or not image.size:
        return None
    scale = min(width / image.shape[1], height / image.shape[0])
    fit_w, fit_h = max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))
    resized = cv2.resize(image, (fit_w, fit_h),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    tile = np.zeros((height, width, 3), dtype=np.uint8)
    x, y = (width - fit_w) // 2, (height - fit_h) // 2
    tile[y:y + fit_h, x:x + fit_w] = resized
    return tile


def _tile(item: Tile, width: int, height: int):
    import numpy as np

    size = max(14, height // 14)
    tile = _frame(item.jpeg, width, height)
    if tile is None:
        tile = np.full((height, width, 3), EMPTY_FILL, dtype=np.uint8)
        note_size = max(16, height // 9)
        note = _fit(item.note, note_size, width - 2 * size)
        _put(tile, note, (width - _text_width(note, note_size)) // 2,
             (height + note_size) // 2 - size, note_size, EMPTY_TEXT)
    # Подпись — полосой внизу плитки: имя слева, время справа.
    bar = int(size * 1.7)
    tile[height - bar:] = (tile[height - bar:] * LABEL_SHADE).astype(np.uint8)
    pad = size // 2
    baseline = height - (bar - size) // 2 - size // 6
    stamp_w = _text_width(item.stamp, size) if item.stamp else 0
    if stamp_w:
        _put(tile, item.stamp, width - pad - stamp_w, baseline, size, LABEL_TEXT)
    title = _fit(item.title, size, width - 2 * pad - (stamp_w + pad if stamp_w else 0))
    _put(tile, title, pad, baseline, size, LABEL_TEXT)
    return tile


def compose(tiles: list[Tile]) -> bytes:
    """Мозаика JPEG в лимитах фото Telegram (по размеру сторон и по весу)."""
    import cv2
    import numpy as np

    if not tiles:
        raise ValueError("no tiles")
    cols, _rows = grid(len(tiles))
    width, height, tile_w, tile_h = geometry(len(tiles))
    canvas = np.full((height, width, 3), BACKGROUND, dtype=np.uint8)
    for index, item in enumerate(tiles):
        row, col = divmod(index, cols)
        x, y = col * (tile_w + GAP), row * (tile_h + GAP)
        canvas[y:y + tile_h, x:x + tile_w] = _tile(item, tile_w, tile_h)
    body = b""
    for quality in JPEG_QUALITY:
        ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise ValueError("jpeg encode failed")
        body = encoded.tobytes()
        if len(body) <= PHOTO_TARGET_BYTES:
            break
    if len(body) > PHOTO_MAX_BYTES:
        raise ValueError("mosaic exceeds the Telegram photo limit")
    return body
