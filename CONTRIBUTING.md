# Contributing

Dozorcam is a personal home project published **as is**, without any warranty or
promise of support (see [LICENSE](LICENSE)). Issues and pull requests are welcome, but
may be answered slowly or not at all, and features are added only if they fit the
author's own setup.

If you still want to help:

- Bugs: describe the camera (vendor, model, firmware), what you expected and what
  happened; attach `docker compose logs` with passwords, tokens and addresses removed.
- Pull requests: keep them small and focused, add a test (`tests/`), and make sure
  `python -m pytest -q` passes. CI runs the same suite on every PR.
- Translations: copy `cctv/i18n/locales/en.json` to `<lang>.json` and translate the values;
  missing keys fall back to English.
- Never commit real camera addresses, MAC addresses, tokens or chat IDs — use
  `192.0.2.0/24` (RFC 5737) and obvious placeholders in tests and docs.

By submitting a contribution you agree to license it under the Apache-2.0 license of this project.

## По-русски

Домашний проект, публикуется «как есть»: без гарантий и без обязательств по поддержке.
Issue и PR принимаются, но ответ может быть нескорым или не прийти вовсе. PR — небольшие,
с тестом и зелёным `pytest`; в тестах и документации — только адреса `192.0.2.0/24` и
явные заглушки вместо реальных токенов, MAC и chat_id.
