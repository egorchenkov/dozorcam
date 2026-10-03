#!/usr/bin/env python3
"""CCTV Bridge v1: isolated mTLS API for camera frames, events and clips.

Camera URLs and credentials live only in the root-owned JSON configuration named
by ``CCTV_CAMERA_CONFIG``.  This program deliberately never prints that file,
its values, or subprocess stderr.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import cv2
    import numpy
except ImportError:  # Detector can fall back to snapshots when OpenCV is absent.
    cv2 = numpy = None

from .. import settings
from . import camera_discovery as discovery
from . import model_switch
from . import threshold_calibration

MAX_BODY = 64 * 1024
DOWNLOAD_TTL_SECONDS = 60
# Оперативное состояние камеры (пауза, имя, снятие) живёт отдельно от
# /etc/cctv-bridge/cameras.json: тот принадлежит root и мосту недоступен на
# запись — сознательно, чтобы кнопка в чате не могла править адреса и пароли.
OVERRIDES_NAME = "overrides.json"
CAMERA_ACTIONS = {"pause", "resume", "retire", "rename"}
# Пароль камеры, введённый в чате, живёт в памяти моста ровно до конца заведения:
# бот получает непрозрачный токен и второй раз пароль не присылает.
PROBE_TTL_SECONDS = 900
SCAN_TTL_SECONDS = 900
PROVISION_SOCKET = os.environ.get("CCTV_PROVISION_SOCKET", "/run/cctv-provision.sock")
DISCOVERY_NETWORKS = os.environ.get("CCTV_DISCOVERY_NETWORKS", "")
STORAGE_BUDGET_BYTES = int(os.environ.get("CCTV_STORAGE_BUDGET_BYTES", str(8 * 1024 ** 3)))
MIN_FREE_BYTES = int(os.environ.get("CCTV_MIN_FREE_BYTES") or str(4 * 1024 ** 3))
CLIP_SECONDS = 30
# Свежесть новейшего сегмента для статуса online: два интервала записи с запасом.
SEGMENT_FRESH_SEC = 30
ERROR_STATUS = {"camera_offline": 503, "not_found": 404, "unavailable": 503,
                "timeout": 504, "media_too_large": 413, "storage_capacity": 507,
                "clip_window_empty": 409}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code


@dataclass(frozen=True)
class Camera:
    camera_id: str
    title: str
    site: str
    rtsp_url: str
    snapshot_url: str | None = None
    snapshot_user: str | None = None
    snapshot_password: str | None = None
    detect_rtsp_url: str | None = None
    # Порог движения калибруется по конкретной сцене и потоку, поэтому он не может
    # быть одним на весь узел: шум покоя третьего потока дачи (p95 0.57 %) и
    # substream городской камеры (p95 0.06 %) отличаются на порядок. None — брать общий
    # CCTV_MOTION_THRESHOLD.
    motion_threshold: float | None = None
    # Классификатор людей включается по камере: на обеих рабочих он включён с
    # 02.09.2026, но новая камера начинает со штатной детекции движения, пока её
    # сцену не проверили.
    person_detection: bool = False
    # База гейта перед YOLO. Тоже свойство сцены: ночной шум dacha3 (p50 1.1 %)
    # на два порядка выше, чем у соседней dacha2, и общий порог там бесполезен.
    # None — брать общий CCTV_PERSON_GATE_THRESHOLD.
    person_gate_threshold: float | None = None
    # Настоящее отношение ширины к высоте сцены. Нужно там, где камера отдаёт
    # снимок в другой геометрии, чем снимает: Tantos дачи при сцене 16:9
    # знает единственный размер JPEG — 704x576, и картинка в чате вытянута.
    snapshot_aspect: float | None = None
    # Детектор людей читает не main, а записанный substream (detect_rtsp_url).
    # YOLO всё равно сжимает кадр до 640 точек, а декодировать 4 Мп × 25 к/с
    # стоило ~26 % ядра на камеру (замер 23.09.2026) — ради картинки, которую
    # модель тут же уменьшает в 4 раза. Substream G5 выставлен в 1280×720 10 к/с;
    # клипы и запись по-прежнему из main.
    detect_substream: bool = False
    # Камера сама умеет отличать человека (Hikvision G2/G5: Smart/FieldDetection с
    # целью human) и отдаёт это по ONVIF как RuleEngine/FieldDetector/ObjectsInside.
    # Флаг включает гейт YOLO по этому сигналу (см. cctv_pipeline.HUMAN_GATE_MODE).
    # Старые классы (городская G0, Tantos дачи) цель не классифицируют —
    # у них остаётся серверный frame-diff-гейт.
    camera_human_events: bool = False


def correct_aspect(jpeg: bytes, aspect: float | None) -> bytes:
    """Вернуть снимку настоящие пропорции сцены.

    Tantos дачи снимает 16:9 (main 2880x1620), а JPEG отдаёт единственного
    размера — 704x576: тот же кадр, сжатый по горизонтали, отчего дом и люди в
    чате вытянуты вверх. Другого снимка у камеры нет (GetSnapshotUri одинаков
    для всех профилей), поэтому геометрию правим у себя. Растягиваем по ширине,
    а не сжимаем по высоте: строки кадра при этом не теряются. Ошибка снимка
    без cv2 не фатальна — лучше кривые пропорции, чем отсутствие картинки.
    """
    if not aspect or cv2 is None or numpy is None:
        return jpeg
    frame = cv2.imdecode(numpy.frombuffer(jpeg, dtype="uint8"), cv2.IMREAD_COLOR)
    if frame is None:
        return jpeg
    height, width = frame.shape[:2]
    if not height or abs(width / height / aspect - 1) <= 0.02:
        return jpeg
    resized = cv2.resize(frame, (max(1, round(height * aspect)), height), interpolation=cv2.INTER_CUBIC)
    ok, encoded = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return encoded.tobytes() if ok else jpeg


@dataclass
class Blob:
    body: bytes
    content_type: str
    expires_at: float


class Bridge:
    def __init__(self, config: dict, storage: pathlib.Path, public_url: str) -> None:
        self.cameras = {c.camera_id: c for c in self._cameras(config)}
        self.storage, self.public_url = storage, public_url.rstrip("/")
        self.events_url = os.environ.get("CCTV_EVENTS_URL")
        self.events_cert = os.environ.get("CCTV_EVENTS_CERT")
        self.events_key = os.environ.get("CCTV_EVENTS_KEY")
        self.events_ca = os.environ.get("CCTV_EVENTS_CA")
        self.max_snapshot = int(os.environ.get("CCTV_MAX_SNAPSHOT_BYTES", str(8 * 1024 * 1024)))
        self.max_clip = int(os.environ.get("CCTV_MAX_CLIP_BYTES", str(48 * 1024 * 1024)))
        self._requests: dict[str, float] = {}
        self._event_ids: dict[str, float] = {}
        self._lock = threading.Lock()
        # Поиск камер и разовые «пробы» с паролем: и то и другое — состояние
        # процесса, а не диска. Перезапуск моста их обнуляет, и это правильно.
        self._scans: dict[str, dict] = {}
        self._probes: dict[str, tuple[float, dict, dict]] = {}
        self.networks = DISCOVERY_NETWORKS
        self.provision_socket = PROVISION_SOCKET
        registry = os.environ.get("CCTV_REGISTRY_FILE")
        self.registry_file = pathlib.Path(registry) if registry else None
        self.registry_seed = pathlib.Path(os.environ.get("CCTV_REGISTRY_SEED")
                                          or os.environ.get("CCTV_CAMERA_CONFIG") or "/nonexistent")
        self.registry_backups = pathlib.Path(os.environ.get("CCTV_PROVISION_BACKUPS")
                                             or settings.engine_state(storage) / "registry-backups")
        self.motion_stale_after = int(os.environ.get("CCTV_MOTION_STALE_SEC", "60"))
        # Состояние (килобайты) и буфер сегментов (tmpfs) живут отдельно от
        # транзита моста: CCTV_STATE_DIR / CCTV_BUFFER_DIR, иначе — внутри storage.
        self.state_dir, self.buffer_dir = settings.engine_state(storage), settings.engine_buffer(storage)
        for path in (self.buffer_dir, self.state_dir):
            path.mkdir(parents=True, exist_ok=True)
        for name in ("events/pending", "events/delivered", "tmp", "media"):
            (storage / name).mkdir(parents=True, exist_ok=True)
        self._sweep_stale_pending()

    def _sweep_stale_pending(self) -> None:
        """Pending-запись перекладывает в delivered поток клипа движения, но он
        умирает вместе с процессом: осиротевшие записи копились бы вечно, потому
        что retention-guard чистит только delivered."""
        cutoff = time.time() - 3600
        for entry in (self.storage / "events" / "pending").glob("*.json"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.replace(self.storage / "events" / "delivered" / entry.name)
            except OSError:
                continue

    @staticmethod
    def _cameras(config: dict) -> list[Camera]:
        result = []
        for raw in config.get("cameras", []):
            if not all(isinstance(raw.get(k), str) and raw[k] for k in ("camera_id", "title", "rtsp_url")):
                raise ValueError("camera config is incomplete")
            result.append(Camera(raw["camera_id"], raw["title"], raw.get("site", raw["camera_id"]),
                                 raw["rtsp_url"], raw.get("snapshot_url"), raw.get("snapshot_user"),
                                 raw.get("snapshot_password"), raw.get("detect_rtsp_url"),
                                 float(raw["motion_threshold"]) if raw.get("motion_threshold") is not None else None,
                                 raw.get("person_detection") is True,
                                 float(raw["person_gate_threshold"]) if raw.get("person_gate_threshold") is not None else None,
                                 float(raw["snapshot_aspect"]) if raw.get("snapshot_aspect") is not None else None,
                                 raw.get("detect_substream") is True,
                                 raw.get("camera_human_events") is True))
        return result

    @property
    def overrides_path(self) -> pathlib.Path:
        return self.state_dir / OVERRIDES_NAME

    def overrides(self) -> dict:
        try:
            data = json.loads(self.overrides_path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_override(self, camera_id: str, action: str, title: str | None = None) -> dict:
        """Пауза/возобновление/переименование/снятие. Реестр камер не трогаем."""
        if camera_id not in self.cameras or action not in CAMERA_ACTIONS:
            raise BridgeError("not_found" if camera_id not in self.cameras else "unavailable")
        if action == "rename" and not (isinstance(title, str) and title.strip()):
            raise BridgeError("unavailable")
        with self._lock:
            data = self.overrides()
            entry = dict(data.get(camera_id) or {})
            if action == "pause":
                entry["status"] = "paused"
            elif action == "resume":
                entry.pop("status", None)
            elif action == "retire":
                entry["status"] = "retired"
            else:
                entry["title"] = title.strip()[:64]
            entry["changed_at"] = now()
            if entry.keys() <= {"changed_at"}:
                data.pop(camera_id, None)
            else:
                data[camera_id] = entry
            tmp = self.overrides_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            tmp.replace(self.overrides_path)  # подмена целиком: половину файла не прочитают
        return {"camera_id": camera_id, "action": action}

    def storage_health(self) -> dict:
        """Диск — транзит: архив живёт в Telegram. Сторожим только переполнение."""
        used = 0
        roots = [self.storage] + [extra for extra in (self.buffer_dir, self.state_dir)
                                  if not extra.is_relative_to(self.storage)]
        for path in (item for root in roots for item in root.rglob("*")):
            try:
                if path.is_file():
                    used += path.stat().st_size
            except OSError:
                continue
        try:
            stat = os.statvfs(self.storage)
            free = stat.f_bavail * stat.f_frsize
        except OSError:
            free = -1
        # Порог «мало места» — тот же, что у retention-guard движка: бот его
        # не угадывает. Жёсткие 2 ГиБ в боте при tmpfs-транзите 256–512 МиБ
        # давали тревогу сразу после старта контейнера (стенд Э3, 02.10.2026).
        return {"used_bytes": used, "budget_bytes": STORAGE_BUDGET_BYTES, "free_bytes": free,
                "min_free_bytes": MIN_FREE_BYTES}

    def detector_models(self) -> dict:
        """Меню модели детектора: семейства, файлы в каталогах моделей, активная, исход смены."""
        return model_switch.catalog(self.state_dir)

    def switch_detector_model(self, request: dict) -> dict:
        """Заявка на смену модели; перезагружает детекторы конвейер (model_switch)."""
        try:
            return model_switch.request_switch(self.state_dir, str(request.get("family") or ""),
                                               str(request.get("model") or ""))
        except LookupError:
            raise BridgeError("not_found")
        except ValueError:
            raise BridgeError("unavailable")

    def _yolo_cameras(self) -> list[tuple[str, str]]:
        return [(c.camera_id, c.title) for c in self.cameras.values() if c.person_detection]

    def detector_thresholds(self) -> dict:
        """Итоговый порог детектора по камерам: ручной, автокалибровка или стартовый."""
        active = model_switch.catalog(self.state_dir).get("active")
        return threshold_calibration.overview(self.state_dir, self._yolo_cameras(), active)

    def set_detector_threshold(self, request: dict) -> dict:
        """Ручной порог камеры под активную модель; value=null — вернуть автокалибровку."""
        camera_id = str(request.get("camera_id") or "")
        if camera_id not in dict(self._yolo_cameras()):
            raise BridgeError("not_found")
        value = request.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise BridgeError("unavailable")
        try:
            return threshold_calibration.set_override(self.state_dir, camera_id,
                                                      None if value is None else float(value),
                                                      model_switch.catalog(self.state_dir).get("active"))
        except ValueError:
            raise BridgeError("unavailable")

    def calibrate_detector(self, request: dict) -> dict:
        """Заявка «Откалибровать»: все камеры с YOLO или одна (camera_id)."""
        camera_id = str(request.get("camera_id") or "")
        if camera_id and camera_id not in dict(self._yolo_cameras()):
            raise BridgeError("not_found")
        return threshold_calibration.request_calibration(self.state_dir, [camera_id] if camera_id else None)

    def registry(self) -> dict:
        cameras, overrides = [], self.overrides()
        for camera in self.cameras.values():
            latest, fresh = self._latest_segment(camera.camera_id)
            entry = overrides.get(camera.camera_id) or {}
            forced = entry.get("status")
            # Снятая и поставленная на паузу камера показывает своё состояние, а
            # не «онлайн»: иначе пауза выглядела бы как поломка потока.
            status = forced if forced in ("paused", "retired") else ("online" if fresh else "unavailable")
            cameras.append({"camera_id": camera.camera_id,
                            "title": entry.get("title") or camera.title, "site": camera.site,
                            "status": status, "last_frame_at": latest,
                            "motion": self.motion_health(camera.camera_id)})
        return {"cameras": cameras, "storage": self.storage_health()}

    def motion_health(self, camera_id: str) -> dict:
        """Живость детектора берётся из его пульса: детектор — соседний процесс.

        Протухший или отсутствующий пульс — это «не знаем», а не «работает»: иначе
        остановленный детектор выглядел бы в теме включённым.
        """
        path = self.state_dir / f"{camera_id}.motion.json"
        try:
            beat = json.loads(path.read_text())
            age = time.time() - path.stat().st_mtime
        except (OSError, ValueError):
            return {"state": "unknown", "reason": "no_heartbeat", "last_motion_at": None}
        if age > self.motion_stale_after:
            return {"state": "stalled", "reason": "heartbeat_stale",
                    "last_motion_at": beat.get("last_motion_at")}
        return {"state": str(beat.get("state") or "unknown"), "reason": str(beat.get("reason") or ""),
                "last_motion_at": beat.get("last_motion_at")}

    # --- заведение камер ---------------------------------------------------
    def start_scan(self, networks: str | list | None = None) -> dict:
        """Опрос сетей идёт фоном: /24 на пяти портах не укладывается в один HTTP."""
        # Сети не заданы — собственные /24 узла плюс ответившие на WS-Discovery:
        # домашней установке не нужно знать, что такое CIDR, чтобы найти камеру.
        target = networks or self.networks or discovery.local_networks()
        if not target:
            return {"ok": False, "error": "сети для поиска не заданы (CCTV_DISCOVERY_NETWORKS)"}
        try:
            parsed = discovery.parse_networks(target)
        except discovery.DiscoveryError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self._sweep_scans()
            running = next((sid for sid, job in self._scans.items()
                            if job["status"] == "running"), None)
            if running:
                # Второй одновременный опрос той же сети — лишняя нагрузка на
                # туннель и ничего нового: отдаём уже идущий.
                return {"ok": True, "scan_id": running, "status": "running"}
            scan_id = secrets.token_urlsafe(8)
            self._scans[scan_id] = {"status": "running", "started": time.time(),
                                    "candidates": [], "networks": [str(n) for n in parsed]}

        def worker() -> None:
            try:
                found = discovery.scan(parsed, extra_hosts=discovery.ws_discover())
                result = {"status": "done", "candidates": [c.as_dict() for c in found]}
            except Exception as exc:  # опрос сети не должен ронять мост
                result = {"status": "failed", "candidates": [],
                          "error": f"опрос не удался ({type(exc).__name__})"}
            with self._lock:
                job = self._scans.get(scan_id)
                if job is not None:
                    job.update(result)
                    job["finished"] = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "scan_id": scan_id, "status": "running",
                "networks": [str(n) for n in parsed]}

    def scan_status(self, scan_id: str) -> dict:
        # Реестр меняется только перезапуском цепочки, поэтому соответствие
        # «хост → камера» можно вычислить и вне блокировки.
        registered = self._registered_hosts()
        with self._lock:
            job = self._scans.get(scan_id)
            if job is None:
                raise BridgeError("not_found")
            candidates = [dict(c, registered_camera_id=registered.get(str(c.get("host") or ""), ""))
                          for c in job["candidates"]]
            return {"ok": True, "scan_id": scan_id, "status": job["status"],
                    "candidates": candidates,
                    "error": job.get("error", ""), "networks": job.get("networks", [])}

    def _registered_hosts(self) -> dict[str, str]:
        """Хост → camera_id по реестру. Кандидат поиска с таким хостом — уже
        заведённая камера: предлагать её кнопкой «добавить» значит заводить дубль."""
        hosts: dict[str, str] = {}
        for camera_id, camera in self.cameras.items():
            for url in (camera.rtsp_url, camera.detect_rtsp_url, camera.snapshot_url):
                host = urllib.parse.urlsplit(url).hostname if url else ""
                if host:
                    hosts.setdefault(host, camera_id)
        return hosts

    def _sweep_scans(self) -> None:
        cutoff = time.time() - SCAN_TTL_SECONDS
        for scan_id in [sid for sid, job in self._scans.items()
                        if job.get("finished", job["started"]) < cutoff]:
            self._scans.pop(scan_id, None)

    def probe(self, host: str, username: str, password: str, detect_url: str = "") -> dict:
        """Определить параметры камеры. Пароль остаётся здесь, наружу — токен.

        `host` — IP или готовый rtsp://… (камера вне поиска); `detect_url` —
        необязательный поток детектора той же камеры.
        """
        if not all(isinstance(v, str) for v in (host, username, password, detect_url)):
            raise BridgeError("unavailable")
        try:
            found = discovery.probe(host.strip(), username, password,
                                    detect_url=detect_url.strip())
        except discovery.DiscoveryError as exc:
            return {"ok": False, "error": str(exc)}
        entry = {
            "rtsp_url": found.main_url,
            "detect_rtsp_url": found.sub_url or found.main_url,
        }
        if found.snapshot_url:
            entry["snapshot_url"] = found.snapshot_url
            entry["snapshot_user"] = username
            entry["snapshot_password"] = password
        token = secrets.token_urlsafe(16)
        summary = found.summary()
        with self._lock:
            self._probes = {t: v for t, v in self._probes.items() if v[0] > time.time()}
            self._probes[token] = (time.time() + PROBE_TTL_SECONDS, entry, summary)
        return {"ok": True, "probe_token": token, "summary": summary}

    def _take_probe(self, token: str) -> dict:
        with self._lock:
            found = self._probes.get(str(token or ""))
        if found is None or found[0] <= time.time():
            raise BridgeError("not_found")
        return dict(found[1])

    def provision(self, command: dict) -> dict:
        """Единственный путь записи в реестр — писарь: сокет root-процесса или,
        в контейнере, тот же писарь в процессе моста над файлом из state.

        Контейнер не даёт ни root, ни systemd, а каталог конфига там только на
        чтение. Поэтому CCTV_REGISTRY_FILE (его выставляет супервизор контейнера)
        включает локальную запись: проверка полей та же (`cctv_provision.apply`),
        а «перезапуск цепочки» делает супервизор — он видит смену файла.
        """
        if self.registry_file is not None:
            return self._provision_local(command)
        payload = (json.dumps(command, ensure_ascii=False) + "\n").encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(20)
                sock.connect(self.provision_socket)
                sock.sendall(payload)
                chunks = []
                while b"\n" not in b"".join(chunks):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError:
            return {"ok": False, "error": "писарь реестра недоступен"}
        try:
            reply = json.loads(b"".join(chunks).split(b"\n", 1)[0] or b"{}")
        except ValueError:
            return {"ok": False, "error": "писарь реестра ответил непонятным"}
        return reply if isinstance(reply, dict) else {"ok": False, "error": "неверный ответ писаря"}

    def _provision_local(self, command: dict) -> dict:
        from . import cctv_provision as writer

        target = self.registry_file
        with self._lock:
            try:
                if command.get("command") == "list" and not target.exists():
                    reply, _ = writer.apply(command, path=self.registry_seed, backups=self.registry_backups)
                    return reply
                if not target.exists() and self.registry_seed.is_file():
                    # Первая правка из чата: реестр из каталога конфига — исходник.
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.registry_seed, target)
                reply, _restart = writer.apply(command, path=target, backups=self.registry_backups)
            except writer.Invalid as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # содержимое команды (пароль) в ответ не попадает
                return {"ok": False, "error": f"внутренняя ошибка писаря ({type(exc).__name__})"}
        return reply

    def registry_config(self, camera_id: str | None = None) -> dict:
        """Что записано в реестре — без паролей: их не отдаём даже боту."""
        reply = self.provision({"command": "list"})
        if not reply.get("ok"):
            return reply
        cameras = reply.get("cameras") or []
        if camera_id is None:
            return {"ok": True, "cameras": cameras}
        found = next((c for c in cameras if c.get("camera_id") == camera_id), None)
        if found is None:
            raise BridgeError("not_found")
        return {"ok": True, "camera": found}

    def add_camera(self, request: dict) -> dict:
        camera_id = str(request.get("camera_id") or "").strip()
        entry = self._take_probe(request.get("probe_token"))
        entry.update({
            "camera_id": camera_id,
            "title": str(request.get("title") or camera_id).strip(),
            "site": str(request.get("site") or request.get("title") or camera_id).strip(),
        })
        if request.get("person_detection") is True:
            entry["person_detection"] = True
        if camera_id in self.cameras:
            return {"ok": False, "error": "камера с таким camera_id уже есть"}
        return self.provision({"command": "upsert", "camera": entry})

    def update_camera(self, camera_id: str, request: dict) -> dict:
        """Правка существующей записи: новые пароль/потоки или флаги детекции."""
        if camera_id not in self.cameras:
            raise BridgeError("not_found")
        entry: dict = {"camera_id": camera_id}
        if request.get("probe_token"):
            entry.update(self._take_probe(request.get("probe_token")))
        for key in ("title", "site"):
            if isinstance(request.get(key), str) and request[key].strip():
                entry[key] = request[key].strip()
        if isinstance(request.get("person_detection"), bool):
            entry["person_detection"] = request["person_detection"]
        if request.get("motion_threshold") is not None:
            entry["motion_threshold"] = request["motion_threshold"]
        if request.get("person_gate_threshold") is not None:
            entry["person_gate_threshold"] = request["person_gate_threshold"]
        if request.get("snapshot_aspect") is not None:
            entry["snapshot_aspect"] = request["snapshot_aspect"]
        if set(entry) == {"camera_id"}:
            return {"ok": False, "error": "нечего менять"}
        # Отключение детекции людей — снятие ключа, а писарь сливает записи:
        # поэтому False передаём как явную замену всей записи.
        if entry.get("person_detection") is False:
            current = self.cameras[camera_id]
            base = {"camera_id": camera_id, "title": current.title, "site": current.site,
                    "rtsp_url": current.rtsp_url}
            for key, value in (("detect_rtsp_url", current.detect_rtsp_url),
                               ("snapshot_url", current.snapshot_url),
                               ("snapshot_user", current.snapshot_user),
                               ("snapshot_password", current.snapshot_password),
                               ("motion_threshold", current.motion_threshold),
                               ("person_gate_threshold", current.person_gate_threshold),
                               ("snapshot_aspect", current.snapshot_aspect)):
                if value is not None:
                    base[key] = value
            base.update({k: v for k, v in entry.items() if k != "person_detection"})
            base.pop("person_detection", None)
            return self.provision({"command": "upsert", "camera": base, "replace": True})
        return self.provision({"command": "upsert", "camera": entry})

    def delete_camera(self, camera_id: str) -> dict:
        if camera_id not in self.cameras:
            raise BridgeError("not_found")
        return self.provision({"command": "delete", "camera_id": camera_id})

    def accept(self, request: dict) -> None:
        request_id = request.get("request_id")
        camera_id, kind = request.get("camera_id"), request.get("kind")
        if not isinstance(request_id, str) or not isinstance(camera_id, str) or kind not in {"snapshot", "clip"}:
            raise BridgeError("unavailable")
        if camera_id not in self.cameras:
            raise BridgeError("not_found")
        with self._lock:
            # Дедуп с прополкой: прежде словарь заявок рос без ограничения весь аптайм.
            cutoff = time.time() - 86400
            self._requests = {key: value for key, value in self._requests.items() if value >= cutoff}
            if request_id in self._requests:
                return
            self._requests[request_id] = time.time()
        threading.Thread(target=self._make_media, args=(request,), daemon=True).start()

    def _make_media(self, request: dict) -> None:
        try:
            camera = self.cameras[request["camera_id"]]
            kind = request["kind"]
            if kind == "snapshot":
                body, captured_at = self.snapshot(camera)
                content_type = "image/jpeg"
            else:
                center = request.get("center_at") or now()
                body, captured_at = self.clip(camera, center)
                content_type = "video/mp4"
            limit = self.max_snapshot if kind == "snapshot" else self.max_clip
            if len(body) > limit:
                raise BridgeError("media_too_large")
            token = self.issue_token(body, content_type)
            event = {"event_id": str(uuid.uuid4()), "type": "media.ready", "request_id": request["request_id"],
                     "camera_id": camera.camera_id, "kind": kind, "occurred_at": now(), "captured_at": captured_at,
                     "content_type": content_type, "bytes": len(body), "sha256": sha(body),
                     "download": {"url": f"{self.public_url}/v1/media/{token}", "expires_at": now_plus(DOWNLOAD_TTL_SECONDS),
                                  "sha256": sha(body), "bytes": len(body), "content_type": content_type}}
            self.push_event(event)
        except BridgeError as exc:
            self.push_event({"event_id": str(uuid.uuid4()), "type": "media.failed", "request_id": request["request_id"],
                             "camera_id": request["camera_id"], "kind": request["kind"], "error": exc.code})
        except Exception:
            self.push_event({"event_id": str(uuid.uuid4()), "type": "media.failed", "request_id": request["request_id"],
                             "camera_id": request["camera_id"], "kind": request["kind"], "error": "unavailable"})

    def snapshot(self, camera: Camera) -> tuple[bytes, str]:
        target = self.storage / "tmp" / f"{uuid.uuid4()}.jpg"
        # -rw_timeout у RTSP-демуксера нет: ffmpeg выходил с "Option not found" мгновенно,
        # и основной путь снимка всегда молча уступал ISAPI-fallback. Опции — только для RTSP.
        transport = ["-rtsp_transport", "tcp", "-timeout", "5000000"] if camera.rtsp_url.startswith("rtsp://") else []
        # Первый кадр RTSP-сессии брать нельзя: Дача-3 (DS-2CD2043G2) в начале
        # каждого TCP-подключения к main шлёт рваный interleave, первый I-кадр
        # обрезан, и снимок уезжает с полосами ниже ~90-й строки (23.09.2026:
        # 8 из 8 проб). Декодируем только ключевые кадры и берём второй — GOP у
        # камер 2 с, лишняя задержка укладывается в timeout.
        keyframes = ["-skip_frame", "nokey"] if camera.rtsp_url.startswith("rtsp://") else []
        pick = ["-vf", r"select=gte(n\,1)"] if keyframes else []
        try:
            completed = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *transport, *keyframes, "-i",
                                        camera.rtsp_url, *pick, "-frames:v", "1", str(target)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
            if completed.returncode == 0 and target.is_file():
                return target.read_bytes(), now()
        except (subprocess.TimeoutExpired, OSError):
            pass
        finally:
            target.unlink(missing_ok=True)
        # Approved fallback, intentionally only after the RTSP primary has failed.
        if not camera.snapshot_url:
            raise BridgeError("camera_offline")
        try:
            password = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            password.add_password(None, camera.snapshot_url, camera.snapshot_user or "", camera.snapshot_password or "")
            # Basic-обработчик обязателен рядом с digest: Tantos (Qualvision) отдаёт на
            # /onvif/Snapshot ДВА заголовка WWW-Authenticate, Basic первым. Digest-обработчик
            # urllib читает только первый заголовок, схему не узнаёт и молча отдаёт 401 —
            # snapshot-fallback для такой камеры не работал бы вообще. Hikvision присылает
            # только Digest, для него порядок обработчиков ничего не меняет.
            opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(password),
                                                 urllib.request.HTTPDigestAuthHandler(password))
            with opener.open(camera.snapshot_url, timeout=8) as response:
                body = response.read(self.max_snapshot + 1)
            if len(body) > self.max_snapshot:
                raise BridgeError("media_too_large")
            return correct_aspect(body, camera.snapshot_aspect), now()
        except BridgeError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BridgeError("camera_offline")

    def clip(self, camera: Camera, center_at: str) -> tuple[bytes, str]:
        try:
            centre = parse_time(center_at)
        except (TypeError, ValueError):
            raise BridgeError("unavailable")
        parts = []
        camera_dir = self.buffer_dir / camera.camera_id
        for entry in sorted(camera_dir.glob("*.ts")):
            try:
                stamp = parse_time(entry.stem)
            except ValueError:
                continue
            if not -15 <= (stamp - centre).total_seconds() <= 15:
                continue
            # Пустые сегменты в окно не берём. Рекордер каждые ~130 с упирается в
            # свой timeout=, обрывает ffmpeg и оставляет за собой сегмент нулевой
            # длины (плюс всегда пуст тот, что пишется прямо сейчас). concat на
            # таком файле либо молча обрывает клип на середине, либо — когда
            # пустой сегмент первый в окне — падает целиком, и запрос клипа
            # возвращает unavailable вместо видео. Воспроизведено 01.09.2026:
            # один из четырёх запросов клипа в рантайме приходил отказом.
            try:
                if entry.stat().st_size == 0:
                    continue
            except OSError:
                continue
            parts.append(entry)
        if not parts:
            # Окно ±15 c вне кольцевого буфера — это не отказ сервиса, и бот
            # обязан сказать про устаревший кадр, а не про недоступность.
            raise BridgeError("clip_window_empty")
        manifest = self.storage / "tmp" / f"{uuid.uuid4()}.txt"
        output = self.storage / "tmp" / f"{uuid.uuid4()}.mp4"
        try:
            manifest.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in parts))
            run = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
                                  str(manifest), "-t", str(CLIP_SECONDS), "-c", "copy", "-movflags", "+faststart", str(output)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if run.returncode != 0 or not output.is_file():
                raise BridgeError("unavailable")
            return output.read_bytes(), centre.replace(microsecond=0).isoformat()
        except subprocess.TimeoutExpired:
            raise BridgeError("timeout")
        finally:
            manifest.unlink(missing_ok=True)
            output.unlink(missing_ok=True)

    # Токены живут на ДИСКЕ, а не в памяти: их выдаёт и детектор (cctv-pipeline —
    # отдельный процесс со своим объектом Bridge), а отдаёт по HTTP процесс
    # cctv-bridge. Токен в памяти детектора для HTTP-сервера не существовал, и
    # каждое медиа движения кончалось 404 → «сервис временно недоступен».
    def issue_token(self, body: bytes, content_type: str) -> str:
        self._sweep_media()
        token = uuid.uuid4().hex
        media_dir = self.storage / "media"
        blob_tmp = media_dir / f"{token}.bin.tmp"
        blob_tmp.write_bytes(body)
        blob_tmp.replace(media_dir / f"{token}.bin")
        meta_tmp = media_dir / f"{token}.json.tmp"
        meta_tmp.write_text(json.dumps({"content_type": content_type,
                                        "expires_at": time.time() + DOWNLOAD_TTL_SECONDS}))
        # Метафайл появляется последним: его наличие означает «блоб готов целиком».
        meta_tmp.replace(media_dir / f"{token}.json")
        return token

    def take_token(self, token: str) -> Blob | None:
        # Токен приходит из URL: всё, что не hex от uuid4, не превращается в путь.
        if len(token) != 32 or not all(c in "0123456789abcdef" for c in token):
            return None
        meta_path = self.storage / "media" / f"{token}.json"
        blob_path = self.storage / "media" / f"{token}.bin"
        try:
            meta = json.loads(meta_path.read_text())
            body = blob_path.read_bytes()
        except (OSError, ValueError):
            return None
        meta_path.unlink(missing_ok=True)
        blob_path.unlink(missing_ok=True)
        expires_at = float(meta.get("expires_at") or 0)
        if expires_at < time.time():
            return None
        return Blob(body, str(meta.get("content_type") or "application/octet-stream"), expires_at)

    def _sweep_media(self) -> None:
        """Невыкупленные блобы прежде жили в памяти вечно — до 48 МБ на каждый отказ."""
        media_dir = self.storage / "media"
        try:
            entries = list(media_dir.iterdir())
        except OSError:
            return
        for meta_path in (p for p in entries if p.suffix == ".json"):
            try:
                if float(json.loads(meta_path.read_text()).get("expires_at") or 0) < time.time():
                    meta_path.with_suffix(".bin").unlink(missing_ok=True)
                    meta_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue
        for stray in entries:
            # Осиротевшие .bin и .tmp (упали между двумя записями) не должны копиться.
            try:
                if (stray.suffix in (".tmp",) or (stray.suffix == ".bin" and not stray.with_suffix(".json").exists())) \
                        and time.time() - stray.stat().st_mtime > DOWNLOAD_TTL_SECONDS * 5:
                    stray.unlink(missing_ok=True)
            except OSError:
                continue

    def _latest_segment(self, camera_id: str) -> tuple[str | None, bool]:
        """Имя новейшего сегмента и его свежесть: старый буфер не делает камеру online.

        Прежде статус был «online», пока в буфере лежал хоть один сегмент любой
        давности — умерший рекордер оставался невидимым, буфер сам себя не чистит.
        """
        paths = list((self.buffer_dir / camera_id).glob("*.ts"))
        if not paths:
            return None, False
        try:
            newest = max(paths, key=lambda p: p.stat().st_mtime)
            return newest.stem, time.time() - newest.stat().st_mtime <= SEGMENT_FRESH_SEC
        except OSError:
            return None, False

    def push_event(self, event: dict) -> None:
        if not self.events_url:  # An unconfigured receiver must not leak or queue media.
            return
        with self._lock:
            cutoff = time.time() - 86400
            self._event_ids = {key: value for key, value in self._event_ids.items() if value >= cutoff}
            if event["event_id"] in self._event_ids:
                return
            self._event_ids[event["event_id"]] = time.time()
        try:
            context = self.events_context()
            payload = json.dumps(event).encode()
            req = urllib.request.Request(self.events_url, payload, {"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, context=context, timeout=10):
                pass
        except (OSError, urllib.error.URLError, ValueError):
            pass  # Never log URL, certificate paths, or camera-specific diagnostics.

    def events_context(self) -> ssl.SSLContext | None:
        """https — только mTLS (клиентский сертификат + CA бота); http — только loopback.

        Открытый http за пределы узла запрещён: событие несёт ссылку на кадр, и
        без TLS её прочёл бы любой в сети. Такой адрес — отказ, а не тихий фолбэк.
        """
        parts = urllib.parse.urlsplit(self.events_url or "")
        if parts.scheme == "https":
            context = ssl.create_default_context(cafile=self.events_ca)
            context.load_cert_chain(self.events_cert, self.events_key)
            return context
        if parts.scheme == "http" and settings.is_loopback_host(parts.hostname):
            return None
        raise ValueError("events URL: http допускается только на loopback")

    def motion(self, camera: Camera, captured_at: str | None = None, *, source: str = "rtsp_frame_diff",
               snapshot_body: bytes | None = None) -> None:
        """Publish one event; an already captured frame avoids a new camera session."""
        try:
            if snapshot_body is None:
                body, captured_at = self.snapshot(camera)
            else:
                body = snapshot_body
            token = self.issue_token(body, "image/jpeg")
            event_id = str(uuid.uuid4())
            self.push_event({"event_id": event_id, "type": "motion.detected", "camera_id": camera.camera_id,
                             "occurred_at": captured_at or now(), "source": source,
                             "snapshot": {"url": f"{self.public_url}/v1/media/{token}",
                                          "expires_at": now_plus(DOWNLOAD_TTL_SECONDS), "sha256": sha(body),
                                          "bytes": len(body)}})
            record = {"event_id": event_id, "camera_id": camera.camera_id,
                      "occurred_at": captured_at or now(), "clip_id": None, "state": "pending"}
            target = self.storage / "events" / "pending" / f"{event_id}.json"
            temp = target.with_suffix(".tmp"); temp.write_text(json.dumps(record)); temp.replace(target)
            threading.Thread(target=self._motion_clip, args=(camera, event_id, captured_at or now(), target), daemon=True).start()
        except BridgeError:
            return

    def _motion_clip(self, camera: Camera, event_id: str, occurred_at: str, record_path: pathlib.Path) -> None:
        """Attach a clip only after its post-window exists; the motion event stays valid without it."""
        time.sleep(15)
        try:
            body, captured_at = self.clip(camera, occurred_at)
            if len(body) > self.max_clip: raise BridgeError("media_too_large")
            clip_id, token = str(uuid.uuid4()), self.issue_token(body, "video/mp4")
            record = json.loads(record_path.read_text()); record.update({"clip_id": clip_id, "state": "ready"})
            temp = record_path.with_suffix(".tmp"); temp.write_text(json.dumps(record)); temp.replace(record_path)
            self.push_event({"event_id": str(uuid.uuid4()), "type": "media.ready", "source_event_id": event_id,
                             "request_id": event_id, "camera_id": camera.camera_id, "kind": "clip", "clip_id": clip_id,
                             "occurred_at": occurred_at, "captured_at": captured_at, "content_type": "video/mp4",
                             "bytes": len(body), "sha256": sha(body), "download": {"url": f"{self.public_url}/v1/media/{token}",
                             "expires_at": now_plus(DOWNLOAD_TTL_SECONDS), "sha256": sha(body), "bytes": len(body), "content_type": "video/mp4"}})
        except (BridgeError, OSError, ValueError):
            return
        finally:
            # Запись уезжает в delivered в любом исходе: retention-guard чистит только
            # delivered, а pending прежде никто не перекладывал — рос без ограничения.
            try:
                record_path.replace(self.storage / "events" / "delivered" / record_path.name)
            except OSError:
                pass


def now_plus(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def camera_path(path: str) -> tuple[str, str]:
    """`/v1/cameras/<id>/<действие>` → (id, действие); иначе — пустые строки."""
    if not path.startswith("/v1/cameras/"):
        return "", ""
    rest = path[len("/v1/cameras/"):]
    if "/" not in rest:
        return urllib.parse.unquote(rest), ""
    camera_id, _, tail = rest.partition("/")
    return urllib.parse.unquote(camera_id), tail.strip("/")


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "cctv-bridge/1", ""
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/v1/cameras": return self.json(200, self.server.bridge.registry())
        if path == "/v1/detector/models": return self.json(200, self.server.bridge.detector_models())
        if path == "/v1/detector/thresholds": return self.json(200, self.server.bridge.detector_thresholds())
        camera_id, tail = camera_path(path)
        if tail == "config":
            try: return self.json(200, self.server.bridge.registry_config(camera_id))
            except BridgeError as exc: return self.json(ERROR_STATUS[exc.code], {"error": exc.code})
        if path.startswith("/v1/discovery/scans/"):
            try: return self.json(200, self.server.bridge.scan_status(path.rsplit("/", 1)[1]))
            except BridgeError as exc: return self.json(ERROR_STATUS[exc.code], {"error": exc.code})
        if path.startswith("/v1/media/"):
            blob = self.server.bridge.take_token(path.rsplit("/", 1)[1])
            if not blob: return self.json(404, {"error": "not_found"})
            self.send_response(200); self.send_header("content-type", blob.content_type); self.send_header("content-length", str(len(blob.body))); self.end_headers(); self.wfile.write(blob.body); return
        self.json(404, {"error": "not_found"})
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        camera_id, tail = camera_path(path)
        known = path in ("/v1/media-requests", "/v1/cameras", "/v1/discovery/scans",
                         "/v1/discovery/probes", "/v1/detector/model", "/v1/detector/threshold",
                         "/v1/detector/calibrate") or tail in ("state", "config", "delete")
        if not known:
            return self.json(404, {"error": "not_found"})
        bridge = self.server.bridge
        try:
            # Границы длины — ДО чтения: иначе заявленные гигабайты читались бы в память.
            length = int(self.headers.get("content-length", "0"))
            if not 0 < length <= MAX_BODY: raise ValueError
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict): raise ValueError
            if tail == "state":
                return self.json(200, bridge.set_override(camera_id, str(request.get("action") or ""),
                                                          request.get("title")))
            if tail == "config":
                return self.json(200, bridge.update_camera(camera_id, request))
            if tail == "delete":
                return self.json(200, bridge.delete_camera(camera_id))
            if path == "/v1/cameras":
                return self.json(200, bridge.add_camera(request))
            if path == "/v1/detector/model":
                return self.json(200, bridge.switch_detector_model(request))
            if path == "/v1/detector/threshold":
                return self.json(200, bridge.set_detector_threshold(request))
            if path == "/v1/detector/calibrate":
                return self.json(200, bridge.calibrate_detector(request))
            if path == "/v1/discovery/scans":
                return self.json(200, bridge.start_scan(request.get("networks")))
            if path == "/v1/discovery/probes":
                # Тело с паролем не логируется нигде: log_message заглушён, а
                # текст ошибки собирается из кодов, а не из запроса.
                return self.json(200, bridge.probe(request.get("host") or "",
                                                   request.get("username") or "",
                                                   request.get("password") or "",
                                                   request.get("detect_url") or ""))
            bridge.accept(request)
        except (ValueError, json.JSONDecodeError): return self.json(400, {"error": "unavailable"})
        except BridgeError as exc: return self.json(ERROR_STATUS[exc.code], {"error": exc.code})
        self.json(202, {"status": "accepted"})
    def json(self, code: int, value: dict):
        data = json.dumps(value, separators=(",", ":")).encode(); self.send_response(code); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, _fmt, *_args): pass


def server_context(env=None) -> ssl.SSLContext | None:
    """mTLS моста включается CCTV_INTERNAL_TLS; без него — открытый http только на loopback."""
    env = os.environ if env is None else env
    if not settings.internal_tls(env):
        bind = env.get("CCTV_BIND", "127.0.0.1")
        if not settings.is_loopback_host(bind):
            raise SystemExit(f"FAIL: без CCTV_INTERNAL_TLS мост слушает только loopback, а не {bind}")
        return None
    missing = [key for key in ("CCTV_SERVER_CERT", "CCTV_SERVER_KEY", "CCTV_CLIENT_CA") if not env.get(key)]
    if missing:
        raise SystemExit("FAIL: CCTV_INTERNAL_TLS=1 требует " + ", ".join(missing))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(env["CCTV_SERVER_CERT"], env["CCTV_SERVER_KEY"])
    context.load_verify_locations(env["CCTV_CLIENT_CA"]); context.verify_mode = ssl.CERT_REQUIRED
    return context


def check_events_url(bridge: Bridge) -> None:
    """Открытый http к боту не на loopback — отказ старта, а не тихо потерянные события."""
    if bridge.events_url:
        try:
            bridge.events_context()
        except ValueError as exc:
            raise SystemExit(f"FAIL: CCTV_EVENTS_URL — {exc}") from exc


def build_server(bridge: Bridge, env=None) -> ThreadingHTTPServer:
    env = os.environ if env is None else env
    context = server_context(env)
    server = ThreadingHTTPServer((env.get("CCTV_BIND", "127.0.0.1"),
                                  int(env.get("CCTV_PORT", str(settings.DEFAULT_BRIDGE_PORT)))), Handler)
    server.daemon_threads = True
    server.bridge = bridge
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main() -> None:
    config_path = pathlib.Path(os.environ.get("CCTV_RUNTIME_CAMERA_CONFIG") or os.environ.get("CCTV_CAMERA_CONFIG") or str(settings.config_dir() / settings.CAMERAS_FILE))
    config = json.loads(config_path.read_text())
    storage = pathlib.Path(os.environ.get("CCTV_STORAGE_ROOT", settings.DEFAULT_STORAGE_ROOT))
    scheme = "https" if settings.internal_tls() else "http"
    public = os.environ.get("CCTV_PUBLIC_URL") or f"{scheme}://127.0.0.1:{settings.DEFAULT_BRIDGE_PORT}"
    bridge = Bridge(config, storage, public)
    check_events_url(bridge)
    server = build_server(bridge)
    server.serve_forever()


if __name__ == "__main__": main()
