# Dozorcam reference

Russian: [reference.ru.md](reference.ru.md). Installation, update and backup:
[install.md](install.md). All documents: [README.md](README.md).

- [Delivery modes](#delivery-modes)
- [Camera map and camera card](#camera-map-and-camera-card)
- [Commands and buttons](#commands-and-buttons)
- [People: owner, allowed, invited](#people-owner-allowed-invited)
- [Cameras](#cameras)
- [Person detector](#person-detector)
- [Configuration](#configuration)
- [Networking](#networking)
- [Engine ↔ bot link and TLS](#engine--bot-link-and-tls)
- [Layout](#layout)
- [Development](#development)

## Delivery modes

Where a camera's events go is a *route*. There are three presets; the owner switches them
with `/mode` (or "🔀 Where events go" on the map). Cameras, their ids and history stay; topics
are reused; the old map says where it moved. If Telegram refuses (rights, topics), the
previous mode is kept.

| Preset | Where events go | Good for |
|---|---|---|
| One chat, no topics (`flat`) | the group itself, or — without a group — a private chat with each allowed person | 1–3 cameras, the fastest start |
| Topic per camera (`camera`, default before 0.3.0) | each camera has its own topic with a pinned card | 4 cameras and more |
| Topic per location (`location`) | topics like "Home" or "Cottage", events of several cameras inside with hashtags | several places |

- **Setup wizard.** After the owner code the bot asks "Where should camera events go?":
  "📱 Here, to this chat" finishes the setup in one tap (flat mode in the private chat, the map
  is pinned there, `/add` works right there); "👥 To a group with topics" waits for a group.
  From 4 cameras the wizard recommends topics. A pinned progress message "Step 1/3 owner ·
  2/3 where events go · 3/3 first camera" is edited as you go and unpinned at the end.
- **Group checks.** A group with topics needs the bot as admin with "Manage topics", "Delete
  messages" and "Pin messages"; a flat group — the last two. The bot rechecks the group by
  itself on every promotion, on the move to a supergroup and once a minute for a day after it
  reported what is missing (turning Topics on sends the bot no update); `/setup` rechecks now.
  The Android / iPhone / Desktop buttons show where to do each step in that client.
- **Location.** 📍 Location on a camera card sets the camera tag `location`; without it the
  registry site is used (unless it equals the camera name), otherwise the "Cameras" topic.
  A camera can keep its own topic in any mode.
- **Hashtags.** Every caption ends with the camera and location hashtags (`#gate #cottage`):
  tap one and Telegram filters the chat by that camera or place.
- **Merging.** Events of one camera within 60 s edit its previous post — a fresh frame and
  "+N within a minute · latest 13:01:40"; the clip button is around the fresh frame. The window
  counts from the first post, so a busy camera still gets one post a minute.
  `CCTV_EVENT_MERGE_SEC` (`event_merge_sec`), `0` — every event separately.
- **Private chat copies.** Independently of the mode, everyone allowed can get a camera's
  events in their private chat: "📩 To my DM" on the camera card (or the topic keyboard).
  The group feed stays silent, the private copy comes with sound; the card line says how many
  people get it (in the group) or whether you do (in your private chat). Without a group the
  private chat is the feed itself and the same button switches its sound. The bot can only
  write to people who pressed Start in its private chat.
- **Motion clips** arrive as a reply to the event post in every place, with that place's
  sound; a clip of an event merged into a post is silent. A "person" after "motion" within the
  merge window is a new post where it rings, not a silent edit.
- **Requested frames and clips** go to whoever asked, into the same topic or chat.

## Camera map and camera card

One pinned message per place: the "Control" topic, the group itself in the flat mode, or a
private chat of each person without a group. It shows "📍 Cameras: 4 · online: 4 · events
today: 12", cameras in sections by location, in a private chat 📩 (or 🔕 without a group) for your own subscription, a button
per camera, ➕ Add camera, 🧠 Detector model, 🎯 Thresholds, 🔀 Where events go, and
"⬆️ Version X is available" when there is one. With more than 12 cameras in several
locations the buttons go by location pages.

Tap a camera and the same message turns into its **card**: status, location and hashtags,
📷 Frame, 🎞 Clip 30 s, 🔄 Status, 📩 To my DM, ⏸ Pause, ✏️ Name, 📍 Location,
⚙️ Settings, 🗑 Retire and "◀ To the map"; a card left open returns to the map after 10
minutes. The pinned panel of a camera topic is the same card. `/cam <name>` sends a card as a
message (by name, `#hashtag` or camera id; in a camera topic without a name — that camera).

A new camera starts with person detection **off**: look at the scene, then turn it on in
⚙️ Settings. Under every event — "🎞 Clip around this frame": the clip is cut from the buffer
around the moment the person was seen, even if the detector was a few seconds behind; a reply
to the event snapshot with any text does the same.

## Commands and buttons

| Command | What it does |
|---|---|
| `/start <code>` | once, in a private chat: makes you the owner (or opens the `t.me/<bot>?start=<code>` link) |
| `/add` | searches for cameras; `/add <IP>`, `/add rtsp://host:port/path [rtsp://… detector stream]` |
| `/menu` | list of cameras; in a camera topic — its buttons |
| `/cam <name>` | a camera card here |
| `/mode [camera\|location\|flat]` | where events go (owner) |
| `/invite` | one-time link for one more person, valid 24 h, and the list with "Remove" (owner, private chat) |
| `/setup` | rechecks the group, tells which admin right is missing |
| `/model` | person detector model and file, switched on the fly with automatic rollback |
| `/lang en\|ru` | interface language; default `CCTV_LANG`, else the owner's Telegram language, else English |
| `/help` | commands and buttons for the current mode |
| `/version` | installed version and the latest release |

## People: owner, allowed, invited

The owner is whoever sent the setup code first (`dozorcam code` shows it again until then).
Allowed people are the owner, `CCTV_ALLOWED_USER_IDS` and people invited with `/invite`;
commands and buttons answer only them. In a group mode everyone in the group sees the feed;
without a group each allowed person gets events in a private chat with their own map and own
notifications. `CCTV_CHAT_ID` / `CCTV_ALLOWED_USER_IDS`, when set, win over
the wizard (there is no setup code then). `CCTV_OWNER_IDS` — who gets alarms in a private
chat (default: allowed).

## Cameras

| Kind | How |
|---|---|
| ONVIF (Profile S) | discovery in the node's own /24 networks and via WS-Discovery (or `CCTV_DISCOVERY_NETWORKS`); stream and snapshot URIs from ONVIF Media |
| Any RTSP camera | `/add rtsp://host:port/path`, snapshot is taken from the stream |
| By IP + login, no ONVIF | `/add <IP>`: typical paths of Reolink, TP-Link Tapo/VIGI, Uniview, Axis, Xiongmai/XMEye and generic ones, 3 s per path, recognised brand first; the bot shows which worked and offers the second stream to the detector — [cameras.md](cameras.md) |
| Hikvision (ISAPI) | RTSP and `/ISAPI/Streaming/channels/101/picture` templates where ONVIF is silent; optional person gate from the camera's own analytics (ONVIF FieldDetector, `human_gate_mode`) |

The login and password are sent as one message `login password`; the bot deletes it before
it reaches the engine and refuses passwords inside an address. An empty search says where it
looked: list other subnets in `CCTV_DISCOVERY_NETWORKS` or enter the address (✍️).

A new, not yet activated Hikvision is activated from `/add` itself: discovery marks it "🔐",
the bot asks for the confirmation word, then for the admin password (or generates one), and
the engine activates the camera (ISAPI activation V3 or the older challenge protocol), checks
the password with one login, enables ONVIF with a separate ONVIF user, checks the stream and
adds the camera; "Activate all" does the same for every new camera found. The password is
shown to the owner once, in a private chat, and a copy stays in the engine state
(`activation-vault.json`, 0600). Other vendors should be activated with their own tools first.

## Person detector

| Family | Default file | Start threshold | Weights license | Shipped in the image |
|---|---|---|---|---|
| YOLOX (default) | `yolox_tiny.onnx` | 0.30 | Apache-2.0 (Megvii) | yes |
| YOLOv5 | `yolov5n.onnx` | 0.35 | AGPL-3.0 (Ultralytics) | no — download it yourself |
| YOLOv8-style (YOLOv8, YOLO11) | `yolov8n.onnx` | 0.30 | AGPL-3.0 (Ultralytics) | no — download it yourself |

- AGPL-3.0 weights are not redistributed: if you put YOLOv5/YOLOv8 files into
  `config/models/` (readable by uid 10001), their license applies to your install. Step by step:
  [models.md](models.md#where-to-put-your-own-weights). Choose with `/model` or
  `person_model_family` / `person_model` in `config.toml`.
- **Threshold per camera.** After a model switch and on "🔄 Calibrate" (🎯 Thresholds on the
  map) the engine collects ~600 background frames per camera and sets the threshold to the
  99th percentile of the background score plus 0.10, never below the model start threshold.
  Confirmed passes (2 consecutive frames and/or the camera's own ONVIF human signal) cap it
  from above. A manual threshold wins over the auto value, survives restarts and stops
  applying when the model is switched.
- **Confirmed by the camera.** Cameras with their own person detection can gate the detector
  or confirm a person (`human_gate_mode = "confirm"`); `human_quiet_cameras` lets a camera
  trust its own silence. A box is checked against the same spot seconds earlier, so a thing
  that never moves is not a person.
- **Honest limit:** auto-calibration finds a threshold *above the noise*; it cannot measure
  how many people are missed. If a camera misses passes, set its threshold manually.
- **Diagnostics.** The engine journals rejected candidates with the reason and writes a daily
  summary of possible misses (`python -m cctv diag-summary`).

## Configuration

`.env` next to `compose.yml` is all most installs need (see `.env.example`). Advanced
settings live in the config directory (`CCTV_CONFIG_PATH`, default `./config`, mounted
read-only): `config.toml` with `[common]`, `[engine]`, `[bot]` sections, `cameras.json`,
`secrets.toml` — see `deploy/config.example.toml`. A key `foo` becomes `CCTV_FOO`;
environment wins over the file; a process sees only `[common]` and its own section.
After editing `.env` run `dozorcam restart` (re-creates the containers).

| Variable | Default | What |
|---|---|---|
| `CCTV_BOT_TOKEN` | — | bot token (the installer asks and checks it) |
| `CCTV_LANG` | owner's Telegram language, else `en` | language of the whole installation: bot, summary, healthcheck |
| `CCTV_TZ` | UTC (the map warns) | IANA time zone of captions and of the summary day |
| `CCTV_EVENT_MERGE_SEC` | `60` | merge window of one camera's events; `0` — off |
| `CCTV_UPDATE_CHECK` | on | `0` — do not ask GitHub Releases for a new version |
| `CCTV_DISCOVERY_NETWORKS` | own /24 networks | where `/add` looks, e.g. `192.0.2.0/24,198.51.100.0/24` |
| `CCTV_CHAT_ID`, `CCTV_ALLOWED_USER_IDS`, `CCTV_OWNER_IDS` | wizard | fixed group and people instead of the wizard |
| `CCTV_TELEGRAM_API` | `api.telegram.org` | your own Bot API server |
| `CCTV_BUFFER_TMPFS` | installer: 370 MB × the cameras that fit into RAM whole (85 % of RAM with processes), at least 512 MB; `64m` with the buffer on disk | RAM for the video buffer of all cameras (a ceiling: RAM is used by recorded segments only) |
| `CCTV_BUFFER_DIR`, `CCTV_BUFFER_DISK` | `/var/lib/cctv/buffer` (tmpfs); installer `DOZORCAM_BUFFER=disk`: `/var/lib/cctv/buffer-disk` | the buffer on disk when RAM is short: the volume `engine-buffer` or a host directory `CCTV_BUFFER_DISK` owned by uid 10001; SSD only (~0.5 MB/s of writes per camera) |
| `CCTV_BUFFER_MAX_BYTES` | 400 MiB, installer: 200 MiB | buffer limit per stream (main and detector); all cameras must fit into `CCTV_BUFFER_TMPFS` — the engine has no common limit |
| `CCTV_SPOOL_TMPFS`, `CCTV_STORAGE_BUDGET_BYTES` | 512m, buffer + spool | engine transit and the storage budget |
| `CCTV_BRIDGE_PORT`, `CCTV_EVENTS_PORT`, `CCTV_RTSP_PROXY_PORT` | 8780, 8781, 28554 | host ports; a second install next to it needs others |
| `CCTV_IMAGE`, `CCTV_IMAGE_TAG` | `ghcr.io/egorchenkov/dozorcam`, pinned by digest | image |

| Path in the container | What | Variable |
|---|---|---|
| `/etc/cctv` (read-only) | `config.toml`, `cameras.json`, `secrets.toml`, `models/` | `CCTV_CONFIG_DIR` |
| `/var/lib/cctv/state` (volumes `engine-state`, `bot-state`) | kilobytes: camera registry, detector heartbeat, bot SQLite | `CCTV_STATE_DIR` |
| `/var/lib/cctv/buffer` (tmpfs) | 10 minutes of segments per stream | `CCTV_BUFFER_DIR` |
| `/var/lib/cctv/buffer-disk` (volume `engine-buffer` or `CCTV_BUFFER_DISK`) | the same buffer on disk, empty while the buffer is in RAM; not backed up | `CCTV_BUFFER_DIR=/var/lib/cctv/buffer-disk` |

The registry built from the chat is written by the engine to `state/cameras.json`; the
`cameras.json` of the config directory only seeds an empty state. Editing the config
directory restarts the chain inside the container. Without a token, or with a token rejected
by Telegram, the bot waits for a fix: `docker compose ps` shows unhealthy, the reason is in
`dozorcam logs bot`.

## Networking

Both containers use the host network: the engine needs WS-Discovery multicast and direct
RTSP to cameras, and the engine↔bot link without TLS is only allowed over loopback. The
price is no network isolation for the containers: they see every host interface and can
reach anything the host can. Everything else is locked down (non-root uid 10001, read-only
root filesystem, `cap_drop: ALL`, `no-new-privileges`), but if the node sits on an untrusted
network, restrict egress with the host firewall. Keeping cameras on a separate VLAN without
internet access is a good idea anyway. Outgoing requests: `api.telegram.org` (or
`CCTV_TELEGRAM_API`) and once a day `api.github.com` for the latest release.

## Engine ↔ bot link and TLS

Engine and bot talk over loopback (`127.0.0.1:8780` bridge, `:8781` bot event receiver); a
non-loopback address without TLS refuses to start. For a split install set
`CCTV_INTERNAL_TLS=1` (`internal_tls = true` in `[common]`) — mutual TLS both ways.

## Layout

```
cctv/engine/   bridge (HTTP API), pipeline (recording buffer, motion gate, YOLO person
               detector, still-object filter), RTSP credential proxy, provisioning, ONVIF discovery
cctv/bot/      Telegram interface (python-telegram-bot): routes (delivery modes), map, cards, wizard
cctv/i18n/     locales/<lang>.json — every string a person reads; CI checks en/ru parity
cctv/container container supervisor (role engine | bot) and its healthcheck
scripts/       install.sh, dozorcam helper, release-assets.sh, site-sync.py, sizing-bench.sh
deploy/        config.example.toml, secrets.example.toml, reference systemd units (non-container install)
tests/         pytest suite: engine, bot in every delivery mode, i18n gate, installer smoke test
```

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e . --no-deps
.venv/bin/python -m pytest -q
```

Contract tests need `ffmpeg`, `openssl` and `curl` and are skipped without them. Multi-arch
image without QEMU: `docker buildx build --platform linux/arm64,linux/amd64 -t dozorcam:dev .`
(needs the containerd image store, default since Docker 29, or `--output type=oci`). Every
string a person reads goes through `cctv/i18n/locales` (en and ru); CI fails on Russian
literals in code. `scripts/site-sync.py --check` keeps the version on the website and in
both READMEs equal to the release.
