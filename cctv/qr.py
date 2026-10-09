"""QR-код в терминале без зависимостей: ссылка владельца для установщика.

Установщик (scripts/install.sh) и обёртка ``dozorcam code`` печатают ссылку
``https://t.me/<бот>?start=<код>`` и QR к ней, чтобы владелец открыл бота с
телефона, не перепечатывая код. На хосте может не быть ни python, ни qrencode,
поэтому кодирует сам образ: ``python -m cctv.qr <текст>``.

Байтовый режим, уровень коррекции M, версии 1–6 — до 106 байт (ссылка с именем
бота до 32 символов и кодом из 10 — не длиннее 62). Печать полублоками с явными
цветами ANSI: тёмные модули на белом фоне читаются и в тёмном, и в светлом
терминале. ``--ascii`` — для терминала без UTF-8.
"""
from __future__ import annotations

import sys

# Версия → (данных на блок, блоков, коррекции на блок, центры выравнивающих узоров); уровень M.
VERSIONS = {
    1: (16, 1, 10, ()),
    2: (28, 1, 16, (6, 18)),
    3: (44, 1, 26, (6, 22)),
    4: (32, 2, 18, (6, 26)),
    5: (43, 2, 24, (6, 30)),
    6: (27, 4, 16, (6, 34)),
}
ECL_M = 0b00
QUIET = 2


def _gf_tables() -> tuple[list[int], list[int]]:
    exp, log = [0] * 512, [0] * 256
    value = 1
    for power in range(255):
        exp[power], log[value] = value, power
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for power in range(255, 512):
        exp[power] = exp[power - 255]
    return exp, log


EXP, LOG = _gf_tables()


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _generator(degree: int) -> list[int]:
    poly = [1]
    for power in range(degree):
        poly = [a ^ _mul(b, EXP[power]) for a, b in zip(poly + [0], [0] + poly)]
    return poly


def _ecc(data: list[int], degree: int) -> list[int]:
    generator, rest = _generator(degree), list(data) + [0] * degree
    for index in range(len(data)):
        factor = rest[index]
        if factor:
            for offset, coef in enumerate(generator):
                rest[index + offset] ^= _mul(coef, factor)
    return rest[len(data):]


def _codewords(payload: bytes) -> tuple[int, list[int]]:
    for version, (per_block, blocks, ecc, _) in VERSIONS.items():
        capacity = per_block * blocks
        if len(payload) <= capacity - 2:
            break
    else:
        raise ValueError(f"too long for QR version 6-M: {len(payload)} bytes")
    bits = [0, 1, 0, 0] + [(len(payload) >> i) & 1 for i in range(7, -1, -1)]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    chunks = [data[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    corrections = [_ecc(chunk, ecc) for chunk in chunks]
    out = [chunk[i] for i in range(per_block) for chunk in chunks]
    out += [block[i] for i in range(ecc) for block in corrections]
    return version, out


MASKS = (
    lambda y, x: (x + y) % 2 == 0,
    lambda y, x: y % 2 == 0,
    lambda y, x: x % 3 == 0,
    lambda y, x: (x + y) % 3 == 0,
    lambda y, x: (y // 2 + x // 3) % 2 == 0,
    lambda y, x: x * y % 2 + x * y % 3 == 0,
    lambda y, x: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda y, x: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Matrix:
    def __init__(self, version: int) -> None:
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]
        self._function_patterns(version)

    def put(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x], self.fixed[y][x] = dark, True

    def _function_patterns(self, version: int) -> None:
        size = self.size
        for i in range(size):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        ring = max(abs(dx), abs(dy))
                        self.put(x, y, ring not in (2, 4))
        centers = VERSIONS[version][3]
        for cy in centers:
            for cx in centers:
                if (cx, cy) in ((6, 6), (6, centers[-1]), (centers[-1], 6)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)  # резерв под формат, настоящие биты — после выбора маски

    def format_bits(self, mask: int) -> None:
        data = ECL_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        size = self.size
        for i in range(6):
            self.put(8, i, bit(i))
        self.put(8, 7, bit(6))
        self.put(8, 8, bit(7))
        self.put(7, 8, bit(8))
        for i in range(9, 15):
            self.put(14 - i, 8, bit(i))
        for i in range(8):
            self.put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.put(8, size - 15 + i, bit(i))
        self.put(8, size - 8, True)

    def place(self, codewords: list[int]) -> None:
        size, index, total = self.size, 0, len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = (right + 1) & 2 == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.fixed[y][x] and index < total:
                        self.dark[y][x] = (codewords[index >> 3] >> (7 - (index & 7))) & 1 == 1
                        index += 1
            right -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        rule = MASKS[mask]
        return [[dark ^ (not fixed and rule(y, x)) for x, (dark, fixed) in enumerate(zip(row, flags))]
                for y, (row, flags) in enumerate(zip(self.dark, self.fixed))]


def _penalty(grid: list[list[bool]]) -> int:
    size, score = len(grid), 0
    lines = grid + [list(column) for column in zip(*grid)]
    finder = ([True, False, True, True, True, False, True, False, False, False, False],
              [False, False, False, False, True, False, True, True, True, False, True])
    for line in lines:
        run, prev = 0, None
        for cell in line:
            run = run + 1 if cell == prev else 1
            prev = cell
            if run == 5:
                score += 3
            elif run > 5:
                score += 1
        for start in range(size - 10):
            if line[start:start + 11] in finder:
                score += 40
    for y in range(size - 1):
        for x in range(size - 1):
            if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                score += 3
    dark, total = sum(map(sum, grid)), size * size
    return score + ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10


def encode(text: str) -> list[list[bool]]:
    """Матрица модулей (True — тёмный) без тихой зоны."""
    version, codewords = _codewords(text.encode("utf-8"))
    matrix = _Matrix(version)
    matrix.place(codewords)
    best = None
    for mask in range(8):
        matrix.format_bits(mask)
        grid = matrix.masked(mask)
        score = _penalty(grid)
        if best is None or score < best[0]:
            best = (score, mask, grid)
    matrix.format_bits(best[1])
    return matrix.masked(best[1])


def render(grid: list[list[bool]], ascii_only: bool = False) -> str:
    size = len(grid) + 2 * QUIET
    padded = [[False] * size for _ in range(QUIET)]
    padded += [[False] * QUIET + row + [False] * QUIET for row in grid]
    padded += [[False] * size for _ in range(QUIET)]
    if ascii_only:
        return "\n".join("".join("##" if dark else "  " for dark in row) for row in padded)
    padded.append([False] * size)
    glyphs = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    lines = []
    for top, bottom in zip(padded[0::2], padded[1::2]):
        cells = "".join(glyphs[(a, b)] for a, b in zip(top, bottom))
        lines.append(f"\033[30;107m{cells}\033[0m")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    ascii_only = "--ascii" in args
    args = [arg for arg in args if arg != "--ascii"]
    if len(args) != 1:
        print("usage: python -m cctv.qr [--ascii] TEXT", file=sys.stderr)
        return 2
    print(render(encode(args[0]), ascii_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
