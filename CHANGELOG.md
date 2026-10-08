# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).

## [0.1.3] — 2026-10-08

Everything a person reads now comes from the language catalogs: English by default, Russian
in full. Before, engine errors, the daily detector summary, the healthcheck and config errors
were Russian whatever the bot language was.

### Changed
- Bot language: `/lang` → `CCTV_LANG` (`lang` in config.toml) → the owner's Telegram language
  → English. The owner's Telegram language is a hint and no longer pins the language the way
  `/lang` does; the first-run wizard also follows `CCTV_LANG` before the owner is set.
- Engine refusals (camera scan, camera probe, registry writes) carry a catalog key
  (`error_key`, `error_params`) next to the English `error` text; the bot shows them in its own
  language. An older bridge without the key still works (its text is shown as is).
- Engine-side text — the daily detector summary (`summary-<day>.txt`, `cctv diag-summary
  --lang`), owner notifications (`cctv-notify`), supervisor log and healthcheck, config
  errors — uses `CCTV_LANG` of that process, else English. For a Russian summary set `lang`
  in `[common]` (or `[engine]`), not only in `[bot]`.
- The topic passport shows the "registered" status from the catalog instead of a raw code.
- `compose.yml` passes `CCTV_LANG` from `.env` to both containers: one line
  `CCTV_LANG=ru` sets the language of the whole installation (empty — as before).

### Added
- Website (`docs/site`, en + ru) rebuilt as a landing page: who it is for, features of 0.1.2,
  how a detection happens, install in 3 steps, bot commands and topics, mock-ups with the bot's
  real texts, supported cameras, honest limits, "Built on Artel". Both READMEs gained "What it
  does", "How to use" and the same block.
- `scripts/site-sync.py`: the release version (`cctv/__init__.py`) and the platform name
  (`docs/site/site.json`) live in `<!--site:KEY-->` marks on the site and in both READMEs.
  `--check` runs in CI, in the release workflow (the tag must equal the version) and in the
  export script: a version bump without the site and READMEs, or a release without its
  CHANGELOG section, fails.
- CI gate: no Russian string literals in `cctv/` outside `cctv/i18n/locales` (docstrings,
  process logs and the transliteration table are allowlisted with reasons); en/ru catalogs
  must have the same keys and placeholders, English must have no Cyrillic, and every key
  used in code must exist.

## [0.1.2] — 2026-10-08

Fewer missed people and fewer false alarms. Cameras that detect people themselves can now
confirm a person instead of only gating the detector, a small figure far away is no longer
lost between frames, and a camera can be told to trust its own silence (a bush or a bag no
longer becomes a person at dawn). A diagnostic journal and a daily summary show which
candidates were rejected and why, so a miss can be checked after the fact.

### Added
- `human_gate_mode = "confirm"` for cameras with `camera_human_events`: the camera's own
  person signal (ONVIF FieldDetector) confirms instead of gating. From 15 s before to 60 s
  after the signal (`human_confirm_pre_sec`, `human_confirm_post_sec`) YOLO looks at every
  sampled frame, one frame is enough (`human_confirm_hits = 1`) at a lower threshold
  (`human_confirm_confidence = 0.20`), and the still-object filter only rejects a dead box
  (`human_confirm_still_inside`: less than 3 % of the box changed — a bucket revealed when a
  gate opens scored 0.20–0.33 in the window and became a "person"; 0 skips the filter). A broken
  subscription never confirms (no fail-open). Without the signal the usual rules apply.
  Confirmed events are logged with `confirm=1` and do not feed threshold calibration.
- `human_quiet_cameras = "a,b"` (confirm mode only, empty by default): on the listed cameras,
  while the subscription is healthy and the camera has had no person in its zone around the
  frame (5-minute hold + 60 s), YOLO needs `human_quiet_confidence` (0.60) instead of the
  usual threshold. Meant for a camera whose own person detection covers every real visit
  while YOLO keeps firing on scene texture (an IR-lit bush at dawn, shadows on a bag at noon).
  Do not list a camera that misses people itself (a crouching person at the frame edge).
  The diag journal reason is `camera_quiet`.
- Miss diagnostics (`state/diag`, `diag_enabled = 0` turns off): a JSONL journal per day of
  detector events, camera person signals and rejected candidates — an episode of frames with
  YOLO ≥ 0.20 that did not become an event, with the reason (`below_threshold`,
  `single_frame`, `still`, `gate_floor`), max confidence, box and, throttled to one per camera
  per 5 minutes, a snapshot of the best frame. Log line `person_reject camera=… reason=… max=…`.
  `gate_stats` counts `floor_closed` frames (the camera base threshold would pass them, the
  floating noise floor did not).
  Detector events in the journal carry the box and the still-filter verdict too, so a false
  event can be explained from the journal alone, without the engine log.
- Daily summary after local midnight (`diag_utc_offset = "+03:00"`) to
  `state/diag/summary-<day>.txt|json` and the log (`diag_daily`, `diag_pair`); on demand —
  `python -m cctv diag-summary [--day …] [--json]`. Camera pairs looking at one place
  (`diag_pairs = "a:b"`, window `diag_pair_window_sec = 120`): an event with a pair on the
  other camera, a single one with no trace on the other camera (explained: someone stayed on
  one side), or a suspicious one — the other camera had a trace (its own person signal or a
  rejected candidate) but no event. Suspicious items and "camera saw a person, no event" are
  candidates for a manual check, not a verdict.

### Changed
- A frame where YOLO sees a person keeps both gates open for `person_hit_hold_sec` (5 s) so the
  second frame of the series actually reaches YOLO. Before, a frame skipped by the frame-diff
  gate reset the series: a small figure on a wide field changing the image below the floating
  noise floor scored 0.68–0.83 on every other frame and never produced an event.
- Still-object filter: when more than 15 % of the frame outside the box changed
  (`person_still_unreliable_outside`; dusk, IR switch, exposure), the reference frame is
  unreliable and the detection passes as "don't know" instead of being rejected. The
  confidence bypass is 0.55 instead of 0.70. A box that was already judged still at the same
  place (IoU ≥ 0.5, remembered for 24 h, survives restarts in `state/<camera>.still.json`)
  gets neither the bypass nor the "don't know" pass, so a static object at 0.53–0.60 does not
  turn into people at dawn.

### Fixed
- Container health check with `internal_tls = true` in `config.toml`: it probed the mTLS
  bridge over plain http and always failed (the engine never became `healthy`). The check now
  reads the config file like the roles do and, with mTLS, verifies that the bridge port listens.

## [0.1.1] — 2026-10-06

### Added
- `/add` activates new Hikvision cameras: discovery reads the activation state without a
  password (`/SDK/activateStatus`, SADP), offers "🔐 Activate" per camera and "Activate all";
  activation V3 (RSA-3072 + AES-CBC challenge) and the older ISAPI challenge protocol, in
  pure Python. The confirmation word comes first, then the admin password (owner's own,
  checked against Hikvision rules, or generated by the engine); the message is deleted
  before any network call. The engine writes the password to `activation-vault.json`
  (0600) before touching a camera, trusts only the camera's state plus one login with the
  new password (no retries, so the camera never locks itself), enables ONVIF with a
  separate user, probes the stream and the bot adds the camera to the registry. Result per
  camera; the password goes to the owner privately once, never to the group.
- `/add` for other brands: discovery recognises the camera brand from what it serves without a
  password (RTSP/HTTP banners, the web page; Axis `systemready` `needsetup`) — never a login
  attempt. A new camera of a brand without auto-activation (Dahua, Imou, Uniview, Tantos, Axis,
  Hanwha, Reolink, TP-Link VIGI, EZVIZ, Milesight, TVT, Xiongmai, Ajax) gets per-brand manual
  setup instructions and a "🔑 Password set" button into the usual login path, instead of a
  dead end; see [docs/vendor-activation.md](docs/vendor-activation.md).
- Engine log `detector_skipped_segments camera=… count=… lag=…` when the recording buffer
  drops segments the detector has not reached yet (CPU overload); for 10 minutes after that
  the detector state is `behind`, and the bot shows "detector falls behind" in the camera
  panel and reports the change.

### Changed
- `person_gate_mode` defaults to `enforce`. In `shadow` the frame-diff gate only counts its
  decision while YOLO still runs on every sampled frame (about 10× the CPU on 4 cameras);
  keep `shadow` for diagnosing the gate.

### Fixed
- ONVIF probe kept the port of the device service when the Media XAddr has none.
- Camera person gate (ONVIF FieldDetector): a lost `inactive` message no longer keeps the
  camera's active…inactive interval open forever. A target without a confirmation lives
  300 s, targets are keyed by Rule/ObjectId, state resets on resubscription; the state is
  logged as `human_gate_state`. Gate decisions are unchanged.
- Under CPU overload the detector silently skipped buffer segments (see Added/Changed above).

## [0.1.0] — 2026-10-03

First public release.

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

### Known limitations
- Caption time zone is fixed to Europe/Moscow.
- No on-disk archive; Telegram chat is the long-term store.
