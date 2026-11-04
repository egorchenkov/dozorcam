#!/usr/bin/env python3
"""Варианты графа детектора людей для бэкендов движка (docs/models.md, «Backends»).

    convert.py fp32  yolov5n.onnx yolov5n.fp32.onnx
    convert.py int8  yolov5n.fp32.onnx yolov5n.int8.onnx --calib DIR [--limit 200]
    convert.py rknn  yolov5n.fp32.onnx --calib DIR --soc rk3576 [--soc rk3588 ...] [--out-dir D]
    convert.py check yolov5n.fp32.onnx              # голова и якоря без конвертации

* ``fp32`` — экспорт YOLOv5n из комплекта Ultralytics хранится в fp16; CPU-бэкенды
  всё равно считают в fp32 и на каждом кадре приводят веса и активации. Здесь веса,
  константы, вход и выход переводятся в fp32 один раз; выход сверяется с исходным.
* ``int8`` — статическое квантование onnxruntime (QDQ, веса по каналам) только для
  свёрток: декодер Detect в хвосте графа остаётся fp32. Калибровка — кадры камер
  (``--calib``, jpg/png), препроцессинг тот же, что у детектора: растяжение до входа
  без letterbox, RGB, 0..1.
* ``rknn`` — RKNN INT8 под SoC Rockchip (нужен ``rknn-toolkit2``): граф режется
  перед слоем Detect (три свёртки головы), декодер исполняет движок
  (``person_backends.decode_yolov5``) — общий выход «координаты 0..640 + вероятности
  0..1» в одной шкале INT8 потерял бы вероятность. Рядом с ``<имя>.<soc>.rknn``
  пишется паспорт ``.rknn.json``: страйды, якоря (сняты с графа численно), множитель
  входа, sha256 исходника. Калибровочные кадры уходят в toolkit готовыми входами
  (.npy uint8, раскладка NCHW графа) — ровно те значения, что движок подаёт на NPU.

Поддерживается семейство yolov5 (вход 640, выход ``(1, N, 5 + классы)``); кадры
калибровки — с камер установки, в репозиторий их не класть.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import tempfile

import cv2
import numpy as np

INPUT_SIZE = 640
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def blob(path: pathlib.Path, size: int = INPUT_SIZE) -> np.ndarray:
    """Вход сети как у PersonDetector: растяжение, RGB, 0..1, NCHW float32."""
    frame = cv2.imread(str(path))
    if frame is None:
        raise SystemExit(f"не читается кадр: {path}")
    return cv2.dnn.blobFromImage(frame, 1 / 255.0, (size, size), swapRB=True)


def frames(directory: pathlib.Path, limit: int) -> list[pathlib.Path]:
    files = sorted(p for p in directory.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        raise SystemExit(f"нет кадров в {directory}")
    if limit and len(files) > limit:  # равномерная подвыборка: все камеры и время суток
        files = [files[round(i * (len(files) - 1) / (limit - 1))] for i in range(limit)]
    return files


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def session(path: pathlib.Path | bytes):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    return ort.InferenceSession(path if isinstance(path, bytes) else str(path), options,
                                providers=["CPUExecutionProvider"])


def run(sess, data: np.ndarray) -> list[np.ndarray]:
    source = sess.get_inputs()[0]
    dtype = np.float16 if source.type == "tensor(float16)" else np.float32
    return [o.astype(np.float32) for o in sess.run(None, {source.name: data.astype(dtype)})]


def scores_close(reference: np.ndarray, got: np.ndarray) -> tuple[float, float]:
    """Расхождение (уверенность person, координаты в пикселях входа) двух выходов (1, N, 85)."""
    person = np.abs(reference[0, :, 4] * reference[0, :, 5] - got[0, :, 4] * got[0, :, 5]).max()
    return float(person), float(np.abs(reference[0, :, :4] - got[0, :, :4]).max())


# --- fp16 → fp32 -----------------------------------------------------------

def to_fp32(source: pathlib.Path, target: pathlib.Path) -> None:
    import onnx
    from onnx import TensorProto, numpy_helper

    model = onnx.load(str(source))
    graph = model.graph
    for index, tensor in enumerate(graph.initializer):
        if tensor.data_type == TensorProto.FLOAT16:
            graph.initializer[index].CopyFrom(numpy_helper.from_array(
                numpy_helper.to_array(tensor).astype(np.float32), tensor.name))
    for node in graph.node:
        for attr in node.attribute:
            if node.op_type == "Constant" and attr.name == "value" and attr.t.data_type == TensorProto.FLOAT16:
                attr.t.CopyFrom(numpy_helper.from_array(numpy_helper.to_array(attr.t).astype(np.float32), attr.t.name))
            if node.op_type == "Cast" and attr.name == "to" and attr.i == TensorProto.FLOAT16:
                attr.i = TensorProto.FLOAT
    for value in (*graph.input, *graph.output, *graph.value_info):
        if value.type.tensor_type.elem_type == TensorProto.FLOAT16:
            value.type.tensor_type.elem_type = TensorProto.FLOAT
    onnx.checker.check_model(model)
    onnx.save(model, str(target))
    data = np.random.default_rng(0).random((1, 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
    person, coords = scores_close(run(session(source), data)[0], run(session(target), data)[0])
    print(f"fp32: {target} — расхождение с исходником: person {person:.4f}, координаты {coords:.2f} px")


# --- fp32 → int8 (onnxruntime QDQ) ------------------------------------------

def to_int8(source: pathlib.Path, target: pathlib.Path, calib: pathlib.Path, limit: int,
            method: str = "minmax", keep: tuple[str, ...] = ("head",)) -> None:
    import onnx
    from onnxruntime.quantization import (CalibrationDataReader, CalibrationMethod, QuantFormat, QuantType,
                                          quantize_static)
    from onnxruntime.quantization.shape_inference import quant_pre_process

    files = frames(calib, limit)

    class Reader(CalibrationDataReader):
        def __init__(self) -> None:
            self.items = iter(files)
            self.name = session(source).get_inputs()[0].name

        def get_next(self):
            path = next(self.items, None)
            return None if path is None else {self.name: blob(path)}

    model = onnx.load(str(source))
    convs = [node for node in model.graph.node if node.op_type == "Conv"]
    exclude = []
    if "head" in keep:  # свёртки головы: в одном тензоре рамки и логиты objectness/классов
        heads = set(head_outputs(model))
        exclude += [node.name for node in convs if node.output[0] in heads]
    if "first" in keep:  # первая свёртка видит сырой кадр
        exclude.append(convs[0].name)
    methods = {"minmax": CalibrationMethod.MinMax, "percentile": CalibrationMethod.Percentile,
               "entropy": CalibrationMethod.Entropy}
    with tempfile.TemporaryDirectory() as tmp:
        prepared = pathlib.Path(tmp) / "prepared.onnx"
        quant_pre_process(str(source), str(prepared), skip_symbolic_shape=True)
        # Только свёртки (SiLU между ними — fp32): декодер Detect и Concat выхода
        # в одной шкале INT8 потеряли бы вероятность так же, как на NPU.
        quantize_static(str(prepared), str(target), Reader(), quant_format=QuantFormat.QDQ,
                        op_types_to_quantize=["Conv"], nodes_to_exclude=exclude, per_channel=True,
                        activation_type=QuantType.QUInt8, weight_type=QuantType.QInt8,
                        calibrate_method=methods[method])
    print(f"int8: {target} — калибровка {method} на {len(files)} кадрах из {calib}, fp32: {len(exclude)} свёрток")


# --- голова YOLOv5 и RKNN ---------------------------------------------------

def head_outputs(model) -> list[str]:
    """Выходы трёх (четырёх у P6) свёрток головы: Conv → Reshape → … → выход графа."""
    consumers: dict[str, list] = {}
    for node in model.graph.node:
        for name in node.input:
            consumers.setdefault(name, []).append(node)
    heads = [node.output[0] for node in model.graph.node if node.op_type == "Conv"
             and any(c.op_type == "Reshape" for c in consumers.get(node.output[0], []))]
    if len(heads) not in (3, 4):
        raise SystemExit(f"голова YOLOv5 не найдена (свёрток перед Reshape: {len(heads)})")
    return heads


def head_model(source: pathlib.Path) -> tuple[bytes, list[str]]:
    import onnx

    model = onnx.load(str(source))
    heads = head_outputs(model)
    with tempfile.TemporaryDirectory() as tmp:
        cut = pathlib.Path(tmp) / "head.onnx"
        onnx.utils.extract_model(str(source), str(cut), [model.graph.input[0].name], heads, check_model=False)
        return cut.read_bytes(), heads


def anchors_from_graph(source: pathlib.Path) -> dict:
    """Страйды и якоря головы — численно, по выходу полного графа на случайном входе.

    ``wh = (2·sigmoid(t))² · якорь`` ⇒ якорь = wh / (2·sigmoid(t))²; медиана по ячейкам.
    Затем декодер движка сверяется с выходом полного графа — расхождение печатается.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from cctv.engine.person_backends import decode_yolov5

    cut, heads = head_model(source)
    data = np.random.default_rng(1).random((1, 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
    full = run(session(source), data)[0]
    parts = run(session(cut), data)
    strides, anchors, offset = [], [], 0
    for head in parts:
        _, channels, height, width = head.shape
        count = 3
        strides.append(INPUT_SIZE // height)
        values = 1 / (1 + np.exp(-head.reshape(count, channels // count, height, width)))
        rows = full[0, offset:offset + count * height * width].reshape(count, height, width, -1)
        offset += count * height * width
        level = []
        for anchor in range(count):
            for axis in (2, 3):
                level.append(float(np.median(rows[anchor, ..., axis] / (2 * values[anchor, axis]) ** 2)))
        anchors.append([round(v, 3) for v in level])
    if offset != full.shape[1]:
        raise SystemExit(f"строк выхода {full.shape[1]}, у головы {offset}")
    person, coords = scores_close(full, decode_yolov5(parts, strides, anchors))
    if person > 1e-3 or coords > 2.0:  # fp16-исходник даёт до ~1 px на рамках 600+ px
        raise SystemExit(f"декодер не совпал с графом: person {person:.5f}, координаты {coords:.3f} px")
    return {"heads": heads, "strides": strides, "anchors": anchors,
            "check": {"person_max_diff": round(person, 6), "coords_max_diff_px": round(coords, 4)}}


def to_rknn(source: pathlib.Path, calib: pathlib.Path, socs: list[str], out_dir: pathlib.Path, limit: int,
            name: str | None, algorithm: str = "normal") -> None:
    from rknn.api import RKNN

    head = anchors_from_graph(source)
    files = frames(calib, limit)
    stem = name or source.name.split(".")[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        dataset = pathlib.Path(tmp) / "dataset.txt"
        lines = []
        for index, path in enumerate(files):
            item = pathlib.Path(tmp) / f"{index:04d}.npy"
            # Ровно вход NPU в движке: blob адаптера × 255 → uint8 (toolkit ждёт .npy в NCHW графа).
            np.save(item, np.round(blob(path) * 255).astype(np.uint8))
            lines.append(str(item))
        dataset.write_text("\n".join(lines) + "\n")
        for chip in socs:
            target = out_dir / f"{stem}.{chip}.rknn"
            rknn = RKNN(verbose=False)
            rknn.config(mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]], target_platform=chip,
                        quantized_algorithm=algorithm, quantized_method="channel", optimization_level=3)
            if rknn.load_onnx(model=str(source), outputs=head["heads"]) != 0:
                raise SystemExit(f"{chip}: load_onnx не прошёл")
            if rknn.build(do_quantization=True, dataset=str(dataset)) != 0:
                raise SystemExit(f"{chip}: build не прошёл")
            if rknn.export_rknn(str(target)) != 0:
                raise SystemExit(f"{chip}: export_rknn не прошёл")
            rknn.release()
            meta = {"family": "yolov5", "decode": "yolov5", "soc": chip, "input_size": INPUT_SIZE,
                    "input_scale": 255, "input_layout": "nhwc_uint8_rgb", "quantization": f"int8 (w8a8, per-channel, {algorithm})",
                    "strides": head["strides"], "anchors": head["anchors"], "head_outputs": head["heads"],
                    "decode_check": head["check"], "calib_frames": len(files),
                    "source": source.name, "source_sha256": sha256(source)}
            target.with_name(target.name + ".json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
            print(f"rknn: {target} ({target.stat().st_size} байт), калибровка на {len(files)} кадрах")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fp32 = sub.add_parser("fp32")
    fp32.add_argument("source", type=pathlib.Path)
    fp32.add_argument("target", type=pathlib.Path)
    int8 = sub.add_parser("int8")
    int8.add_argument("source", type=pathlib.Path)
    int8.add_argument("target", type=pathlib.Path)
    int8.add_argument("--calib", type=pathlib.Path, required=True)
    int8.add_argument("--limit", type=int, default=200)
    int8.add_argument("--method", default="minmax", choices=("minmax", "percentile", "entropy"))
    int8.add_argument("--keep-fp32", default="head", help="свёртки в fp32: head, first через запятую или none")
    rk = sub.add_parser("rknn")
    rk.add_argument("source", type=pathlib.Path, help="fp32 ONNX (после convert.py fp32)")
    rk.add_argument("--calib", type=pathlib.Path, required=True)
    rk.add_argument("--soc", action="append", required=True, choices=("rk3576", "rk3588", "rk3568", "rk3566", "rk3562"))
    rk.add_argument("--out-dir", type=pathlib.Path)
    rk.add_argument("--name", help="имя файла до .<soc>.rknn (по умолчанию — имя исходника до первой точки)")
    rk.add_argument("--limit", type=int, default=200)
    rk.add_argument("--algorithm", default="normal", choices=("normal", "mmse", "kl_divergence"),
                    help="подбор шкал INT8 в rknn-toolkit2")
    check = sub.add_parser("check")
    check.add_argument("source", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.command == "fp32":
        to_fp32(args.source, args.target)
    elif args.command == "int8":
        to_int8(args.source, args.target, args.calib, args.limit, args.method,
                tuple(p for p in args.keep_fp32.split(",") if p != "none"))
    elif args.command == "rknn":
        to_rknn(args.source, args.calib, args.soc, args.out_dir or args.source.parent, args.limit, args.name,
                args.algorithm)
    else:
        print(json.dumps(anchors_from_graph(args.source), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
