#!/usr/bin/env python3
"""Привилегированный писарь реестра камер.

Реестр `/etc/cctv-bridge/cameras.json` принадлежит root и содержит пароли камер.
Мост его только читает — так было задумано, чтобы кнопка в чате не могла править
адреса. Добавление камеры из чата эту границу не отменяет, а переносит: писать
имеет право один маленький процесс с известным протоколом, а не весь мост.

Поэтому здесь:
- единственный вход — unix-сокет `root:cctv 0660`, и клиент проверяется по
  SO_PEERCRED (только root или пользователь моста);
- три команды (`list`, `upsert`, `delete`) и никакого произвольного пути,
  shell и подстановки;
- каждое поле проверяется по белому списку, адреса — только приватные;
- пароль не пишется в журнал ни при какой ошибке;
- цепочка перезапускается ПОСЛЕ ответа клиенту: мост перезапускает сам себя,
  и порядок «ответ, потом рестарт» — единственный, при котором чат узнаёт итог.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import urllib.parse

from .. import i18n, settings

SOCKET_PATH = os.environ.get("CCTV_PROVISION_SOCKET", "/run/cctv/provision.sock")
CONFIG_PATH = pathlib.Path(os.environ.get("CCTV_CAMERA_CONFIG") or settings.config_dir() / settings.CAMERAS_FILE)
BACKUP_DIR = pathlib.Path(os.environ.get("CCTV_PROVISION_BACKUPS") or settings.state_dir() / "registry-backups")
BRIDGE_GROUP = os.environ.get("CCTV_BRIDGE_GROUP", "cctv")
BRIDGE_USER = os.environ.get("CCTV_BRIDGE_USER", "cctv")
RESTART_UNITS = tuple((os.environ.get("CCTV_PROVISION_UNITS")
                       or "cctv-rtsp-proxy.service cctv-bridge.service cctv-pipeline.service").split())
RESTART_DELAY_SEC = float(os.environ.get("CCTV_PROVISION_RESTART_DELAY", "2"))
MAX_BODY = 64 * 1024
MAX_CAMERAS = 32
CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class Invalid(i18n.CodedError, ValueError):
    """Запись не прошла проверку. Ключ каталога (registry.*) безопасен для показа человеку."""

    prefix = "registry"


def mask(url: str | None) -> str:
    return re.sub(r"://[^/@]+@", "://***@", url or "")


def _private_host(url: str, schemes: tuple[str, ...]) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname:
        raise Invalid("bad_scheme", schemes="/".join(schemes), url=mask(url))
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        raise Invalid("host_not_ip") from None
    if not (address.is_private or address.is_loopback):
        raise Invalid("not_private")
    return parts.hostname


def validate(camera: dict) -> dict:
    """Белый список полей: всё лишнее не сохраняется, всё кривое — отказ."""
    if not isinstance(camera, dict):
        raise Invalid("camera_not_object")
    camera_id = str(camera.get("camera_id") or "")
    if not CAMERA_ID_RE.match(camera_id):
        raise Invalid("bad_camera_id")
    title = str(camera.get("title") or camera_id).strip()[:64]
    site = str(camera.get("site") or camera_id).strip()[:64]
    rtsp_url = str(camera.get("rtsp_url") or "")
    host = _private_host(rtsp_url, ("rtsp",))
    entry = {"camera_id": camera_id, "title": title, "site": site, "rtsp_url": rtsp_url}

    detect = camera.get("detect_rtsp_url")
    if detect:
        if _private_host(str(detect), ("rtsp",)) != host:
            raise Invalid("detect_other_camera")
        entry["detect_rtsp_url"] = str(detect)
    snapshot = camera.get("snapshot_url")
    if snapshot:
        if _private_host(str(snapshot), ("http", "https")) != host:
            raise Invalid("snapshot_other_camera")
        entry["snapshot_url"] = str(snapshot)
        for key in ("snapshot_user", "snapshot_password"):
            value = camera.get(key)
            if value:
                entry[key] = str(value)[:128]
    threshold = camera.get("motion_threshold")
    if threshold is not None:
        try:
            value = float(threshold)
        except (TypeError, ValueError):
            raise Invalid("not_number", field="motion_threshold") from None
        if not 0 < value <= 100:
            raise Invalid("out_of_range", field="motion_threshold", range="0..100")
        entry["motion_threshold"] = value
    gate = camera.get("person_gate_threshold")
    if gate is not None:
        try:
            value = float(gate)
        except (TypeError, ValueError):
            raise Invalid("not_number", field="person_gate_threshold") from None
        if not 0 < value <= 100:
            raise Invalid("out_of_range", field="person_gate_threshold", range="0..100")
        entry["person_gate_threshold"] = value
    aspect = camera.get("snapshot_aspect")
    if aspect is not None:
        try:
            value = float(aspect)
        except (TypeError, ValueError):
            raise Invalid("not_number", field="snapshot_aspect") from None
        if not 0.2 <= value <= 5:
            raise Invalid("out_of_range", field="snapshot_aspect", range="0.2..5")
        entry["snapshot_aspect"] = value
    if camera.get("person_detection") is True:
        entry["person_detection"] = True
    return entry


def load(path: pathlib.Path | None = None) -> dict:
    path = path or CONFIG_PATH
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"cameras": []}
    if not isinstance(data, dict) or not isinstance(data.get("cameras"), list):
        raise Invalid("registry_corrupt")
    return data


def save(data: dict, path: pathlib.Path | None = None,
         backups: pathlib.Path | None = None) -> None:
    """Атомарная запись с сохранением владельца и прав: реестр читает мост."""
    path, backups = path or CONFIG_PATH, backups or BACKUP_DIR
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".tmp")
    try:
        stat = path.stat()
        mode, uid, gid = stat.st_mode & 0o7777, stat.st_uid, stat.st_gid
    except FileNotFoundError:
        mode, uid, gid = 0o640, 0, _group_id(BRIDGE_GROUP)
    if path.exists():
        backups.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backups / f"cameras.{stamp}.json"
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    tmp.write_text(body)
    os.chmod(tmp, mode)
    try:
        os.chown(tmp, uid, gid)
    except PermissionError:  # тесты гоняют писаря без root — это не повод падать
        pass
    tmp.replace(path)


def _group_id(name: str) -> int:
    try:
        import grp

        return grp.getgrnam(name).gr_gid
    except (ImportError, KeyError):
        return 0


def summary(data: dict) -> list[dict]:
    """Реестр наружу — без паролей: только адреса с затёртым userinfo."""
    result = []
    for camera in data.get("cameras", []):
        parts = urllib.parse.urlsplit(str(camera.get("rtsp_url") or ""))
        result.append({
            "camera_id": camera.get("camera_id"),
            "title": camera.get("title"),
            "site": camera.get("site"),
            "host": parts.hostname or "",
            "username": urllib.parse.unquote(parts.username or ""),
            "rtsp_url": mask(camera.get("rtsp_url")),
            "detect_rtsp_url": mask(camera.get("detect_rtsp_url")),
            "snapshot_url": camera.get("snapshot_url") or "",
            "person_detection": camera.get("person_detection") is True,
            "motion_threshold": camera.get("motion_threshold"),
            "person_gate_threshold": camera.get("person_gate_threshold"),
            "snapshot_aspect": camera.get("snapshot_aspect"),
        })
    return result


def apply(command: dict, *, path: pathlib.Path | None = None,
          backups: pathlib.Path | None = None) -> tuple[dict, bool]:
    """Выполнить команду над реестром. Второе значение — нужен ли перезапуск.

    Пути берутся при вызове, а не при импорте: значения по умолчанию, снятые в
    момент загрузки модуля, невозможно подменить ни в тесте, ни при переносе.
    """
    path, backups = path or CONFIG_PATH, backups or BACKUP_DIR
    name = command.get("command")
    data = load(path)
    cameras = data["cameras"]
    if name == "list":
        return {"ok": True, "cameras": summary(data)}, False
    if name == "upsert":
        entry = validate(command.get("camera") or {})
        index = next((i for i, c in enumerate(cameras)
                      if c.get("camera_id") == entry["camera_id"]), None)
        if index is None:
            if len(cameras) >= MAX_CAMERAS:
                raise Invalid("registry_full", limit=MAX_CAMERAS)
            cameras.append(entry)
            action = "added"
        else:
            # Правка сохраняет незаданные поля прежней записи: смена пароля не
            # должна молча сбрасывать порог движения и детекцию людей.
            merged = dict(cameras[index])
            merged.update(entry)
            if command.get("replace") is True:
                merged = entry
            cameras[index] = validate(merged)
            action = "updated"
        save(data, path, backups)
        return {"ok": True, "action": action, "camera_id": entry["camera_id"]}, True
    if name == "delete":
        camera_id = str(command.get("camera_id") or "")
        if not CAMERA_ID_RE.match(camera_id):
            raise Invalid("bad_camera_id")
        kept = [c for c in cameras if c.get("camera_id") != camera_id]
        if len(kept) == len(cameras):
            return {"ok": False, "error": "not_found"}, False
        data["cameras"] = kept
        save(data, path, backups)
        return {"ok": True, "action": "deleted", "camera_id": camera_id}, True
    raise Invalid("unknown_command", name=repr(name))


def restart_chain(units=RESTART_UNITS, *, runner=subprocess.run, delay: float = RESTART_DELAY_SEC,
                  log=print) -> None:
    """Перезапуск цепочки отложен: мост должен успеть ответить в чат."""
    def worker() -> None:
        import time

        time.sleep(delay)
        try:
            done = runner(["/usr/bin/systemctl", "restart", *units], capture_output=True,
                          timeout=120)
        except Exception as exc:  # цепочку поднимет Restart=on-failure, но сказать надо
            log(f"перезапуск не выполнен: {type(exc).__name__}")
            return
        if getattr(done, "returncode", 0) != 0:
            log(f"перезапуск вернул код {done.returncode}")

    threading.Thread(target=worker, daemon=True).start()


def peer_allowed(conn: socket.socket) -> bool:
    """Только root и пользователь моста: сокет в /run виден и другим службам."""
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
    except OSError:
        return False
    if uid == 0:
        return True
    try:
        import pwd

        return uid == pwd.getpwnam(BRIDGE_USER).pw_uid
    except (ImportError, KeyError):
        return False


def handle(conn: socket.socket, log=print) -> None:
    with conn:
        if not peer_allowed(conn):
            log("отказ: клиент сокета не мост")
            return
        conn.settimeout(10)
        chunks, size = [], 0
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BODY:
                return
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        try:
            command = json.loads(b"".join(chunks).split(b"\n", 1)[0] or b"{}")
            if not isinstance(command, dict):
                raise Invalid("command_not_object")
            reply, restart = apply(command)
        except Invalid as exc:
            reply, restart = exc.reply(), False
        except Exception as exc:  # содержимое команды в журнал не попадает
            reply, restart = Invalid("writer_failed", reason=type(exc).__name__).reply(), False
            log(f"писарь: {type(exc).__name__}")
        try:
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        except OSError:
            restart = False
        if reply.get("ok") and reply.get("action"):
            log(f"реестр: {reply['action']} {reply.get('camera_id')}")
        if restart:
            restart_chain(log=log)


def serve(path: str = SOCKET_PATH, log=print) -> None:
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o660)
    try:
        os.chown(path, 0, _group_id(BRIDGE_GROUP))
    except PermissionError:
        pass
    server.listen(8)
    log(f"писарь реестра слушает {path}")
    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn, log), daemon=True).start()


def main() -> int:
    def log(message: str) -> None:
        print(message, flush=True)

    serve(SOCKET_PATH, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
