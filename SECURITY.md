# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private vulnerability reporting
(*Security → Report a vulnerability* in this repository). Include the version or commit,
what an attacker can do, and steps to reproduce.

This is a one-person project maintained in spare time: expect an acknowledgement within
about two weeks and no guaranteed fix timeline. Only the latest release is supported.

## Scope and design notes

Things that are in scope — please report:

- anyone other than the owner/allowed users controlling the bot or receiving media;
- camera credentials leaking (logs, Telegram messages, state files, error texts);
- the engine HTTP API reachable from outside loopback without mutual TLS;
- container escapes or privilege gains beyond the documented setup.

Known trade-offs, documented and intentional:

- `network_mode: host` — containers share the host network stack (see README).
- Snapshots and clips are stored in Telegram; anyone in the forum group sees them.
- Camera passwords are kept in the engine state so it can reconnect; protect the host
  and its volumes accordingly.

## По-русски

Об уязвимостях — не в публичные issue, а через приватный отчёт GitHub
(*Security → Report a vulnerability*). Ответ — в пределах примерно двух недель, сроков
исправления проект не обещает; поддерживается только последний релиз.
