#!/bin/sh
# Мс на кадр yolov5n: cv2 и onnxruntime на графах fp16/fp32/int8, каждый — отдельным процессом.
#
#   bench/backends-matrix.sh FRAMES MODELS THREADS... > latency.jsonl
#
# MODELS — каталог с yolov5n.onnx (fp16, экспорт Ultralytics), yolov5n.fp32.onnx и
# yolov5n.int8.onnx (scripts/models/convert.py fp32 / int8). THREADS — 1 2 4 …
# RUNS (30), WARMUP (3), LIMIT (20 кадров), PYTHON (python3) — из окружения.
# cv2 на int8 не замеряется: OpenCV DNN не исполняет QDQ-граф с частичным
# квантованием (int8-пулинг ждёт int8-вход).
set -eu
[ $# -ge 3 ] || { sed -n '2,9p' "$0"; exit 2; }
frames=$1 models=$2; shift 2
here=$(cd "$(dirname "$0")" && pwd)
threads=""; for t in "$@"; do threads="$threads --threads $t"; done
for spec in yolov5n.onnx:cv2 yolov5n.fp32.onnx:cv2 \
            yolov5n.onnx:onnxruntime yolov5n.fp32.onnx:onnxruntime yolov5n.int8.onnx:onnxruntime; do
  # shellcheck disable=SC2086
  "${PYTHON:-python3}" "$here/backends.py" latency "$frames" --model "$models/${spec%%:*}" \
    --backend "${spec##*:}" $threads --runs "${RUNS:-30}" --warmup "${WARMUP:-3}" --limit "${LIMIT:-20}" --json
done
