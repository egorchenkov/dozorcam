"""QR установщика: то, что печатает ``python -m cctv.qr``, читается декодером OpenCV.

Классический QRCodeDetector иногда не находит код на идеальной картинке (не ошибка
кодирования — у него же «пусто», а не чужой текст), поэтому код считается
прочитанным, если его прочитал любой из двух детекторов OpenCV.
"""
from __future__ import annotations

import random
import string
import unittest

import cv2
import numpy as np

from cctv import qr


def picture(grid: list[list[bool]], scale: int = 8) -> np.ndarray:
    size = len(grid) + 8
    image = np.full((size, size), 255, np.uint8)
    for y, row in enumerate(grid):
        for x, dark in enumerate(row):
            if dark:
                image[y + 4, x + 4] = 0
    return cv2.resize(image, (size * scale, size * scale), interpolation=cv2.INTER_NEAREST)


class QrTest(unittest.TestCase):
    detectors = [cv2.QRCodeDetector()] + ([cv2.QRCodeDetectorAruco()]
                                          if hasattr(cv2, "QRCodeDetectorAruco") else [])

    def decoded(self, grid) -> set[str]:
        image = picture(grid)
        return {detector.detectAndDecode(image)[0] for detector in self.detectors}

    def test_owner_links_decode(self):
        for link in ("https://t.me/dozorcam_bot?start=ABCDEFGHJK",
                     "https://t.me/" + "b" * 32 + "?start=Z23456789A"):
            with self.subTest(link=link):
                self.assertIn(link, self.decoded(qr.encode(link)))

    def test_every_version_and_mask_decodes(self):
        rng = random.Random(7)
        for length in (1, 14, 15, 26, 27, 42, 43, 62, 63, 84, 85, 106):
            text = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))
            version, codewords = qr._codewords(text.encode())
            matrix = qr._Matrix(version)
            matrix.place(codewords)
            for mask in range(8):
                with self.subTest(length=length, mask=mask):
                    matrix.format_bits(mask)
                    self.assertIn(text, self.decoded(matrix.masked(mask)))

    def test_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            qr.encode("x" * 107)

    def test_render_has_quiet_zone_and_both_modes(self):
        grid = qr.encode("https://t.me/x?start=AB")
        ascii_lines = qr.render(grid, ascii_only=True).splitlines()
        self.assertEqual(len(grid) + 2 * qr.QUIET, len(ascii_lines))
        self.assertEqual(set(ascii_lines[0]), {" "})
        self.assertNotIn("\033", "".join(ascii_lines))
        blocks = qr.render(grid).splitlines()
        self.assertEqual((len(grid) + 2 * qr.QUIET + 1) // 2, len(blocks))
        self.assertTrue(all(line.startswith("\033[30;107m") for line in blocks))


if __name__ == "__main__":
    unittest.main()
