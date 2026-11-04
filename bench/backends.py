#!/usr/bin/env python3
"""Бэкенды детектора людей: мс на кадр и сверка уверенности на одном наборе кадров.

    backends.py latency --model yolov5n.onnx --backend onnxruntime --threads 1 --threads 2 FRAMES
    backends.py compare --model yolov5n.onnx --backend cv2 \\
                        --candidate yolov5n.fp32.onnx:onnxruntime --candidate yolov5n.rk3576.rknn:rknn FRAMES

Кадры — каталог jpg/png (кадры камер установки в репозиторий не класть). Детектор —
настоящий адаптер семейства (``PersonDetector`` и др.) на выбранном бэкенде, поэтому
замер включает препроцессинг и разбор выхода, как в движке.

* ``latency`` — медиана и p90 полного ``detect()`` в один поток вызова, по каждому
  значению ``--threads`` (потоки onnxruntime; у cv2 — ``cv2.setNumThreads``);
* ``compare`` — уверенность person и рамка эталона и кандидатов на каждом кадре:
  расхождение score (медиана, p95, максимум), совпадение решения «человек есть» при
  пороге ``--confidence``, IoU лучших рамок. ``--csv`` — построчно.

Итог — JSON в stdout (``--json``) или таблица.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import platform
import statistics
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from cctv.engine import person_backends, person_models  # noqa: E402

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def frames(directory: pathlib.Path, limit: int = 0) -> list[pathlib.Path]:
    files = sorted(p for p in directory.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    if limit and len(files) > limit:
        files = [files[round(i * (len(files) - 1) / (limit - 1))] for i in range(limit)]
    return files


def detector(model: pathlib.Path, backend: str, threads: int, family: str):
    settings = person_backends.BackendSettings(backend, threads)
    if backend == "cv2" and threads:
        cv2.setNumThreads(threads)
    factory = {"yolov5": person_models.person_detector.PersonDetector,
               "yolox": person_models.person_detector_yolox.YoloxPersonDetector,
               "yolov8": person_models.person_detector_yolov8.Yolov8PersonDetector}[family]
    return factory(model, person_models.FAMILIES[family].confidence, backend=settings)


def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w, h = min(ax2, bx2) - max(a[0], b[0]), min(ay2, by2) - max(a[1], b[1])
    inter = max(0.0, w) * max(0.0, h)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values), q)) if values else float("nan")


def latency(args) -> dict:
    images = [cv2.imread(str(p)) for p in frames(args.frames, args.limit)]
    results = []
    for threads in args.threads or [0]:
        det = detector(args.model, args.backend, threads, args.family)
        for image in images[:args.warmup]:
            det.detect(image)
        times = []
        for index in range(args.runs):
            image = images[index % len(images)]
            start = time.perf_counter()
            det.detect(image)
            times.append((time.perf_counter() - start) * 1000)
        results.append({"threads": threads or person_backends.default_threads(), "runs": len(times),
                        "median_ms": round(statistics.median(times), 1), "p90_ms": round(percentile(times, 90), 1)})
        del det
    return {"mode": "latency", "model": args.model.name, "backend": args.backend, "family": args.family,
            "machine": platform.machine(), "cpus": person_backends.available_cpus(), "results": results}


def compare(args) -> dict:
    files = frames(args.frames, args.limit)
    reference = detector(args.model, args.backend, args.threads[0] if args.threads else 0, args.family)
    candidates = []
    for spec in args.candidate:
        path, _, backend = spec.rpartition(":")
        candidates.append((spec, detector(pathlib.Path(path), backend, 0, args.family)))
    rows = []
    for path in files:
        image = cv2.imread(str(path))
        score, box = reference.best(image)
        row = {"file": str(path.relative_to(args.frames)), "reference": round(score, 4)}
        for spec, det in candidates:
            got, got_box = det.best(image)
            row[spec] = round(got, 4)
            row[spec + ":iou"] = round(iou(box, got_box), 3)
        rows.append(row)
    summary = {}
    threshold = args.confidence
    ref_scores = [r["reference"] for r in rows]
    summary["reference"] = {"median": round(statistics.median(ref_scores), 4),
                            "found": sum(s >= threshold for s in ref_scores)}
    for spec, _ in candidates:
        got = [r[spec] for r in rows]
        diff = [g - r for g, r in zip(got, ref_scores)]
        absdiff = [abs(d) for d in diff]
        ious = [r[spec + ":iou"] for r in rows if r["reference"] >= threshold]
        summary[spec] = {
            "median": round(statistics.median(got), 4), "found": sum(s >= threshold for s in got),
            "mean_diff": round(statistics.fmean(diff), 4), "median_abs_diff": round(statistics.median(absdiff), 4),
            "p95_abs_diff": round(percentile(absdiff, 95), 4), "max_abs_diff": round(max(absdiff), 4),
            "decision_mismatch": sum((g >= threshold) != (r >= threshold) for g, r in zip(got, ref_scores)),
            "lost": sum(r >= threshold > g for g, r in zip(got, ref_scores)),
            "gained": sum(g >= threshold > r for g, r in zip(got, ref_scores)),
            "box_iou_median": round(statistics.median(ious), 3) if ious else None,
            "box_iou_below_0_5": sum(i < 0.5 for i in ious),
        }
    if args.csv:
        with args.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return {"mode": "compare", "frames": len(rows), "threshold": threshold,
            "reference": f"{args.model.name}:{args.backend}", "summary": summary}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("latency", "compare"))
    parser.add_argument("frames", type=pathlib.Path)
    parser.add_argument("--model", type=pathlib.Path, required=True)
    parser.add_argument("--backend", default="onnxruntime", choices=person_backends.BACKENDS)
    parser.add_argument("--family", default="yolov5", choices=tuple(person_models.FAMILIES))
    parser.add_argument("--threads", type=int, action="append")
    parser.add_argument("--candidate", action="append", default=[], help="файл:бэкенд")
    parser.add_argument("--confidence", type=float, default=0.35)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--csv", type=pathlib.Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = latency(args) if args.mode == "latency" else compare(args)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
