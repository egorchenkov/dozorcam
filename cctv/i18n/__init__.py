"""Локализация: строки — в locales/<lang>.json, ключ → текст.

en — основной язык, ru — полный перевод; es, pt-BR, uk, id — каркас (те же ключи с
пустыми значениями, переводы — вторым проходом). Порядок поиска: запрошенный язык →
базовый язык без региона (pt-BR → pt) → en → сам ключ, чтобы недостающий перевод не
ронял ответ. Пустая строка в каталоге — «не переведено», то есть тоже фолбэк.
"""
from __future__ import annotations

import functools
import json
import pathlib

LOCALES = pathlib.Path(__file__).with_name("locales")
DEFAULT_LANG = "en"


@functools.lru_cache(maxsize=None)
def _catalog(lang: str) -> dict[str, str]:
    path = LOCALES / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def available() -> list[str]:
    return sorted(path.stem for path in LOCALES.glob("*.json"))


def t(key: str, lang: str | None = None, /, **params) -> str:
    for candidate in (lang, (lang or "").split("-")[0], DEFAULT_LANG):
        if candidate and _catalog(candidate).get(key):
            text = _catalog(candidate)[key]
            return text.format(**params) if params else text
    return key
