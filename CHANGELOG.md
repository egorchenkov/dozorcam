# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning: [SemVer](https://semver.org/).
Russian: [CHANGELOG.ru.md](CHANGELOG.ru.md) (since 0.3.1).

## [0.3.1] — 2026-10-09

Stabilization of 0.3.0 after the first day in production: private chats, frames in topics and
the notification button. Hardware sizing recounted from measurements — less RAM for 3–8
cameras — and the video buffer can live on an SSD when RAM is short.

### Changed
- Events go to the feed **and** to private chats at once: the group (camera topic, location
  topic or the group itself) and a copy in the private chat of everyone allowed who pressed
  "📩 To my DM" on the camera card — in every mode. The group feed is silent, the private copy
  rings. Without a group the private chat is the feed and the button switches its sound.
  Before, a group bound in the flat mode took the feed away from private chats.
- The "🔔/🔕 Motion" button is now "📩 To my DM": on shared panels it no longer depends on
  who pressed last, the card line says how many people get the camera in private (group) or
  whether you do (private chat). Old keyboards with "🔔 Motion" keep working.
- A motion clip is a reply to its event post, with the sound of that place; clips of merged
  events are silent. A "person" after "motion" within the merge window is a new post where it
  rings instead of a silent edit.
- `/menu` puts the reply keyboard only in a camera topic and removes it where it cannot work.
- Texts follow the mode: `/mode` says where events go now, the wizard explains that a group
  is connected by adding the bot, menu and hints in the location and flat modes.

- Hardware sizing is recounted from new measurements (live installation of 0.3.0, bench on
  arm64 and amd64 with the detector on the main stream, as cameras added from the chat run):
  5 cameras fit into 4 GB of RAM (was 8 GB), 6–8 into 6 GB, 3–4 cameras need 2 cores instead
  of 4. 9–16 cameras need 6–8 cores instead of "4+": detection on the main stream costs three
  times more CPU in a quiet scene than the earlier bench on the detector stream showed. The
  table in `docs/sizing.md`, the website calculator and the installer use one model.
- The installer sizes the video buffer by the cameras that fit into RAM whole (processes and
  buffer within 85 % of RAM) instead of "25 % of RAM, 512 MB–4 GB", and says how many that is.
  The old rule did not count the up to 130 s the recorder writes between buffer cleanings and
  could overflow the tmpfs with as many cameras as it promised.

- The website and both READMEs describe the platform Dozorcam is built on instead of naming it:
  the working name "Artel" is dropped, the platform has no name yet.

### Added
- The video buffer can live on an SSD when RAM is short: `DOZORCAM_BUFFER=disk` for the
  installer (and `DOZORCAM_BUFFER_PATH` for a separate disk), or `CCTV_BUFFER_DIR` /
  `CCTV_BUFFER_DISK` in `.env`. RAM then holds only the processes (8 cameras — 4 GB). The
  installer and the calculator show the disk writes (~16 TB a year per 2 MP camera) and warn
  against HDDs and SD cards. The buffer volume is not backed up and is removed on uninstall.
- `scripts/sizing-bench.sh`: the detector on the main stream, the buffer on disk with disk
  writes, the installer's per-stream limit, memory minute by minute.

### Fixed
- Flat mode in a group with topics: buttons and the keyboard in a camera or location topic
  answer in that topic (they answered in General or refused), a reply to a frame in a topic
  gives the clip.
- Without a group, the console (➕ search, model, thresholds) answers whoever pressed, not the
  owner.
- A camera whose site equals its name joins the location topic of its neighbours on that site.
- After the map moves (mode change or a group added) the old map is unpinned and says why;
  coming back to topics pins the "Control" map again.
- Updating from 0.3.0 unpins its leftover "The camera map has moved…" message: 0.3.0 left it
  pinned above the live map in the group (after `/mode` back and forth) and in the private chat
  (after a group was added). Checked once on the first start; other pins are not touched.
- The settings card is redrawn after turning person detection on or off.
- "Person detection: turn off" no longer stops all cameras: the engine bridge rebuilt the
  registry entry from the RTSP proxy addresses (no login, not the camera) and replaced it whole,
  so the proxy and the whole engine went down. A change from the chat now edits only its own
  field of the entry on disk; turning detection off and on again gives the same file byte for
  byte. A stream address without login and password is refused instead of being written.
- Changing a camera's login and password, or turning person detection on, no longer drops the
  camera's own person gate (`camera_human_events`) and the detector substream
  (`detect_substream`); the password change keeps the stream paths of the entry.
- "🔑 Login and password" on the card of a camera added by its stream address (outside the
  search) answered "could not detect the camera stream": the password is now checked on the
  stream addresses of the entry, and a snapshot with the same login gets the new password too.
- Turning person detection on from the chat was refused by the registry writer ("the address is
  not rtsp") because the change carried only the flag.
- The last second of a clip is no longer torn (a smeared or broken last frame): a clip took the
  segment the recorder was still writing. Clips are cut only from closed segments — the same
  rule as the detector's — and a motion clip waits 21 s instead of 15 so that its window is
  closed. The same with the buffer in RAM and on an SSD.

## [0.3.0] — 2026-10-09

One release instead of the planned 0.2.0 and 0.2.1: Dozorcam is now installed with one command
and set up from the chat, and events no longer require a forum group. Choose where they go —
here in the private chat, one group without topics, a topic per camera or a topic per
location — and change it any time with `/mode`. A pinned camera map with cards replaces the
"Control" panel, events of a camera within a minute become one post, hashtags filter the feed,
`/invite` adds a person without a group. Cameras without ONVIF are added by IP and login, the
bot rechecks the group by itself and tells you about new versions, times follow your time zone.

### Upgrading from 0.1.x
- 0.1.x has no `dozorcam` helper yet: put `dozorcam` from the 0.3.0 release assets next to
  `compose.yml` and `.env` and run `./dozorcam update` (it fetches the new `compose.yml`, pins
  the image by digest and rolls back if the health check fails); a git checkout can also
  `git pull` and `docker compose pull && docker compose up -d`.
- Existing installations stay in the "topic per camera" mode: topics and panels are reused, the
  "Control" topic gets the camera map. Nothing has to be configured.
- Times were Moscow time; now they follow `CCTV_TZ` and are UTC without it (the map says so).
  Moscow users: `CCTV_TZ=Europe/Moscow`. `diag_utc_offset` is gone.
- The bot needs the admin right "Pin messages" in the group and tells you if it is missing.

### Added
- One-line installer: `curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | sh`
  (`scripts/install.sh`, Linux amd64/arm64, POSIX sh). It checks Docker and the compose plugin
  (offers the official get.docker.com script, y/N), downloads `compose.yml`, `.env.example` and
  the `dozorcam` helper of a release by tag (no git) and checks them against the release
  `SHA256SUMS`, asks for the bot token and checks it with Telegram (`getMe`), takes the language
  from `$LANG` and the time zone from the host, sizes the RAM video buffer from the machine
  memory (25 % of RAM, 512 MB–4 GB), pins the image by digest, starts it, waits for the health
  check and prints the owner link `https://t.me/<bot>?start=<code>` with a QR code — no more
  grep in the logs. Non-interactive: `DOZORCAM_TOKEN`, `DOZORCAM_LANG`, `DOZORCAM_TZ`, …
- `dozorcam` helper next to `compose.yml`: `status`, `logs`, `code` (owner link and QR again),
  `restart`, `update` (latest GitHub release, changelog between versions, pull by digest,
  automatic rollback to the previous version if the health check fails), `backup` / `restore`
  (one tar.gz: `.env`, `config/`, state volumes), `uninstall` (volumes and image only on
  request).
- `CCTV_TZ` (IANA, e.g. `Europe/Berlin`; `tz` in `[common]` of config.toml): time zone of
  captions and of the daily detector summary, passed by `compose.yml` to both containers.
  Without it times are UTC and the Control topic says so.
- `CCTV_TELEGRAM_API`: your own Bot API server (also used by the CI install test).
- Hashtags in event captions: the camera and its location (`#gate #cottage`). Tap one and
  Telegram filters the chat by that camera or place — in a topic, a group or a private chat.
- Events of one camera within 60 s are merged: the bot edits its previous post (fresh frame,
  "+N within a minute · latest 13:01:40") instead of sending a new one; the clip button of
  the merged post is around the fresh frame. Works with topics too. `CCTV_EVENT_MERGE_SEC`
  (default 60, `0` turns merging off); the window counts from the first post, so a camera
  that keeps seeing people still gets one post a minute.
- Delivery modes: where a camera's events go is a route with three presets — a topic per
  camera (the default of existing installations), a topic per location (camera tag `location`,
  otherwise the registry site; without a location — the "Cameras" topic) and flat (one chat
  without topics: the group itself or, without a group, a private chat with each allowed
  person; a requested frame or clip goes to whoever asked, sound follows each person's own
  subscription). A camera can keep its own topic in any mode.
- Camera map instead of the Control panel: one pinned message per place (the Control topic,
  the group itself in the flat mode, or a private chat of each person without a group) with a
  summary (cameras, online, events today), cameras in sections by location, a 🔕 mark for
  cameras without notifications and one button per camera; with more than 12 cameras in
  several locations the buttons go by location pages.
- Camera card: tap a camera on the map and the same message turns into its card (status,
  location and hashtags, snapshot, clip, pause, notifications, name, 📍 location, settings,
  removal) with "◀ To the map"; a card left open goes back to the map after 10 minutes. The
  pinned panel of a camera topic is the same card. Actions from a card on the map answer
  right there.
- `/cam <name>` — the card of a camera as a message here (by name, hashtag or camera_id;
  without a name in a camera topic — that camera); works on the map, in the camera's place
  and in a private chat with the bot.
- `/mode` — where events go: a topic per camera, a topic per location or one chat without
  topics (owner only). Cameras, their ids and history stay; topics are reused, the old map
  says where it moved. 📍 Location on a card sets the camera's location tag.
- `/invite` (owner, private chat) — a one-time link for one more person, valid 24 h, and the
  list of invited people with "Remove". An invited person is allowed like
  `CCTV_ALLOWED_USER_IDS`; without a group they get events in a private chat with their own
  map and their own notifications.
- Setup wizard, step 2: "Where should camera events go?" — "📱 Here, to this chat" finishes
  the setup in one tap (flat mode in the private chat, the camera map is pinned there, `/add`
  works right there) or "👥 To a group with topics" (as before). From 4 cameras the wizard
  recommends topics. A group can be connected later with `/mode`; nothing is lost.
- Flat mode in a group without topics: in the flat mode a regular group needs no topics and no
  "Manage topics" right — only admin with "Delete messages" and "Pin messages". Once ready it
  takes over the feed from private chats, and their maps say where the map moved. A group
  without topics in the topics mode suggests `/mode flat`.
- Setup progress: a pinned message in the owner's private chat — "Step 1/3 ✅ owner · Step 2/3
  ⬜ where events go · Step 3/3 ⬜ first camera" — edited as the steps are done and unpinned
  when all three are (the pin then belongs to the camera map).
- "🤖 Android / 🍏 iPhone / 💻 Desktop" buttons after "To a group with topics" and under the
  bot's "the group still needs …" message: where to create the group, turn on Topics, add the
  bot and give it admin rights in that client (text, no screenshots; without topics in the
  flat mode).
- The group is rechecked by itself, no `/setup` needed: on every promotion of the bot
  (`my_chat_member`), on the group's move to a supergroup, and once a minute for a day after
  the bot reported what is missing — turning Topics on sends the bot no update. The same list
  of missing things is not repeated in the group.
- `/help` — short map of commands and buttons for the current mode; `/version` — installed
  version and the latest release. Once a day the bot asks GitHub Releases for the latest
  version (nothing about the installation is sent); a newer one shows on the camera map as
  "⬆️ Version X is available — on the server: dozorcam update". `CCTV_UPDATE_CHECK=0`
  (`update_check = false` in `[bot]`) turns the check off.
- Camera by IP and login without knowing the RTSP path: if ONVIF is silent, the engine tries
  typical paths of Reolink, TP-Link Tapo/VIGI, Uniview, Axis, Xiongmai/XMEye
  (`/user=…&password=…&channel=1&stream=0.sdp`, built by the engine from the separately sent
  login — the bot still refuses passwords inside an address, now also `password=` in the
  path) and generic ones (`/live`, `/11`, `/live/ch00_0`, `/videoMain`, `/onvif1`), 3 s per
  path, recognised brand first; the bot shows which path worked and offers the second stream
  to the detector. All `401` means a wrong login, not "no stream". Passwords in a path are
  masked everywhere like `user:***@`. Table and checked cameras: `docs/cameras.md`.
- Documentation for 0.3.0: both READMEs are a short start (one-command install, three steps in
  Telegram, update/backup/uninstall, delivery modes, sizing); the reference moved to `docs/` with
  a table of contents — `docs/install.md` (installer, first 10 minutes, manual compose, update,
  backup, uninstall, upgrading), `docs/reference.md` (modes, map, card, commands, people,
  cameras, detector, configuration, network, TLS), `docs/sizing.md`, each with a Russian twin.
- Website: "Install in 1 command", "The first 10 minutes", delivery modes with the map, card,
  hashtags and merging, Update / Backup & restore / Uninstall, and a hardware calculator
  (cameras, resolution, activity → RAM, CPU, disk, a board that fits; no external resources).
- `scripts/sizing-bench.sh`: measures the engine on your machine with N synthetic cameras under
  docker CPU and memory limits; the `sizing` workflow runs it on an amd64 GitHub runner.
- For developers: `scripts/release-assets.sh` (installer assets and `SHA256SUMS` of a release),
  CI job `install-smoke` (install → backup → uninstall → restore → update → rollback against a
  local registry and mocked Telegram/GitHub), contract tests of the bot in every delivery mode.

### Changed
- An empty `/add` search says where it looked and why a camera may be missing (another
  subnet): list the networks in `CCTV_DISCOVERY_NETWORKS` or enter the address (✍️ button).
- Times were always Moscow time; now they follow `CCTV_TZ` (UTC if unset). The separate
  `diag_utc_offset` setting is gone — the summary day follows `CCTV_TZ` as well. Moscow users:
  set `CCTV_TZ=Europe/Moscow`.

### Fixed
- A Telegram outage (Bad Gateway, timeout, flood wait) while updating the pinned map or the
  "Control" message is no longer taken for a deleted message: the bot keeps the message and
  retries the edit on the next round. Before, each such error posted and pinned a new map next
  to the old one. Only a message that is really gone is posted again.
- The installer limits the buffer to 200 MB per stream (7 minutes of a 4 Mbit/s camera) instead
  of half the buffer: the engine prunes each stream by its own limit only, and two 4 Mbit/s
  cameras on a 2 GB machine (512 MB buffer, 256 MB per stream) overflowed the RAM buffer after
  about eight minutes, which stops recording. Now the buffer holds about one camera per 240 MB
  (2 GB RAM — 2 cameras, 4 GB — 4, 8 GB — 8), and the installer says so.
- `dozorcam restart` re-creates the containers with the current `.env` (`up -d --force-recreate`)
  instead of `docker compose restart`, which kept the old environment: a changed `CCTV_TZ`,
  `CCTV_DISCOVERY_NETWORKS` or `CCTV_UPDATE_CHECK` silently did not apply.
- The Telegram command menu ("/") follows the explicit installation language (`/lang`,
  `CCTV_LANG`) for every client, like the rest of the output. Before, with `lang = "ru"` a
  client with an English Telegram interface still saw the English menu. Without an explicit
  language the menu still follows the client (Russian for a Russian client, English otherwise);
  `/lang` updates the menu at once, not only after a restart.
- `/add rtsp://…` (and an address typed after "✍️ Enter address") asked for the camera login
  and password twice — once in the topic and once as a reply; now only as the reply.
- The group setup now also asks for the bot admin right "Pin messages": without it the camera
  panels were not pinned and creating a camera topic failed once before a retry. A missing pin
  right no longer breaks the camera topic.
- "No owner yet" and "wrong code" messages point to `dozorcam code` instead of `docker logs`.
- A reply to the bot's request (login and password, camera name) was taken for a reply to a
  frame ("this message is not a frame"); now it is the requested input unless the replied
  message is a frame.
- The first motion of a newly added camera was dropped ("motion for an unknown camera"): it
  arrived before the bot synced the registry after the pipeline restart. Now an event of a
  camera the bot does not know yet triggers the sync and is delivered.
- Without the camera's own topic (flat mode, topic per location) the bot no longer points to
  "its topic": the first frame, a new camera, retiring, renaming, the settings card and hints
  talk about the map and the card instead.
- The storage line of the Control topic shows the budget with one decimal: on a 2 GB machine
  it said "of 1 GiB" for 0.75 GiB.

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
