# Dozorcam

**English** · [Русский](README.ru.md) · [Website](https://egorchenkov.github.io/dozorcam/) · [Documentation](docs/README.md)

Self-hosted video surveillance with a Telegram front end. The engine keeps a short
recording buffer from your IP cameras, looks for people (motion gate → YOLO) and sends
snapshots and clips to Telegram: to your private chat, a group, or a forum group with a
topic per camera or per location. No cloud: video lives in the node's RAM and in your chat.

Current release: **<!--site:version-->0.3.1<!--/site-->** ·
[Changelog](CHANGELOG.md) · [Releases](https://github.com/egorchenkov/dozorcam/releases)

> Provided **as is** (see [CONTRIBUTING](CONTRIBUTING.md)): a one-person home project,
> no support guarantees.

## What it does

- **People, not motion.** Motion gate with a per-camera noise floor, then an ONNX person
  detector (YOLOX-Tiny bundled, YOLOv5/YOLOv8 optional) with a per-camera threshold; cameras
  with their own person detection can confirm it; things that never move are not people.
- **Snapshots and clips in Telegram.** A snapshot with the person boxed, a clip around that
  moment on a tap, a fresh frame or a 30 s clip on demand. Events of one camera within a
  minute are merged into one post; hashtags `#gate #cottage` filter the feed by camera or place.
- **Your delivery mode.** Here in the private chat (1–3 cameras), one group without topics, a
  topic per camera or a topic per location — switch any time with `/mode`, nothing is lost.
- **Camera map and cards.** One pinned map with every camera, its state and today's events;
  tap a camera for its card: frame, clip, pause, notifications, name, location, settings.
- **Setup from the chat.** A wizard with a pinned progress, `/add` finds cameras (ONVIF,
  WS-Discovery, typical RTSP paths by IP and login) and even activates a new Hikvision.
- **Explains its decisions.** Diagnostic journal of rejected candidates and a daily summary.
- **English or Russian throughout** (`CCTV_LANG` or `/lang`), time in your zone (`CCTV_TZ`).
- **No cloud.** No account, no telemetry; the long-term copy is your own chat. The only
  request besides Telegram is a daily look at the latest release on GitHub (nothing is sent;
  `CCTV_UPDATE_CHECK=0` turns it off).

## Quick start

You need Linux (amd64 or arm64) with 2 GB RAM or more and a bot token from
[@BotFather](https://t.me/BotFather) (`/newbot`). Then one command:

```bash
curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | sh
```

The installer checks Docker (offers to install it), downloads the release files into
`~/dozorcam` and checks them against `SHA256SUMS`, asks for the token, sizes the RAM buffer,
starts the containers and prints a link with a QR code. The rest happens in Telegram:

1. **Open the link** (`t.me/<your bot>?start=<code>`) — you are the owner. Lost it:
   `~/dozorcam/dozorcam code`.
2. **Where should events go?** "📱 Here, to this chat" finishes in one tap; "👥 To a group with
   topics" — the bot shows where to create the group and which admin rights to give
   (Android / iPhone / Desktop buttons) and notices by itself when the group is ready.
3. **`/add`** — the bot finds cameras, asks for the login and password (the message is
   deleted), then a name. The first frame arrives; turn person detection on in ⚙️ Settings
   of the camera card after a look at the scene.

Manual install with `docker compose`, non-interactive install and every installer option:
[docs/install.md](docs/install.md).

## Update, backup, uninstall

```bash
~/dozorcam/dozorcam update            # latest release: changelog, pull by digest, rollback if unhealthy
~/dozorcam/dozorcam backup            # one tar.gz: .env, config/, camera registry, bot state
~/dozorcam/dozorcam restore <file>    # back on this or another machine
~/dozorcam/dozorcam uninstall         # stop and remove containers; --volumes, --image on request
```

Also `status`, `logs [-f] [engine|bot]`, `restart` (applies a changed `.env`) and `code`.
The bot tells you about a new version on the camera map and in `/version`.

## How to use

| Command | What it does |
|---|---|
| `/start <code>` | once, in a private chat: makes you the owner |
| `/add` | searches for cameras; `/add <IP>` or `/add rtsp://host:port/path` for one outside the search |
| `/menu` | list of cameras (the map itself is pinned) |
| `/cam <name>` | a camera card here (by name, `#hashtag` or id) |
| `/mode` | where events go: a topic per camera, a topic per location or one chat (owner) |
| `/invite` | a one-time link for one more person, valid 24 h (owner, private chat) |
| `/setup` | rechecks the group and tells which admin right is missing |
| `/model` | person detector model, switched on the fly with automatic rollback |
| `/lang en\|ru` | interface language |
| `/help`, `/version` | commands and buttons for your mode; installed and latest version |

Reply to an event snapshot with any text to get a clip around that moment. Modes, map,
card, merging and every button: [docs/reference.md](docs/reference.md#delivery-modes).

## Will it run on my box?

| Cameras (2 MP) | RAM | CPU | Example |
|---|---|---|---|
| 1–2 | 2 GB | 2 cores | Raspberry Pi 4/5 (2 GB+), Orange Pi 5, any mini PC |
| 3–4 | 4 GB | 4 cores | Raspberry Pi 5 (4–8 GB), Intel N100 mini PC |
| 5–8 | 8 GB | 4 cores | Intel N100/N305 mini PC, a home server |
| 9–16 | 16 GB | 4+ cores | a home server |

Disk: about 2 GB for the image; video is kept in RAM, not on disk. **Will not run:** 1 GB
RAM boards (Raspberry Pi 3, Zero 2), 32-bit ARM (armv7 — no image). Per-camera numbers,
the calculator and how they were measured: [website](https://egorchenkov.github.io/dozorcam/#sizing),
[docs/sizing.md](docs/sizing.md).

## Cameras

ONVIF (Profile S), any RTSP camera, cameras without ONVIF by IP and login (typical RTSP
paths of Reolink, TP-Link Tapo/VIGI, Uniview, Axis, Xiongmai/XMEye are tried in turn), and
Hikvision with activation of a new camera from `/add`. Tested live on Hikvision G0/G2/G5 and
Tantos; table of paths and checked cameras: [docs/cameras.md](docs/cameras.md).

## Limitations

- No on-disk archive: the buffer holds 10 minutes per camera in RAM; the chat is the long-term copy.
- The node must reach `api.telegram.org`; containers use the host network ([why](docs/reference.md#networking)).
- Threshold auto-calibration measures background noise, not recall ([details](docs/models.md)).
- UI languages: English and Russian. Comments in the code are mostly in Russian.

## Documentation

[Install and update](docs/install.md) · [Reference: modes, commands, configuration,
network, TLS](docs/reference.md) · [Detector models and thresholds](docs/models.md) ·
[Cameras](docs/cameras.md) · [Sizing](docs/sizing.md) · [All documents](docs/README.md)

Development: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt &&
.venv/bin/pip install -e . --no-deps && .venv/bin/python -m pytest -q` —
[docs/reference.md](docs/reference.md#development).

## Built on a general-purpose platform

> **🧩 Built on <!--site:platform.en-->a general-purpose platform for building applications and AI agents through Telegram bots<!--/site-->**
>
> Dozorcam is built on <!--site:platform.en-->a general-purpose platform for building applications and AI agents through Telegram bots<!--/site-->, developed by
> Roman Egorchenkov. The platform's source code will be published on GitHub soon — follow the
> updates in his repositories:
> [github.com/egorchenkov](https://github.com/egorchenkov).

## License

[Apache-2.0](LICENSE), copyright 2026 Roman Egorchenkov ([NOTICE](NOTICE)).
The container image bundles third-party software under its own licenses, including a
GPL-3.0-or-later build of FFmpeg — see [THIRD_PARTY.md](THIRD_PARTY.md).
