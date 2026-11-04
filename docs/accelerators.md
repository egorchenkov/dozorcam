# Detector accelerators

[Русская версия](accelerators.ru.md)

The person detector runs one ONNX model per camera. The **backend** decides what executes
that model: the CPU, or an accelerator (NPU, iGPU, GPU). This page is the overview: which
backends exist, how one is chosen, how a wrong accelerator is caught, how to build a model
for the Rockchip NPU and what it gives. Details, thresholds and the bench are in
[models.md](models.md#backends-what-runs-the-model).

## Supported backends

| `person_backend` | Hardware | Model file | Status | In the image |
|---|---|---|---|---|
| `onnxruntime` | any CPU, x86_64 and arm64 | the `.onnx` itself | **default CPU reference** | yes |
| `cv2` | any CPU (OpenCV DNN) | the `.onnx` itself | as in 0.7 and earlier, same scores on fp32 | yes |
| `rknn` | Rockchip NPU: RK3576, RK3588, RK3568/RK3566 | `<model>.<soc>.rknn` + `.rknn.json` | experimental, checked on RK3576 | arm64: runtime `librknnrt` 2.5.0 |
| `openvino` | Intel CPU, iGPU, NPU | the `.onnx` itself | checked on a CPU against the reference; iGPU/NPU detected, not yet checked on hardware | no: image variant `--target openvino` |
| `cuda` | NVIDIA GPU, Jetson (onnxruntime TensorRT/CUDA) | the `.onnx` itself | experimental, not checked on hardware | no: needs `onnxruntime-gpu` |
| `hailo` | Hailo-8 / 8L (M.2) through HailoRT | `<model>.hef` + `.hef.json` | experimental skeleton, not checked on hardware | no: needs HailoRT |

Not supported, and why:

| Hardware | Status | Reason |
|---|---|---|
| Coral EdgeTPU | not implemented | an older TFLite-only path; Hailo-8 covers the "external accelerator for any machine" case |
| AMD iGPU (Vega and similar) | not implemented | in a VM it needs passthrough of the whole iGPU (the host loses its console), ROCm does not officially support these iGPUs, and Vulkan gives little against an onnxruntime CPU that already does 27–46 ms per frame on x86 |
| AMD XDNA NPU (Ryzen AI) | reserved | the way in is the onnxruntime VitisAI execution provider, the same way as `cuda`; not done without hardware to check it on |
| Apple CoreML | not applicable | Docker on macOS does not see the Neural Engine |

## Choosing the backend

`config/config.toml`, section `[engine]` (or the `CCTV_*` environment variables):

```toml
[engine]
# person_backend = "auto"   # CCTV_PERSON_BACKEND: auto | cpu | onnxruntime | cv2 | rknn | openvino | cuda | hailo
# person_threads = 0        # CCTV_PERSON_THREADS: threads per inference, 0 = automatic
```

- `auto` (default) — look for an accelerator, check it, use it if it passes; otherwise the CPU.
- `cpu` — the CPU reference without any search: `onnxruntime`, or `cv2` if the package is
  missing.
- `onnxruntime`, `cv2` — exactly that CPU backend; an explicit CPU backend is never replaced.
- `rknn`, `openvino`, `cuda`, `hailo` — force that accelerator. It still has to pass the
  self-check; if it does not, the engine falls back to the CPU the same way as `auto`.
- OpenVINO device: `CCTV_OPENVINO_DEVICE` (default `AUTO`: NPU or iGPU if present, else CPU).
- An unknown backend name or a bad thread count is a configuration error: person detection
  stops (`person_backend_config_invalid` in the engine log) instead of guessing.

### Thread policy

`person_threads` is the number of threads of one inference on the CPU. `0` (default) means all
cores available to the engine, **but at most four**; the container CPU limit (cgroup
`cpu.max`) and CPU affinity are taken into account. Idle threads sleep instead of spinning, so
the recording ffmpeg processes are not starved. Two threads pay off everywhere; past two the
gain is small, because the cameras of one engine share the cores anyway. With `onnxruntime`
one session per model file is shared by all cameras.

## Automatic choice, self-check and fallback

With `auto` the engine decides once per model file at start and again after a model switch
from the bot:

1. **Search** without loading any runtime, in this order: `rknn` (the `rknpu` driver in sysfs
   and its device nodes in the container), `cuda` (`/dev/nvidia0` or Jetson `/dev/nvhost-*`
   and a GPU build of onnxruntime), `openvino` (an Intel `i915`/`xe` render node or an
   `intel_vpu` NPU and the `openvino` package — a plain Intel CPU stays on onnxruntime),
   `hailo` (`/dev/hailo*` and HailoRT). Nothing found is the normal case on x86, in a VM and
   on ARM without an NPU: `reason=no_accelerator`, not an error.
2. **Load** the model on the accelerator (`.rknn`/`.hef` next to the `.onnx`, the runtime).
3. **Self-check against the CPU reference.** Two built-in test frames — a person in a scene
   and the empty scene (public-domain/CC0 images shipped in the image) — run through the
   accelerator and through `onnxruntime` on the CPU. The accelerator is used only if the
   person score differs by **at most 0.15** on both frames and the person box overlaps the
   CPU box with **IoU ≥ 0.5**. Honest INT8 rounding passes; a broken graph, a wrong passport,
   a wrong input layout or NaN does not.
4. **Anything else falls back to `onnxruntime` on the CPU silently.** The engine never stops
   detecting because of an accelerator.

The decision is one line in the engine log and a line in `/model` of the bot:

```
person_backend_selected file=yolov5n.onnx requested=auto backend=rknn reason=selfcheck_ok accelerator=rk3576 iou=0.97 score=0.83 cpu_score=0.85 ms=55 cpu_ms=290 delta=0.02
person_backend_selected file=yolox_tiny.onnx requested=auto backend=onnxruntime threads=4(auto) reason=no_accelerator
```

`/model` says, for example, "Accelerator: NPU rk3576 (rknn), self-check passed — 55 ms per
frame (CPU 290 ms)", "No accelerator, CPU (onnxruntime)", or why an accelerator that exists
is not used (device not passed into the container, no `.rknn` next to the model, runtime
missing, self-check mismatch). The full list of `reason` values is in
[models.md](models.md#automatic-choice-and-self-check).

**Thresholds follow the backend that actually runs.** A per-camera threshold found or set on
the CPU does not apply on `rknn` and vice versa (`cv2` and `onnxruntime` share one — same
scores). When the backend of a calibrated camera changes, it recalibrates by itself and keeps
its previous automatic threshold until then; a manual threshold is not carried over.

### Devices in the container

The `dozorcam` wrapper finds the accelerator nodes on the host and passes them with their
groups to the engine only where they exist; without them `compose.yml` maps harmless
placeholders and the engine starts as before.

| Accelerator | Nodes | Set by hand in `.env` (plain `docker compose`) |
|---|---|---|
| Rockchip NPU | `/dev/dri/by-path/*npu-card` + `*npu-render`, or `/dev/rknpu` | `CCTV_NPU_CARD`, `CCTV_NPU_RENDER`, `CCTV_NPU_CARD_GID`, `CCTV_NPU_RENDER_GID` |
| Intel iGPU | `/dev/dri` with an `i915`/`xe` render node, no passthrough | `CCTV_INTEL_DRI`, `CCTV_INTEL_RENDER_GID` |

Group ids: `stat -c %g <node>`.

## Converting a model for the Rockchip NPU

The NPU runs an INT8 `.rknn` file built from a YOLOv5 fp32 ONNX graph. One command on an
x86_64 machine with Docker builds it for all four SoCs:

```sh
scripts/models/rknn-convert.sh yolov5n.fp32.onnx FRAMES OUT_DIR [SOC...]
```

- `FRAMES` — a few hundred jpg/png frames from **your own cameras** (day and night, with and
  without people): the INT8 scales are taken from them. Keep them out of the repository.
- `OUT_DIR` gets `<name>.<soc>.rknn` and its passport `<name>.<soc>.rknn.json`; put both next
  to the `.onnx` in `/etc/cctv/models`. A precision variant such as `yolov5n.fp32.onnx`
  without its own `.rknn` uses the base model's `yolov5n.<soc>.rknn`.
- The toolkit is pinned (`rknn-toolkit2` 2.5.0 in `scripts/models/rknn-toolkit.Dockerfile`)
  and matches `librknnrt` 2.5.0 in the arm64 image. It runs only on x86_64. One run for four
  SoCs takes about 5.5 minutes on 2 vCPU.
- To convert an fp16 export to fp32 first:
  `python3 scripts/models/convert.py fp32 yolov5n.onnx yolov5n.fp32.onnx`.
- Before relying on it, compare it with the CPU on your own frames:
  `bench/backends.py compare --candidate yolov5n.<soc>.rknn:rknn …`.

## Sizing: CPU and NPU

YOLOv5n 640, median milliseconds per frame (full detection: resize, inference, decoding),
fp32 graph, `onnxruntime` unless stated:

| Machine | 1 thread | 2 threads | 4 threads |
|---|---|---|---|
| x86 AMD Zen 3, VM with 2 vCPU | 46 (`cv2`: 79) | 27 (`cv2`: 43) | — |
| arm64 Neoverse-N1 (cloud), 4 cores | 197 (`cv2`: 266) | 133 | 102 |
| arm64 Cortex-A72 (RK3576, 4 big cores) | 687 (`cv2`: 962) | 367 | 269 |
| **RK3576 NPU (`rknn`, INT8)** | **50** (p90 52–54) | | |

- On an RK3576 board the NPU is about 5× faster than its four Cortex-A72 cores and frees them
  for recording. INT8 lowers scores by 0.023 on average (p95 of the difference 0.10); at the
  threshold 0.35, 82 of 85 people are still found, none added. INT8 on the CPU is not used:
  it loses 13 of the same 85.
- fp32 is the default graph for the CPU on every architecture: scores match `cv2` to 1e-4 and
  it is the fastest exact variant on x86.
- On x86 and a cloud ARM the CPU is enough for typical camera counts; per-camera numbers for
  whole machines are in [sizing.md](sizing.md).
- Numbers come from 115 frames with people (day, dusk, night, infrared) and
  `bench/backends-matrix.sh`; the full table with fp16/int8 graphs is in
  [models.md](models.md#graph-precision-fp32-for-the-cpu).
