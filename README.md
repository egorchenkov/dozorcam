# Dozorcam

**English** · [Русский](README.ru.md) · [Website](https://egorchenkov.github.io/dozorcam/)

Self-hosted video surveillance with a Telegram front end. The engine keeps a short
recording buffer from your IP cameras, looks for people (motion gate → YOLO) and sends
snapshots and clips to a Telegram forum group: one topic per camera plus a "Control"
topic with system status. No cloud: video lives in the node's RAM buffer and in your chat.

Current release: **<!--site:version-->0.1.3<!--/site-->** ·
[Changelog](CHANGELOG.md) · [Releases](https://github.com/egorchenkov/dozorcam/releases)

> Provided **as is** (see [CONTRIBUTING](CONTRIBUTING.md)): a one-person home project,
> no support guarantees.

## What it does

- **People, not motion.** Motion gate with a per-camera noise floor, then an ONNX person
  detector (YOLOX-Tiny bundled, YOLOv5/YOLOv8 optional) with a per-camera threshold
  calibrated against the scene noise.
- **Confirmed by the camera.** Cameras with their own person detection (Hikvision, ONVIF
  FieldDetector) can gate the detector or, since 0.1.2, confirm a person
  (`human_gate_mode = "confirm"`): small or half-hidden figures are not missed.
- **Still objects are not people.** A box is checked against the same spot seconds earlier;
  a bag or a bush that never moves is rejected. A camera can be told to trust its own
  silence (`human_quiet_cameras`), so an IR-lit bush at dawn does not become a person.
- **Snapshots and clips in Telegram.** Snapshot with the person boxed in the camera topic,
  a clip around that moment on a tap, a fresh frame or a 30 s clip on demand.
- **Control from the chat.** "Control" topic with every camera's state and storage, detector
  model, thresholds; `/add` finds cameras and even activates a new Hikvision.
- **Explains its decisions.** A diagnostic journal of rejected candidates with the reason and
  a daily summary with possible misses (`python -m cctv diag-summary`).
- **One language throughout.** Since 0.1.3 everything a person reads — the bot, camera
  search and add errors, the daily summary, the healthcheck — is English or Russian, as you
  choose: `CCTV_LANG` for the whole installation or `/lang` in the chat.
- **No cloud.** No account, no telemetry; the long-term copy is your own chat.

## Install in 3 commands

You need Linux (arm64 or amd64), Docker with compose, and a bot token from
[@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/egorchenkov/dozorcam.git && cd dozorcam
cp .env.example .env && sed -i 's/^CCTV_BOT_TOKEN=.*/CCTV_BOT_TOKEN=<token>/' .env
docker compose pull --ignore-pull-failures && docker compose up -d
```

`docker compose pull` fetches the release image `ghcr.io/egorchenkov/dozorcam`; if it is
not reachable, `up` builds the same image locally from the `Dockerfile`.

The rest happens in Telegram:

1. `docker compose logs bot | grep SETUP` prints a one-time code; send `/start <code>`
   to the bot in a private chat — you become the owner. Interface language: `CCTV_LANG`
   in `.env` (`en` or `ru`) for the whole installation, otherwise your Telegram language,
   otherwise English; `/lang en|ru` in the chat overrides both.
2. Create a group with **Topics** enabled, add the bot as an admin with "Manage topics"
   and "Delete messages". The bot creates the "Control" topic itself and tells you if a
   permission is missing (`/setup` to retry).
3. `/add` — the bot scans for cameras (ONVIF/RTSP in the node's own /24 networks and via
   WS-Discovery, or `CCTV_DISCOVERY_NETWORKS`). Tap a camera, send `login password` in one
   message (deleted before it reaches the engine), give it a name — you get a topic and
   the first frame. Camera outside discovery: `/add rtsp://host:port/path [rtsp://… detector stream]`.
4. Optional: `/model` — person detector model (family and file), switched on the fly with
   automatic rollback; see [docs/models.md](docs/models.md).

## How to use

| Command | What it does |
|---|---|
| `/start <code>` | once, in a private chat: makes you the owner |
| `/setup` | finishes the group setup, tells which admin right is missing |
| `/add` | searches for cameras; `/add rtsp://host:port/path` for a camera outside the search |
| `/menu` | cameras and links to their topics |
| `/model` | person detector model, switched on the fly with automatic rollback |
| `/lang en\|ru` | interface language (default: `CCTV_LANG`, else your Telegram language, else English) |

- **Camera topic** — events arrive here. The pinned panel shows stream and detector state;
  buttons: 📷 Frame, 🎞 Clip 30 s, 🔄 Status, 🔔 motion notifications, ⏸ Pause, ✏️ Name,
  ⚙️ Settings, 🗑 Retire. A new camera starts with person detection off — turn it on in
  ⚙️ Settings after a look at the scene.
- **Clips** — "🎞 Clip around this frame" under every event cuts the clip from the buffer
  around the moment the person was seen, even if the detector was a few seconds behind.
- **"Control" topic** — state of all cameras and storage, ➕ Add camera, 🧠 Detector model,
  🎯 Thresholds (auto-calibration or manual per camera).

## Cameras

| Kind | How |
|---|---|
| ONVIF (Profile S) | discovery, stream and snapshot URIs via ONVIF Media |
| Any RTSP camera | `/add rtsp://host:port/path`, snapshot is taken from the stream |
| Hikvision (ISAPI) | built-in RTSP and `/ISAPI/Streaming/channels/101/picture` templates where ONVIF is silent; optional person gate from the camera's own analytics (ONVIF FieldDetector, `human_gate_mode`) |

Tested live on Hikvision G0/G2/G5 and Tantos cameras. A new, not yet activated Hikvision
is activated from `/add` itself: discovery marks it "🔐", the bot asks for the confirmation
word, then for the admin password (or generates one), and the engine activates the camera
(ISAPI activation V3 or the older challenge protocol), checks the password with one login,
enables ONVIF with a separate ONVIF user, checks the stream and adds the camera; "Activate
all" does the same for every new camera found. The password is shown to the owner once, in
a private chat, and a copy stays in the engine state (`activation-vault.json`, 0600).
Other vendors should be activated with their own tools first; they should work over
ONVIF/RTSP but are untested.

## Networking: `network_mode: host`, honestly

Both containers use the host network: the engine needs WS-Discovery multicast and direct
RTSP to cameras, and the engine↔bot link without TLS is only allowed over loopback. The
price is no network isolation for the containers: they see every host interface and can
reach anything the host can. Everything else is locked down (non-root uid 10001,
read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`), but if the node sits
on an untrusted network, restrict egress with the host firewall. Keeping cameras on a
separate VLAN without internet access is a good idea anyway.

## Person detector: models, thresholds, weight licenses

| Family | Default file | Start threshold | Weights license | Shipped in the image |
|---|---|---|---|---|
| YOLOX (default) | `yolox_tiny.onnx` | 0.30 | Apache-2.0 (Megvii) | yes |
| YOLOv5 | `yolov5n.onnx` | 0.35 | AGPL-3.0 (Ultralytics) | no — download it yourself |
| YOLOv8-style (YOLOv8, YOLO11) | `yolov8n.onnx` | 0.30 | AGPL-3.0 (Ultralytics) | no — download it yourself |

- Only YOLOX-Tiny is bundled. AGPL-3.0 weights are not redistributed with this project:
  if you put YOLOv5/YOLOv8 files into `config/models/`, their license applies to your
  install, not to this repository's Apache-2.0 code.
- Using YOLOv5/YOLOv8 anyway: download or export the ONNX file yourself, put it into
  `config/models/` (readable by uid 10001) and pick it with `/model` or
  `person_model_family`/`person_model` — step by step in
  [docs/models.md](docs/models.md#where-to-put-your-own-weights).
- Choose the model in `config.toml` (`person_model_family`, `person_model`) or from the
  bot (`/model`, switched on the fly with automatic rollback). Installs that already ran
  YOLOv5n keep it with the same 0.35 threshold until you choose otherwise.
- **Threshold per camera.** After a model switch and on "🔄 Calibrate" ("🎯 Thresholds" in
  "Control") the engine collects ~600 background frames per camera and sets the threshold
  to the 99th percentile of the scene's background score plus a margin of 0.10, never
  below the model start threshold. Confirmed passes (an event of `PERSON_HITS`=2
  consecutive frames and/or the camera's own ONVIF human signal) cap it from above, so a
  noisy scene does not lose people the engine has already seen. Until calibration
  finishes, the start threshold of the model applies. Any camera can get a manual
  threshold in the same menu; it wins over the auto value, survives restarts and stops
  applying when the model is switched.
- **Honest limit:** auto-calibration finds a threshold *above the noise*. It cannot measure
  recall — how many people are missed — without labelled people, and people the model
  never scores above the background are invisible to it. If a camera misses passes, set
  its threshold manually. Details and bench numbers: [docs/models.md](docs/models.md).

## Limitations

- Timestamps in captions are Europe/Moscow (time zone is not configurable yet).
- No on-disk archive: the buffer holds minutes in RAM; the Telegram chat is the long-term copy.
- The node must reach `api.telegram.org`.
- Threshold auto-calibration measures only background noise, not recall (see above).
- UI languages: English and Russian. More languages (es, pt-BR, uk, id) are planned; their
  files are empty skeletons for now and show English.
- Comments in the code are mostly in Russian.

## Layout

```
cctv/engine/   bridge (HTTP API), pipeline (recording buffer, motion gate, YOLO person
               detector, still-object filter), RTSP credential proxy, provisioning, ONVIF discovery
cctv/bot/      Telegram interface (python-telegram-bot), bridge client, event receiver
cctv/i18n/     locales/<lang>.json
cctv/container container supervisor (role engine | bot) and its healthcheck
deploy/        config.example.toml, secrets.example.toml, reference systemd units (non-container install)
tests/         pytest suite
```

## Configuration

`.env` next to `compose.yml` is all most installs need (see `.env.example`: ports, RAM
limits for the tmpfs buffer, image tag). Advanced settings live in the config directory
(`CCTV_CONFIG_PATH`, default `./config`, mounted read-only): `config.toml` with
`[common]`, `[engine]`, `[bot]` sections, `cameras.json`, `secrets.toml` — see
`deploy/config.example.toml`. A key `foo` becomes `CCTV_FOO`; environment wins over the file.

| What | Default | Variable |
|---|---|---|
| Config (read-only) | `/etc/cctv` | `CCTV_CONFIG_DIR` |
| State (kilobytes: detector heartbeat, camera registry, bot SQLite) | `/var/lib/cctv/state` | `CCTV_STATE_DIR` |
| Segment buffer (tmpfs) | `/var/lib/cctv/buffer` | `CCTV_BUFFER_DIR` |

Person detector model (YOLOX-Tiny bundled; YOLOv5/YOLOv8 weights you add yourself),
families and thresholds: [docs/models.md](docs/models.md).

Engine and bot talk over loopback (`127.0.0.1:8780` bridge, `:8781` bot event receiver).
For a split install set `internal_tls = true` — mutual TLS both ways.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e . --no-deps
.venv/bin/python -m pytest -q
```

Contract tests need `ffmpeg`, `openssl` and `curl` and are skipped without them.
Multi-arch image without QEMU: `docker buildx build --platform linux/arm64,linux/amd64 -t dozorcam:dev .`
(needs the containerd image store, default since Docker 29, or `--output type=oci`).

## Built on <!--site:platform.en-->Artel<!--/site-->

> **🧩 <!--site:platform.en-->Artel<!--/site--> — a universal platform for building apps through Telegram bots**
>
> Dozorcam is built with <!--site:platform.en-->Artel<!--/site-->, a universal platform for building applications
> through Telegram bots, developed by Roman Egorchenkov. The platform's source code will be
> published on GitHub soon — follow the updates in his repositories:
> [github.com/egorchenkov](https://github.com/egorchenkov).

## License

[Apache-2.0](LICENSE), copyright 2026 Roman Egorchenkov ([NOTICE](NOTICE)).
The container image bundles third-party software under its own licenses, including a
GPL-3.0-or-later build of FFmpeg — see [THIRD_PARTY.md](THIRD_PARTY.md).
