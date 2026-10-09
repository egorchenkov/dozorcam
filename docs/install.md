# Installing and running Dozorcam

Russian: [install.ru.md](install.ru.md). Modes, commands and settings:
[reference.md](reference.md). All documents: [README.md](README.md).

- [Requirements](#requirements)
- [Install in one command](#install-in-one-command)
- [The first 10 minutes](#the-first-10-minutes)
- [Manual install with docker compose](#manual-install-with-docker-compose)
- [The dozorcam helper](#the-dozorcam-helper)
- [Update](#update)
- [Backup and restore](#backup-and-restore)
- [Uninstall](#uninstall)
- [Upgrading from 0.1.x](#upgrading-from-01x)

## Requirements

- Linux amd64 or arm64 with 2 GB RAM or more (how much for your cameras —
  [sizing.md](sizing.md) and the calculator on the [website](https://egorchenkov.github.io/dozorcam/#sizing));
  32-bit ARM (armv7) is not supported.
- Docker with the compose plugin — the installer offers to install it.
- Access to `api.telegram.org`; IP cameras in a network the node can reach.
- A bot token: [@BotFather](https://t.me/BotFather) → `/newbot`.

## Install in one command

```bash
curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | sh
```

What it does, in order (POSIX sh, readable before running):

1. Checks Docker and the compose plugin; without them offers the official `get.docker.com`
   script (y/N).
2. Downloads `compose.yml`, `.env.example`, `CHANGELOG.md` and the `dozorcam` helper of the
   latest release into `~/dozorcam` (no git) and checks them against the release `SHA256SUMS`.
3. Asks for the bot token and checks it with Telegram (`getMe`).
4. Takes the language from `$LANG` (`CCTV_LANG`) and the time zone from the host (`CCTV_TZ`).
5. Sizes the RAM video buffer from the machine memory: 25 % of RAM within 512 MB–4 GB; warns
   below 2 GB.
6. Pins the image by digest, starts it, waits for the health check and prints the owner link
   `https://t.me/<bot>?start=<code>` with a QR code.

Running it again in the same directory keeps `.env` (token, settings). Non-interactive
install — everything by variables:

| Variable | What |
|---|---|
| `DOZORCAM_TOKEN` | bot token |
| `DOZORCAM_LANG` | `en` or `ru` |
| `DOZORCAM_TZ` | IANA zone, e.g. `Europe/Berlin` |
| `DOZORCAM_DIR` | install directory, default `~/dozorcam` |
| `DOZORCAM_VERSION` | a release, default the latest |
| `DOZORCAM_INSTALL_DOCKER=1` | install Docker without asking |

```bash
curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | DOZORCAM_TOKEN=<token> DOZORCAM_TZ=Europe/Berlin sh
```

## The first 10 minutes

1. **Owner.** Open the link printed by the installer (or scan the QR) and press Start — you
   are the owner. The link is lost: `~/dozorcam/dozorcam code`.
2. **Where events go.** The bot asks "Where should camera events go?" and pins a progress
   message "Step 1/3 · 2/3 · 3/3".
   - "📱 Here, to this chat" — done in one tap; good for 1–3 cameras.
   - "👥 To a group with topics" — create a group, turn on Topics, add the bot as admin with
     "Manage topics", "Delete messages" and "Pin messages". The Android / iPhone / Desktop
     buttons show where each step is in your client; the bot notices the ready group by itself
     within a minute (`/setup` — right now). It creates the "Control" topic with the camera map.
   - A group can be connected later with `/mode`; nothing is lost.
3. **First camera.** `/add` — the bot searches the node's networks and WS-Discovery. Tap a
   camera, send `login password` in one message (deleted at once), give a name. A camera
   without ONVIF: `/add <IP>` (typical RTSP paths are tried) or `/add rtsp://host:port/path`.
   Found nothing — the bot says where it looked; add `CCTV_DISCOVERY_NETWORKS` to `.env` and
   `dozorcam restart`.
4. **First frame.** It arrives where events go; the camera appears on the map. Tap it for the
   card: 📷 Frame, 🎞 Clip 30 s.
5. **Person detection.** Look at the scene, then turn detection on in ⚙️ Settings of the card.
   Walk in front of the camera — a snapshot with a box arrives, and "🎞 Clip around this
   frame" under it. `/help` lists everything for your mode.

## Manual install with docker compose

```bash
git clone https://github.com/egorchenkov/dozorcam.git && cd dozorcam
cp .env.example .env && sed -i 's/^CCTV_BOT_TOKEN=.*/CCTV_BOT_TOKEN=<token>/' .env
docker compose pull --ignore-pull-failures && docker compose up -d
docker compose logs bot | grep SETUP     # one-time code: send /start <code> to the bot
```

Set `CCTV_IMAGE_TAG` to a release (e.g. `CCTV_IMAGE_TAG=0.3.0`), `CCTV_LANG` and
`CCTV_TZ` in `.env`. If the registry is unreachable, compose builds the same image locally
from the `Dockerfile`. The `dozorcam` helper works here too: `scripts/dozorcam` expects to sit
next to `compose.yml` and `.env` (copy it there).

## The dozorcam helper

```
dozorcam status | logs [-f] [engine|bot] | code | restart | start
dozorcam update [-y] [VERSION] | backup [FILE] | restore [-y] FILE
dozorcam uninstall [-y] [--volumes] [--image]
```

`restart` re-creates the containers with the current `.env` — use it after editing `.env`.

## Update

```bash
~/dozorcam/dozorcam update
```

Reads the latest release from GitHub Releases, shows the changelog between your version and
it, updates `compose.yml` and the image pinned by digest, starts it and waits for the health
check. If the check fails, the previous `.env` and `compose.yml` come back and the previous
image starts again (it stays on the machine — rollback needs no network). `update 0.3.0` —
a specific version, `-y` — without questions. The bot shows "⬆️ Version X is available" on the
camera map and in `/version` once a day (`CCTV_UPDATE_CHECK=0` — off).

## Backup and restore

```bash
~/dozorcam/dozorcam backup        # ~/dozorcam/backups/dozorcam-<date>-<time>.tar.gz
~/dozorcam/dozorcam restore ~/dozorcam/backups/dozorcam-<date>-<time>.tar.gz
```

One tar.gz: `.env` (with the bot token — keep the file private), `compose.yml`, `config/`,
the engine and bot state volumes (camera registry with passwords, owner, group, map). The
containers pause for the copy and start again. The video buffer is not included: it lives in
RAM. On another machine: run the installer, then `restore` there.

## Uninstall

```bash
~/dozorcam/dozorcam uninstall                      # asks about the state and the image separately
~/dozorcam/dozorcam uninstall -y --volumes --image  # everything, without questions
```

The state volumes (camera list, camera passwords, owner) and the image are removed only if
you say so; `~/dozorcam` with `.env`, `config/` and `backups/` stays — delete it yourself if
you do not need it.

## Upgrading from 0.1.x

- 0.1.x has no `dozorcam` helper: download `dozorcam` from the assets of the
  [latest release](https://github.com/egorchenkov/dozorcam/releases/latest) next to
  `compose.yml` and `.env`, `chmod +x dozorcam`, then `./dozorcam update`. A git checkout can
  also `git pull && docker compose pull && docker compose up -d`.
- The delivery mode stays "topic per camera"; topics and panels are reused, the "Control"
  topic gets the camera map instead of the old panel.
- Times were always Moscow time before 0.3.0; now they follow `CCTV_TZ` and are UTC without
  it. Moscow users: `CCTV_TZ=Europe/Moscow`. `diag_utc_offset` is gone.
- The bot needs the "Pin messages" admin right in the group; it tells you if it is missing.
