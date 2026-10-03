"""Каркас i18n: фолбэк по языку и ключу не роняет ответ."""
from __future__ import annotations

import unittest
from unittest import mock

from cctv import i18n


class I18nFallbackTest(unittest.TestCase):
    def setUp(self):
        i18n._catalog.cache_clear()
        self.addCleanup(i18n._catalog.cache_clear)

    def test_planned_languages_have_catalogs(self):
        self.assertTrue({"en", "ru", "es", "pt-BR", "uk", "id"} <= set(i18n.available()))

    def test_fallback_chain(self):
        catalogs = {"en": {"hi": "Hello, {name}", "only_en": "en"}, "pt": {"hi": "Olá, {name}"}}
        with mock.patch.object(i18n, "_catalog", side_effect=lambda lang: catalogs.get(lang, {})):
            self.assertEqual(i18n.t("hi", "pt-BR", name="R"), "Olá, R")
            self.assertEqual(i18n.t("only_en", "ru"), "en")
            self.assertEqual(i18n.t("missing", "ru"), "missing")


class CatalogsTest(unittest.TestCase):
    """en — основной, ru — полный; каркас остальных — те же ключи (пустые = фолбэк на en)."""

    def load(self, lang: str) -> dict:
        import json

        return json.loads((i18n.LOCALES / f"{lang}.json").read_text(encoding="utf-8"))

    def test_en_and_ru_are_complete_and_consistent(self):
        import string

        en, ru = self.load("en"), self.load("ru")
        self.assertEqual(set(en), set(ru))
        for key in en:
            with self.subTest(key=key):
                self.assertTrue(en[key] and ru[key], key)
                fields = lambda text: {f for _, f, _, _ in string.Formatter().parse(text) if f}
                self.assertEqual(fields(en[key]), fields(ru[key]))

    def test_skeleton_languages_have_every_key(self):
        en = self.load("en")
        for lang in ("es", "pt-BR", "uk", "id"):
            with self.subTest(lang=lang):
                self.assertEqual(set(en), set(self.load(lang)))

    def test_untranslated_skeleton_falls_back_to_english(self):
        i18n._catalog.cache_clear()
        self.assertEqual(i18n.t("wizard.owner_set", "en"), i18n.t("wizard.owner_set", "pt-BR"))

    def test_every_key_used_by_the_bot_exists(self):
        import re

        source = (i18n.LOCALES.parents[1] / "bot" / "bot.py").read_text(encoding="utf-8")
        source += (i18n.LOCALES.parents[1] / "bot" / "main.py").read_text(encoding="utf-8")
        en = self.load("en")
        used = set(re.findall(r'_t\(\s*"([a-z_]+\.[a-z0-9_.]+)"', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - set(en))


if __name__ == "__main__":
    unittest.main()
