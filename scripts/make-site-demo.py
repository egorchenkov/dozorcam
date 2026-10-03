#!/usr/bin/env python3
"""Рисует demo.gif и event.png для сайта: синтетический двор, человек идёт по дорожке,
детектор обводит его рамкой. Настоящих кадров с камер на сайте нет — только эта сцена.

    python3 scripts/make-site-demo.py      # нужен Pillow; пишет в docs/site/assets
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 480, 270
OUT = Path(__file__).resolve().parent.parent / "docs" / "site" / "assets"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def font(size):
    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default()


def scene(d):
    d.rectangle([0, 0, W, 110], fill=(176, 196, 214))            # небо
    d.rectangle([0, 110, W, H], fill=(104, 138, 92))             # газон
    d.polygon([(150, H), (330, H), (270, 120), (215, 120)], fill=(176, 164, 146))  # дорожка
    d.rectangle([300, 40, 470, 140], fill=(226, 214, 190))       # дом
    d.polygon([(290, 42), (385, 0), (480, 42)], fill=(140, 84, 70))
    d.rectangle([330, 70, 365, 105], fill=(92, 112, 130))
    d.rectangle([400, 80, 430, 140], fill=(120, 90, 70))
    for x in range(0, 160, 16):                                   # забор
        d.rectangle([x, 92, x + 10, 140], fill=(150, 120, 90))
    d.rectangle([0, 104, 160, 110], fill=(130, 100, 75))
    d.ellipse([20, 150, 110, 230], fill=(70, 110, 64))            # куст


def person(d, x, y, step):
    s = 1.0 + (y - 120) / 110                                      # ближе — крупнее
    head, body = 7 * s, 30 * s
    d.ellipse([x - head, y - body - 2 * head, x + head, y - body], fill=(60, 60, 70))
    d.rectangle([x - 6 * s, y - body, x + 6 * s, y - 8 * s], fill=(52, 86, 150))
    sw = (6 if step % 2 else -6) * s
    d.line([x, y - 8 * s, x - sw, y + 12 * s], fill=(40, 40, 48), width=max(2, int(4 * s)))
    d.line([x, y - 8 * s, x + sw, y + 12 * s], fill=(40, 40, 48), width=max(2, int(4 * s)))
    return [x - 12 * s, y - body - 2 * head - 3, x + 12 * s, y + 13 * s]


def frame(i, n):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    scene(d)
    t = i / (n - 1)
    x, y = 250 - 30 * t, 125 + 120 * t
    box = person(d, x, y, i // 2)
    if i >= 6:
        score = min(0.41 + 0.02 * (i - 6), 0.78)
        d.rectangle(box, outline=(40, 220, 90), width=2)
        label = f"person {score:.2f}"
        d.rectangle([box[0], box[1] - 14, box[0] + 7 * len(label) + 4, box[1] - 1], fill=(20, 30, 24))
        d.text((box[0] + 2, box[1] - 14), label, fill=(40, 220, 90), font=font(11))
    sec = 17 + i // 5
    d.text((8, 6), f"2026-10-03 14:02:{sec:02d}", fill=(255, 255, 255), font=font(13))
    d.text((W - 78, H - 20), "Camera 1", fill=(255, 255, 255), font=font(13))
    return img


def main():
    n = 24
    frames = [frame(i, n) for i in range(n)]
    frames[0].save(OUT / "demo.gif", save_all=True, append_images=frames[1:] + [frames[-1]] * 6,
                   duration=160, loop=0, optimize=True)
    frames[14].save(OUT / "event.png", optimize=True)


if __name__ == "__main__":
    main()
