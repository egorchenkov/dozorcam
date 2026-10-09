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

    def test_every_key_used_anywhere_exists(self):
        """Ключи i18n.t/_t/t и коды ошибок движка (DiscoveryError → discovery.* и т.д.)."""
        import re

        prefixes = {"DiscoveryError": "discovery", "Invalid": "registry", "writer_error": "registry",
                    "ConfigError": "config", "SettingsError": "config"}
        en, used = self.load("en"), set()
        for path in i18n.LOCALES.parents[1].rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            used |= set(re.findall(r'(?:i18n\.t|_t|(?<![\w.])t)\(\s*"([a-z_]+\.[a-z0-9_.]+)"', source))
            for name, prefix in prefixes.items():
                used |= {f"{prefix}.{code}" for code in re.findall(rf'\b{name}\(\s*"([a-z_]+)"', source)}
        self.assertGreater(len(used), 250)
        self.assertEqual(set(), used - set(en))

    def test_english_has_no_cyrillic_and_russian_is_russian(self):
        import re

        cyrillic = re.compile("[А-Яа-яЁё]")
        en, ru = self.load("en"), self.load("ru")
        self.assertEqual([], [key for key, text in en.items() if cyrillic.search(text)])
        # Без кириллицы в ru — только строки из одних подстановок и значков.
        neutral = {key for key, text in ru.items() if not cyrillic.search(text)}
        self.assertEqual({"thr.button_set", "scan.manual_brand", "config.unreadable", "time.format",
                          "event.shared", "map.section", "map.page",
                          # марки клиентов Telegram и «Dozorcam 0.3.0» — имена, а не текст
                          "client.android.button", "client.ios.button", "version.current"}, neutral)


class LanguageChoiceTest(unittest.TestCase):
    """Выбор языка: явная настройка → CCTV_LANG → language_code владельца → en."""

    def test_choose_takes_first_known_language(self):
        self.assertEqual("ru", i18n.choose("ru", "en"))
        self.assertEqual("en", i18n.choose("", None, "en", "ru"))
        self.assertEqual("ru", i18n.choose("xx", "ru-RU"))   # незнакомый код не перебивает следующий
        self.assertEqual("pt-BR", i18n.choose("pt"))
        self.assertEqual("en", i18n.choose(None, "", "klingon"))

    def test_env_lang(self):
        self.assertEqual("ru", i18n.env_lang({"CCTV_LANG": "ru"}))
        self.assertEqual("en", i18n.env_lang({}))
        self.assertEqual("en", i18n.env_lang({"CCTV_LANG": "xx"}))

    def bot(self, cfg_lang="", owner=7, owner_ids=(), allowed=(7, 8)):
        import types

        from cctv.bot import bot as botmod
        from cctv.bot.state import State

        state = State(":memory:")
        self.addCleanup(state.close)
        cfg = types.SimpleNamespace(lang=cfg_lang, allowed_user_ids=frozenset(allowed),
                                    owner_ids=frozenset(owner_ids), chat_id=None)
        core = botmod.CctvBot(cfg, state, bridge=None, tg=None)
        if owner:
            state.set_service(botmod.OWNER_KEY, str(owner))
        return core, state, botmod

    def test_cctv_lang_beats_language_code_beats_english(self):
        core, _state, _ = self.bot()
        self.assertEqual("en", core.lang)                       # ничего не задано
        core.note_language(7, "ru-RU")
        self.assertEqual("ru", core.lang)                       # language_code владельца
        core, _state, _ = self.bot(cfg_lang="en")
        core.note_language(7, "ru")
        self.assertEqual("en", core.lang)                       # CCTV_LANG сильнее language_code
        core, _state, _ = self.bot(cfg_lang="ru")
        core.note_language(7, "en")
        self.assertEqual("ru", core.lang)

    def test_lang_command_beats_cctv_lang(self):
        import asyncio

        core, _state, _ = self.bot(cfg_lang="ru")
        self.assertEqual("ru", core.lang)
        answer = asyncio.run(core.set_language(7, ["en"]))
        self.assertEqual("en", core.lang)
        self.assertIn("en", answer)

    def test_only_owner_language_counts(self):
        core, _state, _ = self.bot()
        core.note_language(8, "ru")      # в allow-list, но не владелец
        core.note_language(None, "ru")
        self.assertEqual("en", core.lang)
        core, _state, _ = self.bot(owner=None, owner_ids={8, 9})
        core.note_language(8, "ru")      # владелец из CCTV_OWNER_IDS
        self.assertEqual("ru", core.lang)
        core.note_language(9, "en")      # второй владелец не перебивает первого
        self.assertEqual("ru", core.lang)
        core.note_language(8, "en")      # тот же владелец сменил язык клиента
        self.assertEqual("en", core.lang)

    def test_wizard_owner_language_is_a_hint_not_a_choice(self):
        import asyncio

        core, state, botmod = self.bot(owner=None, allowed=())
        code = core.setup_code()
        answer = asyncio.run(core.on_start(5, 5, "private", [code], "ru"))
        self.assertEqual("ru", core.lang)
        self.assertIn("владелец", answer)
        self.assertIsNone(state.get_service(botmod.LANG_KEY))  # /lang не занят подсказкой
        core.cfg.lang = "en"
        self.assertEqual("en", core.lang)

    def test_wizard_before_owner_follows_cctv_lang_then_client(self):
        import asyncio

        core, _state, _ = self.bot(cfg_lang="ru", owner=None, allowed=())
        core.setup_code()
        self.assertIn("Код не подошёл", asyncio.run(core.on_start(5, 5, "private", ["WRONG"], "en")))
        core, _state, _ = self.bot(owner=None, allowed=())
        core.setup_code()
        self.assertIn("Код не подошёл", asyncio.run(core.on_start(5, 5, "private", ["WRONG"], "ru")))
        self.assertIn("Wrong code", asyncio.run(core.on_start(6, 6, "private", ["WRONG"], None)))


class EngineErrorTranslationTest(unittest.TestCase):
    """Отказ движка (ключ каталога) человек читает на языке бота, а не движка."""

    def test_coded_error_text_and_reply(self):
        from cctv.engine import camera_discovery as discovery

        exc = discovery.DiscoveryError("not_private", network="8.8.8.0/24")
        self.assertEqual("сеть не приватная: 8.8.8.0/24", exc.text("ru"))
        reply = exc.reply()
        self.assertEqual({"ok": False, "error": "the network is not private: 8.8.8.0/24",
                          "error_key": "discovery.not_private",
                          "error_params": {"network": "8.8.8.0/24"}}, reply)

    def test_bot_translates_bridge_refusal(self):
        import types

        from cctv.bot import bot as botmod
        from cctv.bot.state import State
        from cctv.engine import cctv_provision

        state = State(":memory:")
        self.addCleanup(state.close)
        reply = cctv_provision.Invalid("registry_full", limit=8).reply()
        for lang, expected in (("ru", "в реестре уже 8 камер"), ("en", "the registry already has 8 cameras")):
            cfg = types.SimpleNamespace(lang=lang, allowed_user_ids=frozenset(), owner_ids=frozenset())
            core = botmod.CctvBot(cfg, state, bridge=None, tg=None)
            with self.subTest(lang=lang):
                self.assertEqual(expected, core._refusal(reply, "registry.refused"))
                self.assertEqual(core._t("error.not_found"),
                                 core._refusal({"ok": False, "error": "not_found"}, "registry.refused"))
                self.assertEqual("old bridge text", core._refusal({"error": "old bridge text"}, "registry.refused"))
                self.assertEqual(core._t("registry.refused"), core._refusal({"ok": False}, "registry.refused"))
                broken = dict(reply, error_params={"unexpected": 1})
                self.assertEqual(reply["error"], core._refusal(broken, "registry.refused"))


if __name__ == "__main__":
    unittest.main()
