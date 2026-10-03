# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Person detector: three model families with one interface — YOLOX (default),
  YOLOv5, YOLOv8-style (anchor-free) — chosen by `person_model_family` + `person_model`,
  with a start threshold per family; see [docs/models.md](docs/models.md).
- Bot: `/model` (and "🧠 Detector model" in "Control") shows the supported families, the
  model files found and the active model, and switches the model on the fly: the engine
  loads and checks the new model for every camera while the old one keeps working, then
  swaps between frames — no restart, no lost events. A file that does not load or belongs
  to another family is rolled back automatically, with a message in "Control".
- Per-camera detector threshold: auto-calibration after a model switch and on
  "🔄 Calibrate" ("🎯 Thresholds" in "Control") — p99 of the scene background score plus a
  margin, not below the model start threshold, capped by confirmed passes (`PERSON_HITS`
  events and/or the camera's ONVIF human signal); manual threshold per camera that
  survives restarts. It measures noise, not recall — see [docs/models.md](docs/models.md).

### Changed
- The project is named **Dozorcam**: repository `egorchenkov/dozorcam`, image
  `ghcr.io/egorchenkov/dozorcam` (default in `compose.yml`, tag `latest`; set
  `CCTV_IMAGE`/`CCTV_IMAGE_TAG` for a local build). Python package, commands and
  `CCTV_*` variables keep their names.
- License: Apache-2.0 (was a draft MIT), with `NOTICE`; `THIRD_PARTY.md` lists what the
  image bundles — the GPL-3.0-or-later FFmpeg build, YOLOX-Tiny (Apache-2.0), Python
  packages. The three files are also in the image under `/usr/share/doc/dozorcam`.
- Project website (GitHub Pages, English and Russian) from `docs/site`.
- The image ships YOLOX-Tiny (Apache-2.0) instead of YOLOv5n (AGPL-3.0). New installs
  use YOLOX-Tiny. Installs that set `CCTV_PERSON_MODEL` or have
  `/usr/share/cctv/models/yolov5n.onnx` keep YOLOv5n; after upgrading the image, put
  `yolov5n.onnx` into `config/models/` and set `person_model_family = "yolov5"` to keep it.

## [0.1.0] — first public release (unreleased)

### Added
- Engine: rolling recording buffer per camera (RAM/tmpfs), RTSP credential proxy,
  motion gate with per-camera noise floor, YOLO person detector, still-object filter
  (objects that do not move are not people), optional person gate from Hikvision camera
  analytics (ONVIF FieldDetector: `off | shadow | enforce`).
- Telegram bot: forum group with one topic per camera and a "Control" topic, snapshots
  and clips on demand and on detection, pause/resume, rename, retire cameras.
- First-run wizard: one-time owner code in the logs → `/start <code>`, group setup by
  the bot itself, `/add` with ONVIF/WS-Discovery search or a manual RTSP URL; the
  password message is deleted before it reaches the engine.
- i18n: English and Russian, `/lang`; more languages (es, pt-BR, uk, id) are planned.
- Container: one multi-arch image (linux/amd64, linux/arm64), compose with host network,
  non-root, read-only rootfs, tmpfs buffer limits, healthchecks; empty config waits for
  setup instead of crash-looping.
- Single config directory (`config.toml`, `cameras.json`, `secrets.toml`), environment
  overrides, optional mutual TLS between engine and bot.
- CI: tests on pull requests, multi-arch image to GHCR on version tags.

### Known limitations
- Caption time zone is fixed to Europe/Moscow.
- No on-disk archive; Telegram chat is the long-term store.
