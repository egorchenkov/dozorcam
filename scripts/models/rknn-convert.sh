#!/bin/sh
# ONNX → .rknn INT8 под SoC Rockchip в закреплённом контейнере rknn-toolkit2 (x86_64).
#
#   scripts/models/rknn-convert.sh MODEL.fp32.onnx CALIB_DIR OUT_DIR [SOC...]
#
# SOC по умолчанию — rk3576 rk3588 rk3568 rk3566. CALIB_DIR — кадры своих камер
# (jpg/png), в репозиторий их не класть. В OUT_DIR — <имя>.<soc>.rknn и паспорт
# .rknn.json; LIMIT (200) — сколько кадров калибровки, ALGORITHM (normal | mmse |
# kl_divergence) — подбор шкал INT8, IMAGE — тег образа toolkit.
set -eu
[ $# -ge 3 ] || { sed -n '2,10p' "$0"; exit 2; }
model=$(realpath "$1") calib=$(realpath "$2"); mkdir -p "$3"; out=$(realpath "$3"); shift 3
[ $# -gt 0 ] || set -- rk3576 rk3588 rk3568 rk3566
here=$(cd "$(dirname "$0")" && pwd) root=$(cd "$(dirname "$0")/../.." && pwd)
image=${IMAGE:-dozorcam-rknn-toolkit:2.5.0}
docker image inspect "$image" >/dev/null 2>&1 \
  || docker build --platform linux/amd64 -t "$image" -f "$here/rknn-toolkit.Dockerfile" "$here"
socs=""; for s in "$@"; do socs="$socs --soc $s"; done
# shellcheck disable=SC2086
docker run --rm --platform linux/amd64 --network none --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$root:/src:ro" -v "$model:/in/$(basename "$model"):ro" -v "$calib:/calib:ro" -v "$out:/out" \
  "$image" python scripts/models/convert.py rknn "/in/$(basename "$model")" --calib /calib $socs \
  --out-dir /out --limit "${LIMIT:-200}" --algorithm "${ALGORITHM:-normal}"
