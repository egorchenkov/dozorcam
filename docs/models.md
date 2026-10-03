# Person detector models

The engine runs one ONNX person detector per camera (CPU, OpenCV DNN). Three model
families are supported. They differ only in how the network output is decoded; every
adapter returns the same `detect(frame) -> (found, score, box)`. There is no plugin
system: a model outside these three output formats is not supported.

| Family (`person_model_family`) | Default file | Input | Output layout | Start threshold | Weights license | In the image |
|---|---|---|---|---|---|---|
| `yolox` (default) | `yolox_tiny.onnx` | 416×416 | `(1, 3549, 85)` raw grid offsets, objectness × class | **0.30** | Apache-2.0 (Megvii) | yes |
| `yolov5` | `yolov5n.onnx` | 640×640 | `(1, 25200, 85)` decoded boxes, objectness × class | **0.35** | AGPL-3.0 (Ultralytics) | no |
| `yolov8` | `yolov8n.onnx` | 640×640 | `(1, 84, 8400)` decoded boxes, class only (no objectness) | **0.30** | AGPL-3.0 (Ultralytics) | no |

The `yolov8` family covers the Ultralytics anchor-free export format (YOLOv8, YOLO11 and
models exported the same way). The `yolov5` family also accepts the P6 variants (`*6.onnx`).

## Choosing a model

`config/config.toml`, section `[engine]` (or the matching `CCTV_*` environment variables):

```toml
[engine]
person_model_family = "yolov8"   # CCTV_PERSON_MODEL_FAMILY: yolov5 | yolox | yolov8
person_model = "yolov8n.onnx"    # CCTV_PERSON_MODEL: file name or absolute path
# person_confidence = 0.30       # CCTV_PERSON_CONFIDENCE: overrides the start threshold
```

- `person_model` is a file name looked up in `/etc/cctv/models` (your files) and then in
  `/usr/share/cctv/models` (the image). Without it the family's default file is used.
- Without `person_model_family`:
  - if `CCTV_PERSON_MODEL` is set, or `/usr/share/cctv/models/yolov5n.onnx` exists (images
    and installs from before this change), the engine keeps running **YOLOv5n exactly as
    before** (same file, same 0.35 threshold);
  - otherwise (new installs) it runs the bundled **YOLOX-Tiny**.
- An unknown family, a missing file or a file of a different family stops person detection
  for the camera (`person_model_unavailable` in the heartbeat) instead of silently
  falling back to another model. The engine log shows the active choice on start:
  `person_model camera=… family=… file=… confidence=… reason=…`.

## Switching from the bot

`/model` (or "🧠 Detector model" in the "Control" topic) shows the supported families, the
`*.onnx` files found in the model directories and the active model. Pick a family, then a
file:

- the engine builds the new model for every camera and runs a test frame through each,
  while the current model keeps processing frames; only when all are ready it swaps them
  between two frames. The engine is not restarted, the recording buffer and the event
  queue are untouched: frames before the swap are checked by the old model, after it — by
  the new one;
- if the file does not load (not ONNX, damaged, missing) or its output does not match the
  family, the new model is discarded and the old one keeps running (it was never stopped);
  "Control" gets a message with the reason;
- the threshold after a switch is the family start threshold (scores of different families
  are not comparable), until per-camera auto-calibration adjusts it (see below);
- the choice is stored in the engine state (`person_model.json`) and overrides
  `person_model_family` / `person_model` / `person_confidence` from `config.toml` after a
  restart too. If the chosen file is gone at start, the engine logs `person_model_rollback
  stage=start` and falls back to `config.toml`. No choice made in the bot — the model from
  `config.toml` is used exactly as before.

Only file names from the model directories are accepted; a path cannot be sent from chat.

## Where to put your own weights

Only YOLOX-Tiny is shipped. YOLOv5/YOLOv8 weights are AGPL-3.0 and you download them
yourself.

1. Export or download the ONNX file (fixed 640×640 input, 80 COCO classes, `person` = class 0):
   - YOLOv5n: `https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx`;
   - YOLOv8n: `pip install ultralytics && yolo export model=yolov8n.pt format=onnx imgsz=640`.
2. Put it into the `models/` directory of the config directory (compose: `./config/models/`,
   mounted read-only as `/etc/cctv/models`); the file must be readable by uid 10001
   (`chmod 0644`).
3. Set `person_model_family` (and `person_model` if the file name differs from the
   default) in `config.toml` and restart the engine: `docker compose restart engine`.

## Start thresholds

The start threshold is used until the per-camera auto-calibration has enough data. The
values come from a bench on a labelled archive of 4 home cameras (373 events, 97 clips,
~2650 night IR background frames; the same event logic as the engine: 2 consecutive
frames above the threshold, still-object filter on):

- **YOLOX-Tiny 0.30** — the highest common threshold that kept every person pass
  (recall 0.963 at 0.30 with 1 false event; at 0.35 — 0.951 / 1). Median score on people
  0.77. Per-camera best thresholds ranged 0.30–0.70.
- **YOLOv5n 0.35** — the threshold this project has always used; recall 0.877 / 4 false
  events on the same set. Median score on people 0.51.
- **YOLOv8n 0.30** — recall 0.951 (77 of 81 passes) with 1 false event; at 0.35 recall
  drops to 0.877 with the same 1 false event (on the camera where people sit behind a
  railing the median score is 0.32). Median score
  on people 0.70. On this set YOLOv8n is not better than YOLOX-Tiny, which is why YOLOX
  stays the default.

Scores of different families are not comparable: switching the model also means a
different threshold, so set `person_confidence` only together with the family.

## Per-camera threshold: auto-calibration and manual override

"🎯 Thresholds" in "Control" (also a button in `/model`) shows the threshold each camera
with person detection actually uses and where it comes from: **manual**, **auto** or the
model **start** threshold.

When it runs: after every model switch (all cameras, the old threshold is dropped at
once — it belongs to the old model) and on "🔄 Calibrate". Without either, nothing
changes: an install that never used the menu keeps the threshold from `config.toml`.

How the value is found (`cctv/engine/threshold_calibration.py`):

1. **Background noise** — the best "person" score of frames that went through the
   detector and are not a person: not part of a run of `PERSON_HITS` consecutive
   detections (the engine's definition of a person) and not within 10 s of such a run
   (people approaching the threshold are not noise), not a still object suppressed by
   the still-object filter (it never makes an event at any threshold), and the camera's
   own human signal (ONVIF FieldDetector, when healthy) was off. A single spike above the
   threshold *is* noise: two of them in a row are what makes a false event.
2. After `CCTV_PERSON_CALIBRATION_FRAMES` (600) such frames: threshold =
   `max(start, p99(noise) + 0.10)`.
3. **Confirmed passes** — YOLO events (the weaker of the `PERSON_HITS` frames, marked
   whether the camera confirmed it) and episodes the camera saw but YOLO did not turn
   into an event (the peak YOLO score of the episode). Passes scoring no higher than
   noise + 0.05 cannot be saved by any threshold and are ignored. With at least 3 useful
   passes the threshold is capped at their 20th percentile, but not below noise + 0.05 —
   so passes can also bring it below the start threshold. The last 50 passes per camera
   and model are kept.
4. The result is clamped to 0.05–0.95 and stored in `person_threshold.json` in the
   engine state; it survives restarts and is valid only for the model it was measured on.

Manual threshold: "✏️ <camera>" in the menu, send a number (0.05–0.95) or `auto` to remove
it. It wins over auto-calibration, survives restarts (`person_threshold.override.json`
in the engine state) and is bound to the active model: after a switch it stops applying.

**Limit:** auto-calibration finds a threshold above the scene noise. Recall — how many
people the camera misses — cannot be measured without labelled people: a person the model
scores at the background level is invisible both to the detector and to the calibration.
Confirmed passes help only where the engine or the camera already noticed someone. If a
camera misses passes, lower its threshold manually and watch false events.

| Variable | Default | Meaning |
|---|---|---|
| `CCTV_PERSON_CALIBRATION_FRAMES` | 600 | background frames per calibration |
| `CCTV_PERSON_CALIBRATION_MARGIN` | 0.10 | margin over the noise percentile |
| `CCTV_PERSON_CALIBRATION_NOISE_QUANTILE` | 0.99 | noise percentile |
| `CCTV_PERSON_CALIBRATION_PASS_MIN` | 3 | useful passes needed to cap the threshold |
