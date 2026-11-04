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

## Backends: what runs the model

The family decides how the model output is read; the backend decides what executes the graph.
The CPU backends give the same fields (box, confidence) and use the same thresholds, so
calibrated per-camera thresholds stay valid when the backend changes between them.

```toml
[engine]
# person_backend = "auto"       # CCTV_PERSON_BACKEND: auto | cpu | onnxruntime | cv2 | rknn
# person_threads = 0            # CCTV_PERSON_THREADS: onnxruntime threads per inference
```

- `auto` (default) — an accelerator if the machine has one and it passes the self-check,
  otherwise `onnxruntime` on the CPU ([below](#automatic-choice-and-self-check)).
- `cpu` — the CPU reference without looking for an accelerator: `onnxruntime`, or `cv2`
  if the `onnxruntime` package is missing.
- `onnxruntime` — onnxruntime on CPU, the same `.onnx` file. It is noticeably
  faster on the same graph (about −40 % per frame on x86). One session per model file is
  shared by all cameras of the engine. An explicitly chosen CPU backend is never replaced.
- `cv2` — OpenCV DNN on CPU, one network per camera, as in 0.7 and earlier. Set it to get
  exactly the earlier behaviour.

How the default was checked: 115 frames with people from real cameras (day, dusk, night,
infrared), the best person score and box of each frame on `cv2` and on `onnxruntime`,
arm64:

| Model graph | Max score difference | Box IoU (median / min) | Decisions changed at the family threshold |
|---|---|---|---|
| `yolox_tiny.onnx` (fp32, in the image) | 0.0000 | 1.000 / 1.000 | 0 of 115 at 0.30 |
| `yolov8n.onnx` (fp32) | 0.0000 | 1.000 / 1.000 | 0 of 115 at 0.30 |
| `yolov5n.onnx` exported as fp32 | 0.0001 | 1.000 / 1.000 | 0 of 115 at 0.35 |
| `yolov5n.onnx` exported as fp16 | 0.0185 (p95 0.013) | 0.994 / 0.969 | 0 of 115 at 0.35 |

An fp32 graph gives the same scores on both backends. An fp16 graph is computed in fp16
by onnxruntime on arm64, while `cv2` converts it to fp32: scores drift by up to about 0.02,
below the 0.05 margin of the per-camera threshold calibration. On a camera whose threshold
sits right at a frame's score such a frame can flip (2–3 of these 115 frames at thresholds
0.45–0.60). If that matters, use an fp32 export or `person_backend = "cv2"`.
- `person_threads` — threads of one inference. `0` (default) means all cores available to
  the engine, but at most four: the container CPU limit (cgroup `cpu.max`) and CPU affinity
  are taken into account. Idle threads sleep instead of spinning, so recording ffmpeg is
  not starved.
- An unknown backend or a bad thread count stops person detection
  (`person_backend_config_invalid` in the engine log) instead of silently falling back.
  The start log line shows the backend: `person_model … backend=onnxruntime threads=4(auto)`.

### Automatic choice and self-check

With `person_backend = "auto"` the engine decides per model file, once per start and again
after a model switch from the bot:

1. **Look for accelerators.** Now: a Rockchip NPU (RK3576, RK3588, RK3568/RK3566) — the
   `rknpu` driver in sysfs, its device nodes in the container (`/dev/dri/card*` +
   `renderD*`, or `/dev/rknpu`) and the SoC name. Nothing found — `onnxruntime` on the CPU,
   reason `no_accelerator`; this is the normal case on x86 (also in a VM without devices)
   and on ARM without an NPU, not an error.
2. **Load the model on it**: `<model>.<soc>.rknn` next to the `.onnx` file and the
   `librknnrt` runtime (in the arm64 image).
3. **Self-check against the CPU reference.** Two built-in test frames (a person pasted into
   a scene and the scene alone; public-domain/CC0 images in `cctv/engine/selfcheck/`) go
   through the accelerator and through `onnxruntime` on the CPU. The accelerator is used only
   if the person score differs by at most 0.15 on both frames and the box of the person the
   CPU finds overlaps (IoU ≥ 0.5). Honest INT8 rounding passes (−0.02 on average, see below);
   a broken graph, a wrong passport or a wrong input does not.
4. **Anything else falls back to the CPU silently**: the engine keeps detecting on
   `onnxruntime`, never stops because of an accelerator.

The result is one engine log line and a line in `/model` of the bot:

```
person_backend_selected file=yolov5n.onnx requested=auto backend=rknn reason=selfcheck_ok accelerator=rk3576 iou=0.97 score=0.83 cpu_score=0.85 ms=55 cpu_ms=290 delta=0.02
person_backend_selected file=yolox_tiny.onnx requested=auto backend=onnxruntime threads=4(auto) reason=no_accelerator
```

| `reason` | Meaning | `/model` |
|---|---|---|
| `selfcheck_ok` | accelerator passed the self-check and is used | "Accelerator: NPU rk3576 (rknn), self-check passed — 55 ms per frame (CPU 290 ms)" |
| `no_accelerator` | none on this machine | "No accelerator, CPU (onnxruntime)" |
| `rknn_device_missing` | the board has an NPU, but its device nodes are not in the container | "Accelerator rk3576: not used — the NPU device is not passed into the container; …" |
| `person_rknn_model_missing` | no `<model>.<soc>.rknn` (+ `.rknn.json`) next to the model | … |
| `person_backend_runtime_missing` | `librknnrt` cannot be loaded | … |
| `person_rknn_runtime_failed` | the runtime refused the `.rknn` file (damaged, other SoC) | … |
| `rknn_inference_failed` | the test run on the NPU failed | … |
| `person_selfcheck_*` | the test run does not match the CPU (score, box, NaN) | … |

- `person_backend = "rknn"` forces the NPU: no other accelerator is tried, but the same
  self-check runs and a failure falls back to the CPU the same way (`rknn_not_found` on a
  machine without one). `cpu`, `onnxruntime` and `cv2` skip the search and the check.
- **Thresholds.** The per-camera threshold belongs to the backend that actually runs
  (`…@rknn` on the NPU): when `auto` switches a calibrated camera between the NPU and the
  CPU, it recalibrates by itself (below).
- **Compose.** The `dozorcam` wrapper finds the NPU nodes on the host
  (`/dev/dri/by-path/*npu-card`, `*npu-render`, or `/dev/rknpu`) and their groups, and
  passes them to the engine; without an NPU `compose.yml` maps harmless placeholders and the
  engine starts as before. With plain `docker compose`, set `CCTV_NPU_CARD`,
  `CCTV_NPU_RENDER` and their group ids `CCTV_NPU_CARD_GID`, `CCTV_NPU_RENDER_GID`
  (`stat -c %g <node>`) in `.env`.

### Graph precision: fp32 for the CPU

The same YOLOv5n can be exported in fp16 (the Ultralytics default), fp32 or int8.
Median milliseconds per frame (full `detect()`: resize, inference, decoding), YOLOv5n 640,
each variant in its own process, the lower of two runs (`bench/backends-matrix.sh`):

| Machine | Threads | `cv2` fp16 | `cv2` fp32 | `onnxruntime` fp16 | `onnxruntime` fp32 | `onnxruntime` int8 |
|---|---|---|---|---|---|---|
| x86 AMD Zen 3, VM with 2 vCPU | 1 | 78 | 79 | 54 | **46** | 45 |
| | 2 | 42 | 43 | 35 | **27** | 31 |
| arm64 Neoverse-N1 (cloud), 4 cores | 1 | 267 | 266 | 172 | **197** | 110 |
| | 2 | 147 | 143 | 107 | **133** | 70 |
| | 4 | 127 | 107 | 93 | **102** | 59 |
| arm64 Cortex-A72 (RK3576, 4 big cores, isolated container) | 1 | 958 | 962 | 718 | **687** | 453 |
| | 2 | 457 | 446 | 368 | **367** | 291 |
| | 4 | 265 | 266 | 271 | **269** | 226 |

`cv2` cannot run the int8 graph (OpenCV DNN does not execute a partially quantized QDQ
graph). The Cortex-A72 numbers are the mean of two runs (they agree within 6 %), with the
engine of that node idle. The x86 runs agree within 3 %. The Neoverse-N1 host had other work running (load
5–6 on 4 cores): one-thread numbers repeat within ~10 %, with 2–4 threads they vary by up
to 25 %.

- **Use an fp32 graph: the default for the CPU on every architecture.** Its scores match
  `cv2` to 1e-4, so thresholds calibrated on either backend stay valid. On x86 it is also
  the fastest exact variant (−15…−23 % against fp16). On Cortex-A72 (ARMv8.0, no fp16
  arithmetic) fp16 gives nothing: 718/368/271 against 687/367/269 ms. Only on Neoverse-N1
  (ARMv8.2) onnxruntime computes an fp16 graph in fp16 and is faster on one thread
  (172–179 against 197–222 ms), at the price of the score drift described above — not
  worth a separate graph per architecture. When onnxruntime opens an fp16 graph, the
  engine log says so once with the conversion command (`person_model_precision … precision=fp16`).
  `yolox_tiny.onnx` in the image is already fp32. To convert an fp16 YOLOv5 export:
  `python3 scripts/models/convert.py fp32 yolov5n.onnx yolov5n.fp32.onnx` (needs the `onnx`
  package; the result is checked against the source).
- **int8 on the CPU is not used.** It is about 1.8× faster on arm64 (no gain on x86), but
  on the same 115 frames scores drop by 0.04 on average and by up to 0.34, and 13 of 85
  people found at 0.35 are lost (`scripts/models/convert.py int8`, head convolutions kept
  in fp32). Thresholds cannot fix that: the error differs from frame to frame.
- Two threads per inference pay off everywhere; past two the gain is small, because the
  cameras of an engine share the cores anyway (see `person_threads`).

### Converting for the Rockchip NPU

A YOLOv5 fp32 graph is converted to INT8 `.rknn` files for RK3576, RK3588, RK3568 and
RK3566 in one command on an x86_64 machine with Docker:

```sh
scripts/models/rknn-convert.sh yolov5n.fp32.onnx FRAMES OUT_DIR [SOC...]
```

- `FRAMES` is a directory of a few hundred jpg/png frames from your own cameras (day and
  night, with and without people); the INT8 scales are taken from them. Keep these frames
  out of the repository. `LIMIT` (default 200) frames are taken evenly over the sorted
  list, so a rerun on the same directory uses the same frames.
- The result in `OUT_DIR`: `<name>.<soc>.rknn` and its passport `<name>.<soc>.rknn.json`
  (head outputs, anchors, input layout, number of calibration frames, sha256 of the source
  graph).
- The network is cut before the YOLOv5 `Detect` layer; the engine decodes the three head
  outputs itself. Before the build the script checks that this decoding gives the same
  person scores and boxes as the full graph and stops otherwise.
- Versions are pinned in `scripts/models/rknn-toolkit.Dockerfile`: `rknn-toolkit2` 2.5.0
  on Python 3.12 (Debian bookworm), `torch` 2.2.0 CPU. The `librknnrt` runtime on the device
  must be the same version, 2.5.0. The toolkit runs only on x86_64: the aarch64 wheel does
  not build models without an NPU.
- On a 2-vCPU x86 VM one run for all four SoCs takes about 5.5 minutes. Two runs on the
  same graph and frames give files of the same size and identical passports; the files are
  not byte-identical.
- `ALGORITHM=mmse` or `kl_divergence` selects another INT8 scale search (default `normal`).

### Rockchip NPU (`rknn`, experimental)

`rknn` runs YOLOv5 on the NPU of Rockchip RK3576, RK3588 and RK3568/RK3566 through the
`librknnrt` C API. `auto` picks it when the NPU, the runtime and a `.rknn` file for the
model are present and the self-check passes ([above](#automatic-choice-and-self-check)).

- The model file is `<name>.<soc>.rknn` next to the ONNX file (`yolov5n.rk3576.rknn` for
  `yolov5n.onnx`, SoC from the device tree) with its passport `.rknn.json`, both built by
  `scripts/models/rknn-convert.sh` (above). A precision variant such as
  `yolov5n.fp32.onnx` without its own `.rknn` uses the base model's `yolov5n.rk3576.rknn`.
- The engine decodes the three head outputs itself and only the rows it reads (box,
  objectness, person).
- The container needs both NPU device nodes (`/dev/dri/by-path/platform-*.npu-card` and
  `…npu-render`, or `/dev/rknpu` on older kernels) and their groups (`video` and `render`
  on RK3576) — the `dozorcam` wrapper passes them. The library is `/usr/lib/librknnrt.so`
  in the arm64 image (2.5.0, like the toolkit; RKNN SDK License, `THIRD_PARTY.md`) or
  `CCTV_RKNN_LIB`.
- Speed on RK3576: 50 ms per frame (p90 52–54 ms) against 265 ms for the best CPU variant
  on its four Cortex-A72 cores and 958 ms for `cv2` on one thread.
- INT8 lowers scores a little and unevenly. On the same 115 frames with people (reference:
  fp32 on the CPU) the median goes 0.480 → 0.459, the mean shift is −0.023 (−0.03 in the
  0.30–0.60 range where thresholds live), p95 of the difference 0.10, maximum 0.17;
  scores drop on 83 of 115 frames. Box IoU median 0.94. At 0.35, 82 of 85 people are
  still found (3 lost, none added); compare int8 on the CPU above: 13 lost.
- **No fixed threshold correction and no hybrid mode.** Lowering the threshold by the mean
  shift does not reduce the mismatches: over thresholds 0.25–0.60 a −0.02 shift trades
  people lost for frames added (37 lost / 12 added → 29 / 21) — the per-frame spread (p95
  0.10) is larger than the shift, and it is the spread a threshold cannot fix. A CPU check
  of near-threshold frames would cost what the NPU saves. Instead the threshold is per
  backend: a per-camera threshold found or set on the CPU does not apply on `rknn` and
  vice versa (`cv2` and `onnxruntime` share it — same scores). After a change of backend a
  camera that had a calibrated threshold recalibrates by itself on the new scores
  (`person_threshold_calibration … reason=backend` in the log), like after a model switch;
  until it finishes the camera keeps its previous automatic threshold (the scores of the
  two backends differ by about 0.02, much closer than the start threshold of the family).
  A manual threshold is not carried over.
- Before using it, still compare it with the CPU on your own frames
  (`bench/backends.py compare --candidate yolov5n.rk3576.rknn:rknn …`).

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
   engine state; it survives restarts and is valid only for the model it was measured on
   (and, for the INT8 NPU backend `rknn`, only on that backend — see above).

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
