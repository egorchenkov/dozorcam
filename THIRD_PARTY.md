# Third-party software

Dozorcam's own code is licensed under [Apache-2.0](LICENSE). The repository contains no
third-party code or model weights. The container image (`ghcr.io/egorchenkov/dozorcam`)
bundles the components below; each keeps its own license. Versions are pinned in
`Dockerfile` and `requirements.txt`.

## FFmpeg (static build by mwader) — GPL-3.0-or-later

- Image: `mwader/static-ffmpeg:7.1.1` (digest pinned in `Dockerfile`), files
  `/usr/local/bin/ffmpeg` and `/usr/local/bin/ffprobe`.
- FFmpeg 7.1.1 configured with `--enable-gpl --enable-version3` and GPL libraries
  (x264, x265, xvid and others), so these binaries are distributed under the
  **GNU General Public License v3.0 or later**. Run `ffmpeg -L` and `ffmpeg -buildconf`
  inside the image for the exact license text and configuration.
- Corresponding source: FFmpeg — <https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz>;
  build recipe and the exact versions of every library — <https://github.com/wader/static-ffmpeg>
  (tag `7.1.1`, `Dockerfile`; the recipe itself is MIT-licensed).
- Dozorcam runs `ffmpeg`/`ffprobe` as separate processes and does not link to them.
  If you need the source of the exact build and cannot get it from the links above,
  open an issue in this repository.

## Person detector model: YOLOX-Tiny — Apache-2.0

- `/usr/share/cctv/models/yolox_tiny.onnx`, the default model.
- Megvii YOLOX release `0.1.1rc0`:
  <https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0>,
  checksum pinned in `Dockerfile`. Copyright (c) 2021-2022 Megvii Inc.
  License: Apache-2.0 — <https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE>.

**Not bundled:** YOLOv5 and YOLOv8/YOLO11 weights by Ultralytics are AGPL-3.0. Neither the
repository nor the image contains or downloads them. If you put such a file into
`config/models/` yourself, its license applies to your installation; see
[docs/models.md](docs/models.md#where-to-put-your-own-weights).

## Rockchip NPU runtime: librknnrt 2.5.0 — RKNN SDK License (arm64 image only)

- `/usr/lib/librknnrt.so` in the **arm64** image; the amd64 image does not contain it.
  It runs the `rknn` person detector backend on Rockchip RK3576/RK3588/RK3568/RK3566 NPUs
  and is loaded only when such an NPU is found ([docs/models.md](docs/models.md)).
- From `airockchip/rknn-toolkit2` `v2.5.0`, `rknpu2/runtime/Linux/librknn_api/aarch64/`:
  <https://github.com/airockchip/rknn-toolkit2/tree/v2.5.0/rknpu2/runtime>, checksum pinned in
  `Dockerfile`. Copyright (c) Rockchip Electronics Co., Ltd. Proprietary binary, used
  unmodified, for applications compatible with Rockchip products. License: RKNN SDK
  License — <https://github.com/airockchip/rknn-toolkit2/blob/v2.5.0/LICENSE>, its text is
  in the image at `/usr/share/doc/dozorcam/RKNN-SDK-LICENSE`.

## Intel OpenVINO runtime 2026.4.1 — Apache-2.0 (`-openvino` image variant only)

- Python package `openvino` (`requirements-openvino.txt`), added only by the
  `--target openvino` build (tags ending in `-openvino`); the default image does not
  contain it. It runs the `openvino` person detector backend (Intel CPU/iGPU/NPU).
- Copyright (C) Intel Corporation. License: Apache-2.0; texts of the components it bundles —
  `openvino-2026.4.1.dist-info/licenses/` under `/usr/local/lib/python3.12/site-packages`
  in that image.

## Python packages (`requirements.txt` and their dependencies)

| Package | Version | License |
|---|---|---|
| python-telegram-bot | 22.7 | LGPL-3.0-only — used unmodified as a separate package |
| opencv-python-headless | 5.0.0.93 | Apache-2.0; the wheel bundles FFmpeg libraries (LGPL-2.1+), libvpx, libaom, OpenBLAS and others — see `cv2/LICENSE-3RD-PARTY.txt` inside the image |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| anyio | 4.15.1 | MIT |
| h11 | 0.16.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| typing_extensions | 4.16.0 | PSF-2.0 |

Each package's license text is in its `*.dist-info` directory under
`/usr/local/lib/python3.12/site-packages` in the image.

## Base image

`python:3.12-slim` (Debian; digest pinned in `Dockerfile`): CPython under the PSF License,
Debian packages under their own licenses — see `/usr/share/doc/*/copyright` in the image.
