"""Диагностика пропусков детектора людей: журнал отвергнутых кандидатов и суточная сводка.

Разбор пропусков 06.10.2026 шёл по ``docker logs`` вручную: ``person_stats`` раз в
минуту давал только максимум уверенности, ``person_still`` писался не чаще раза в
минуту, и причину каждого отказа приходилось восстанавливать. Здесь — то же
знание в виде, пригодном для разбора без перепросмотра видео:

* **эпизод-кандидат** — кадры одной камеры с уверенностью YOLO не ниже
  ``CANDIDATE_MIN`` подряд (разрыв больше ``EPISODE_GAP_SEC`` закрывает эпизод).
  Эпизод, не ставший событием, пишется в журнал одной строкой с причиной отказа
  (ниже порога / один кадр / неподвижный / пол гейта), максимумом уверенности и,
  с троттлингом, снимком кадра с этим максимумом;
* **события** и **сигналы камеры** (ONVIF FieldDetector) — в тот же журнал;
* **суточная сводка** — по камерам и по парам камер, смотрящих на одно место
  (дверь с двух сторон): событие с парой, одиночное объяснимое (на второй камере
  нет никакого следа) и подозрительное (след на второй камере был, события нет).
  Парность не абсолютна — подозрительное есть кандидат на ручную проверку, а не
  доказанный пропуск, поэтому вердикта сводка не выносит.

Цена: строка JSON на эпизод (не на кадр) и не больше одного JPEG на камеру за
``SNAPSHOT_INTERVAL_SEC``. Модуль не знает ни YOLO, ни Telegram.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import pathlib
import re
import sys
import threading
import time

# Нижняя граница кандидата: ниже — фон (p95 шума сцены на камерах 0.1–0.2).
CANDIDATE_MIN = float(os.environ.get("CCTV_DIAG_CANDIDATE_MIN", "0.20"))
EPISODE_GAP_SEC = float(os.environ.get("CCTV_DIAG_EPISODE_GAP_SEC", "15"))
SNAPSHOT_INTERVAL_SEC = float(os.environ.get("CCTV_DIAG_SNAPSHOT_INTERVAL_SEC", "300"))
SNAPSHOT_MIN = float(os.environ.get("CCTV_DIAG_SNAPSHOT_MIN", "0.25"))
SNAPSHOT_KEEP = int(os.environ.get("CCTV_DIAG_SNAPSHOT_KEEP", "200"))
JOURNAL_KEEP_DAYS = int(os.environ.get("CCTV_DIAG_KEEP_DAYS", "30"))
# Пары камер на одно место: "a:b,c:d". Окно пары — с запасом на отставание
# детектора (10–60 с) и путь человека от одной камеры до другой.
PAIRS = os.environ.get("CCTV_DIAG_PAIRS", "")
PAIR_WINDOW_SEC = float(os.environ.get("CCTV_DIAG_PAIR_WINDOW_SEC", "120"))
# Сутки сводки — по местному времени владельца, без зависимости от tzdata в образе.
UTC_OFFSET = os.environ.get("CCTV_DIAG_UTC_OFFSET", "+00:00")
ENABLED = os.environ.get("CCTV_DIAG_ENABLED", "1") != "0"

# Приоритет причины эпизода: самая «близкая к событию» объясняет пропуск лучше.
REASONS = ("still", "single_frame", "gate_floor", "camera_quiet", "below_threshold")


def parse_offset(text: str = UTC_OFFSET) -> datetime.timezone:
    match = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", (text or "").strip())
    if not match:
        return datetime.timezone.utc
    sign = -1 if match.group(1) == "-" else 1
    delta = datetime.timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
    return datetime.timezone(sign * delta)


def local_day(at: float, tz: datetime.timezone | None = None) -> str:
    return datetime.datetime.fromtimestamp(at, tz or parse_offset()).strftime("%Y-%m-%d")


def local_time(at: float, tz: datetime.timezone | None = None) -> str:
    return datetime.datetime.fromtimestamp(at, tz or parse_offset()).strftime("%H:%M:%S")


def parse_pairs(text: str = PAIRS) -> list[tuple[str, str]]:
    pairs = []
    for chunk in (text or "").split(","):
        left, _, right = chunk.strip().partition(":")
        if left and right and left != right:
            pairs.append((left.strip(), right.strip()))
    return pairs


class Journal:
    """JSONL по суткам в ``<state>/diag``; один на процесс, пишут потоки камер."""

    def __init__(self, root: pathlib.Path, tz: datetime.timezone | None = None,
                 log=lambda line: print(line, flush=True)) -> None:
        self.root, self.tz, self.log = root, tz or parse_offset(), log
        self.lock = threading.Lock()

    def path_for(self, day: str) -> pathlib.Path:
        return self.root / f"journal-{day}.jsonl"

    def write(self, record: dict) -> None:
        at = record.get("at") or record.get("start") or time.time()
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with self.lock:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(self.path_for(local_day(at, self.tz)), "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            self.log(f"diag_journal_error error={type(exc).__name__}")

    def read(self, day: str) -> list[dict]:
        try:
            lines = self.path_for(day).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def prune(self, now: float | None = None) -> None:
        cutoff = local_day((now or time.time()) - JOURNAL_KEEP_DAYS * 86400, self.tz)
        for path in self.root.glob("*-????-??-??.*"):
            day = path.stem.rsplit("-", 3)[-3:]
            if "-".join(day) < cutoff:
                path.unlink(missing_ok=True)


@dataclasses.dataclass
class Episode:
    start: float
    end: float
    frames: int = 0
    max_conf: float = 0.0
    box: tuple = ()
    reasons: dict = dataclasses.field(default_factory=dict)
    camera_saw: bool | None = None
    confirm: bool = False
    still_note: str = ""
    best_frame: object = None


class CandidateLog:
    """Эпизоды-кандидаты одной камеры. Вызывается из потока детектора на каждый
    кадр, прошедший YOLO (``frame``), и на каждое событие (``event``)."""

    def __init__(self, camera_id: str, journal: Journal | None, snapshots: pathlib.Path | None = None,
                 encode=None, log=lambda line: print(line, flush=True)) -> None:
        self.camera_id, self.journal, self.snapshots = camera_id, journal, snapshots
        self.encode, self.log = encode, log
        self.episode: Episode | None = None
        self.snapshot_at = 0.0
        self.rejects = 0

    def frame(self, at: float, score: float, reason: str, box=(), camera_saw: bool | None = None,
              confirm: bool = False, still_note: str = "", image=None) -> None:
        """``reason`` — что сделал с кадром детектор: hit (засчитан в серию),
        below_threshold, still, gate_floor."""
        self.tick(at)
        if score < CANDIDATE_MIN and reason != "gate_floor":
            return
        episode = self.episode
        if episode is None:
            episode = self.episode = Episode(start=at, end=at)
        episode.end, episode.frames = at, episode.frames + 1
        episode.reasons[reason] = episode.reasons.get(reason, 0) + 1
        episode.confirm = episode.confirm or confirm
        if camera_saw is not None:
            episode.camera_saw = bool(episode.camera_saw) or bool(camera_saw)
        if score >= episode.max_conf:
            episode.max_conf, episode.box, episode.best_frame = score, tuple(box), image
        if reason == "still" and still_note:
            episode.still_note = still_note

    def broken_series(self, at: float, hits: int) -> None:
        """Серия засчитанных кадров оборвалась, не дотянув до события."""
        if hits and self.episode is not None:
            self.episode.reasons["single_frame"] = self.episode.reasons.get("single_frame", 0) + hits

    def event(self, at: float, score: float, confirm: bool = False, camera_saw: bool | None = None,
              box=(), still_note: str = "") -> None:
        if self.episode is not None:
            self.episode = None  # эпизод стал событием — это не отказ
        if self.journal is not None:
            record = {"kind": "event", "camera": self.camera_id, "at": round(at, 1), "conf": round(score, 2)}
            if confirm:
                record["confirm"] = True
            if camera_saw is not None:
                record["camera_human"] = int(bool(camera_saw))
            # Рамка и вердикт фильтра — чтобы разобрать ложное событие по журналу:
            # лог движка пропадает при пересоздании контейнера (разбор ложного события на камере дачи).
            if box:
                record["box"] = [round(v, 3) for v in box]
            if still_note:
                record["still"] = still_note
            self.journal.write(record)

    def tick(self, at: float) -> None:
        if self.episode is not None and at - self.episode.end > EPISODE_GAP_SEC:
            self.close()

    def close(self) -> None:
        episode, self.episode = self.episode, None
        if episode is None:
            return
        reason = next((r for r in REASONS if episode.reasons.get(r)), "below_threshold")
        snapshot = self._snapshot(episode)
        self.rejects += 1
        record = {"kind": "reject", "camera": self.camera_id, "start": round(episode.start, 1),
                  "end": round(episode.end, 1), "frames": episode.frames, "max_conf": round(episode.max_conf, 2),
                  "reason": reason, "reasons": episode.reasons,
                  "box": [round(v, 3) for v in episode.box]}
        if episode.camera_saw is not None:
            record["camera_human"] = int(episode.camera_saw)
        if episode.confirm:
            record["confirm"] = True
        if episode.still_note:
            record["still"] = episode.still_note
        if snapshot:
            record["snapshot"] = snapshot
        if self.journal is not None:
            self.journal.write(record)
        seen = f" camera_human={record['camera_human']}" if "camera_human" in record else ""
        self.log(f"person_reject camera={self.camera_id} reason={reason} max={episode.max_conf:.2f} "
                 f"frames={episode.frames} dur={episode.end - episode.start:.0f}s{seen}"
                 f"{' snapshot=' + snapshot if snapshot else ''}")

    def _snapshot(self, episode: Episode) -> str | None:
        if (self.snapshots is None or self.encode is None or episode.best_frame is None
                or episode.max_conf < SNAPSHOT_MIN or time.time() - self.snapshot_at < SNAPSHOT_INTERVAL_SEC):
            return None
        try:
            body = self.encode(episode.best_frame)
            if not body:
                return None
            target = self.snapshots / self.camera_id
            target.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.fromtimestamp(episode.start, datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = target / f"{stamp}-{episode.max_conf:.2f}.jpg"
            path.write_bytes(body)
            self.snapshot_at = time.time()
            for old in sorted(target.glob("*.jpg"))[:-SNAPSHOT_KEEP]:
                old.unlink(missing_ok=True)
            return f"{self.camera_id}/{path.name}"
        except OSError:
            return None


# --- сводка -----------------------------------------------------------------

def _near(at: float, moments: list[float], window: float) -> bool:
    return any(abs(at - moment) <= window for moment in moments)


def summarize(records: list[dict], pairs: list[tuple[str, str]] | None = None,
              window: float = PAIR_WINDOW_SEC) -> dict:
    """Счёт по камерам и классы парности. Записи — из ``Journal.read``."""
    pairs = parse_pairs() if pairs is None else pairs
    cameras: dict[str, dict] = {}
    events: dict[str, list[dict]] = {}
    traces: dict[str, list[tuple[float, str, dict]]] = {}
    signals: dict[str, list[float]] = {}
    for record in records:
        camera = record.get("camera")
        if not camera:
            continue
        stats = cameras.setdefault(camera, {"events": 0, "confirm_events": 0, "rejects": {}, "camera_signals": 0})
        kind = record.get("kind")
        if kind == "event":
            stats["events"] += 1
            stats["confirm_events"] += int(bool(record.get("confirm")))
            events.setdefault(camera, []).append(record)
        elif kind == "reject":
            reason = record.get("reason", "below_threshold")
            stats["rejects"][reason] = stats["rejects"].get(reason, 0) + 1
            traces.setdefault(camera, []).append((float(record.get("start", 0)), "reject", record))
        elif kind == "camera_signal":
            stats["camera_signals"] += 1
            signals.setdefault(camera, []).append(float(record["at"]))
            traces.setdefault(camera, []).append((float(record["at"]), "camera_signal", record))
    pair_reports = []
    for left, right in pairs:
        report = {"pair": [left, right], "paired": 0, "single_explained": 0, "suspicious": []}
        for own, other in ((left, right), (right, left)):
            other_events = [float(e["at"]) for e in events.get(other, [])]
            for event in events.get(own, []):
                at = float(event["at"])
                if _near(at, other_events, window):
                    report["paired"] += 1
                    continue
                near = [(t, kind, rec) for t, kind, rec in traces.get(other, []) if abs(at - t) <= window]
                if not near:
                    report["single_explained"] += 1
                    continue
                report["suspicious"].append({
                    "kind": "unpaired_event", "camera": own, "at": at, "conf": event.get("conf"),
                    "missing_on": other, "trace": [_trace(t, kind, rec) for t, kind, rec in near][:3]})
        # Пару событий считаем один раз: выше она учтена с обеих сторон.
        report["paired"] //= 2
        pair_reports.append(report)
    # Камера сама видела человека, а события нет ни на ней, ни (если есть пара) на соседке.
    silent = []
    partner = {a: b for a, b in pairs} | {b: a for a, b in pairs}
    for camera, moments in signals.items():
        own_events = [float(e["at"]) for e in events.get(camera, [])]
        other_events = [float(e["at"]) for e in events.get(partner.get(camera, ""), [])]
        last = None
        for at in sorted(moments):
            if last is not None and at - last <= window:
                continue  # один эпизод камеры — одна строка
            last = at
            if _near(at, own_events, window):
                continue
            rejects = [rec for t, kind, rec in traces.get(camera, []) if kind == "reject" and abs(at - t) <= window]
            silent.append({"kind": "camera_signal_no_event", "camera": camera, "at": at,
                           "partner_event": _near(at, other_events, window),
                           "trace": [_trace(float(r["start"]), "reject", r) for r in rejects][:3]})
    return {"cameras": cameras, "pairs": pair_reports, "camera_signal_no_event": silent}


def _trace(at: float, kind: str, record: dict) -> dict:
    trace = {"at": at, "kind": kind}
    if kind == "reject":
        trace.update(reason=record.get("reason"), max_conf=record.get("max_conf"))
        if record.get("snapshot"):
            trace["snapshot"] = record["snapshot"]
    return trace


REASON_TEXT = {"below_threshold": "ниже порога", "single_frame": "один кадр", "still": "неподвижный",
               "gate_floor": "пол гейта", "camera_quiet": "камера молчит"}


def render(summary: dict, day: str, tz: datetime.timezone | None = None) -> str:
    tz = tz or parse_offset()
    lines = [f"Сводка детектора за {day}"]
    for camera, stats in sorted(summary["cameras"].items()):
        rejects = ", ".join(f"{REASON_TEXT.get(k, k)} {v}" for k, v in sorted(stats["rejects"].items())) or "нет"
        confirm = f" (по сигналу камеры {stats['confirm_events']})" if stats["confirm_events"] else ""
        signals = f"; сигналов камеры {stats['camera_signals']}" if stats["camera_signals"] else ""
        lines.append(f"• {camera}: событий {stats['events']}{confirm}; отказов: {rejects}{signals}")
    for report in summary["pairs"]:
        left, right = report["pair"]
        lines.append(f"Пара {left}↔{right}: парных {report['paired']}, одиночных объяснимых "
                     f"{report['single_explained']}, подозрительных {len(report['suspicious'])}")
        for item in report["suspicious"]:
            lines.append(f"  ? {local_time(item['at'], tz)} {item['camera']} {item.get('conf')}: "
                         f"на {item['missing_on']} события нет, след: {_trace_text(item['trace'], tz)}")
    for item in summary["camera_signal_no_event"]:
        partner = ", у соседки событие есть" if item["partner_event"] else ""
        trace = f", след: {_trace_text(item['trace'], tz)}" if item["trace"] else ""
        lines.append(f"  ? {local_time(item['at'], tz)} {item['camera']}: камера видела человека, "
                     f"события нет{partner}{trace}")
    lines.append("«?» — кандидат на пропуск для ручной проверки, не вердикт.")
    return "\n".join(lines)


def _trace_text(trace: list[dict], tz) -> str:
    parts = []
    for item in trace:
        if item["kind"] == "camera_signal":
            parts.append(f"сигнал камеры {local_time(item['at'], tz)}")
        else:
            text = f"{REASON_TEXT.get(item.get('reason'), item.get('reason'))} {item.get('max_conf')}"
            if item.get("snapshot"):
                text += f" [{item['snapshot']}]"
            parts.append(text)
    return "; ".join(parts) or "—"


def write_summary(journal: Journal, day: str, log=lambda line: print(line, flush=True)) -> dict:
    summary = summarize(journal.read(day))
    text = render(summary, day, journal.tz)
    try:
        journal.root.mkdir(parents=True, exist_ok=True)
        (journal.root / f"summary-{day}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
        (journal.root / f"summary-{day}.txt").write_text(text + "\n")
    except OSError as exc:
        log(f"diag_summary_error error={type(exc).__name__}")
    for camera, stats in sorted(summary["cameras"].items()):
        rejects = ",".join(f"{k}:{v}" for k, v in sorted(stats["rejects"].items()))
        log(f"diag_daily day={day} camera={camera} events={stats['events']} confirm={stats['confirm_events']} "
            f"signals={stats['camera_signals']} rejects={rejects or '-'}")
    for report in summary["pairs"]:
        log(f"diag_pair day={day} pair={':'.join(report['pair'])} paired={report['paired']} "
            f"single={report['single_explained']} suspicious={len(report['suspicious'])}")
    return summary


def daily_loop(journal: Journal, interval: float = 60.0) -> None:
    """Сводка за прошедшие сутки — один раз после местной полуночи."""
    while True:
        try:
            yesterday = local_day(time.time() - 86400, journal.tz)
            if journal.path_for(yesterday).exists() and not (journal.root / f"summary-{yesterday}.txt").exists():
                write_summary(journal, yesterday)
                journal.prune()
        except Exception as exc:  # сводка не имеет права ронять конвейер
            print(f"diag_summary_error error={type(exc).__name__}", flush=True)
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    """``python -m cctv diag-summary [--day YYYY-MM-DD] [--json]`` — сводка вручную."""
    from .. import settings
    parser = argparse.ArgumentParser(prog="cctv diag-summary")
    parser.add_argument("--day", help="местная дата, по умолчанию сегодня")
    parser.add_argument("--dir", help="каталог журнала (по умолчанию <state>/diag)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.dir) if args.dir else settings.engine_state(
        pathlib.Path(os.environ.get("CCTV_STORAGE_ROOT", settings.DEFAULT_STORAGE_ROOT))) / "diag"
    journal = Journal(root)
    day = args.day or local_day(time.time(), journal.tz)
    summary = summarize(journal.read(day))
    print(json.dumps(summary, ensure_ascii=False, indent=1) if args.json else render(summary, day, journal.tz))
    return 0


if __name__ == "__main__":
    sys.exit(main())
