"""Локализация: строки — в locales/<lang>.json, ключ → текст.

en — основной язык, ru — полный перевод; es, pt-BR, uk, id — каркас (те же ключи с
пустыми значениями, переводы — вторым проходом). Порядок поиска: запрошенный язык →
базовый язык без региона (pt-BR → pt) → en → сам ключ, чтобы недостающий перевод не
ронял ответ. Пустая строка в каталоге — «не переведено», то есть тоже фолбэк.

Язык выбирается цепочкой ``choose``: первый кандидат, для которого есть каталог.
У бота это /lang → CCTV_LANG → language_code владельца → en; у движка, сторожа и
консольных команд — CCTV_LANG → en (``env_lang``).
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
from typing import Mapping

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


def has(key: str) -> bool:
    return bool(_catalog(DEFAULT_LANG).get(key))


def match(code: str | None) -> str | None:
    """Код языка (ru, pt-br, en-US, ru_RU…) → имя каталога; нет каталога — None."""
    names = {name.lower(): name for name in available()}
    raw = (code or "").strip().replace("_", "-").lower()
    if not raw:
        return None
    if raw in names:
        return names[raw]
    base = raw.split("-")[0]
    if base in names:
        return names[base]
    # pt → pt-BR: другого португальского каталога нет
    return next((name for key, name in sorted(names.items()) if key.split("-")[0] == base), None)


def resolve(code: str | None) -> str:
    """language_code Telegram (ru, pt-br, en-US…) → язык из каталога, иначе en."""
    return match(code) or DEFAULT_LANG


def choose(*candidates: str | None) -> str:
    """Первый кандидат, для которого есть каталог; незнакомый код не перебивает следующий."""
    for candidate in candidates:
        found = match(candidate)
        if found:
            return found
    return DEFAULT_LANG


def env_lang(env: Mapping[str, str] | None = None) -> str:
    """Язык процессов без собеседника (движок, сторож, консоль): CCTV_LANG → en."""
    env = os.environ if env is None else env
    return resolve(env.get("CCTV_LANG"))


class CodedError(Exception):
    """Ошибка с ключом каталога вместо готовой фразы.

    Бросает её движок, а читает человек на своём языке: мост отдаёт боту ключ и
    параметры (``error_key``/``error_params``), бот переводит. ``str(exc)`` —
    текст на языке процесса (CCTV_LANG, иначе en), для журнала и консоли.
    """

    prefix = "error"

    def __init__(self, code: str, /, **params) -> None:
        self.code = code
        self.params = params
        super().__init__(code)

    @property
    def key(self) -> str:
        return f"{self.prefix}.{self.code}"

    def text(self, lang: str | None = None) -> str:
        return t(self.key, lang or env_lang(), **self.params)

    def __str__(self) -> str:
        return self.text()

    def reply(self) -> dict:
        """Отказ в ответе моста: ключ для перевода у бота и текст для прочих клиентов."""
        return {"ok": False, "error": self.text(DEFAULT_LANG), "error_key": self.key,
                "error_params": self.params}
