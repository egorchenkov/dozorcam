#!/usr/bin/env python3
"""Клиент CCTV Bridge API v1: единственный исходящий путь бота кроме Telegram.

Bridge не принимает произвольные URL, команды и пути. Поэтому здесь нет
«скачай что дадут»: media URL проверяется на схему и хост Bridge, поток режется
по лимиту размера, SHA-256 сверяется до отправки в Telegram, временный файл
удаляется в любом исходе.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import tempfile
import urllib.parse
from dataclasses import dataclass

import httpx

KNOWN_ERRORS = ("camera_offline", "not_found", "unavailable", "timeout", "media_too_large",
                "clip_window_empty", "storage_capacity")
CHUNK = 64 * 1024


class BridgeError(RuntimeError):
    """Ошибка с кодом контракта; текст пользователю — только известный код."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code if code in KNOWN_ERRORS else "unavailable"


class MediaRejected(BridgeError):
    """Медиа не прошло проверку целостности или лимита — публиковать нельзя."""


@dataclass(frozen=True)
class Camera:
    camera_id: str
    title: str
    site: str
    status: str
    last_frame_at: str | None
    # Живость детектора приходит из реестра Bridge: сам бот кадры не смотрит.
    motion_state: str = "unknown"
    motion_reason: str = ""
    last_motion_at: str | None = None


@dataclass(frozen=True)
class Storage:
    """Диск моста — транзит: архив живёт в темах Telegram, здесь только буфер."""
    used_bytes: int
    budget_bytes: int
    free_bytes: int
    # Порог движка (CCTV_MIN_FREE_BYTES); 0 — старый мост без поля.
    min_free_bytes: int = 0

    @property
    def over_budget(self) -> bool:
        return self.budget_bytes > 0 and self.used_bytes > self.budget_bytes


@dataclass(frozen=True)
class Downloaded:
    path: str
    size: int
    sha256: str
    content_type: str


def build_ssl_context(cfg) -> ssl.SSLContext | bool:
    """Контекст клиента mTLS (только с CCTV_INTERNAL_TLS; иначе loopback http — True).

    Контекст клиента mTLS: доверие CA Bridge плюс собственный сертификат.

    Собирается вручную, а не параметрами httpx, по конкретной причине: в
    httpx 0.28 ветка `verify=<путь>` возвращает контекст сразу, до применения
    `cert=...`, и клиентский сертификат молча теряется. Соединение при этом не
    падает на ровном месте — оно просто перестаёт быть взаимно
    аутентифицированным, что заметно только со стороны Bridge.
    """
    if not getattr(cfg, "internal_tls", True):
        return True  # http://127.0.0.1 — TLS нет, проверять нечего
    context = ssl.create_default_context(cafile=str(cfg.bridge_ca_bundle))
    context.load_cert_chain(str(cfg.bridge_client_cert), str(cfg.bridge_client_key))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class Bridge:
    def __init__(self, cfg, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._own_client = client is None
        self.client = client or httpx.Client(
            base_url=cfg.bridge_base_url,
            verify=build_ssl_context(cfg),
            timeout=cfg.bridge_timeout_sec,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    # --- registry ---------------------------------------------------------
    def cameras(self) -> list[Camera]:
        return self.registry()[0]

    def registry(self) -> tuple[list[Camera], Storage | None]:
        """Камеры и состояние диска одним запросом: сторож опрашивает их вместе."""
        payload = self._json("GET", "/v1/cameras")
        cameras = []
        for item in payload.get("cameras", []):
            camera_id = str(item.get("camera_id", ""))
            motion = item.get("motion") if isinstance(item.get("motion"), dict) else {}
            cameras.append(Camera(
                camera_id=camera_id,
                title=str(item.get("title") or camera_id),
                site=str(item.get("site") or ""),
                status=str(item.get("status") or "unknown"),
                last_frame_at=item.get("last_frame_at"),
                motion_state=str(motion.get("state") or "unknown"),
                motion_reason=str(motion.get("reason") or ""),
                last_motion_at=motion.get("last_motion_at"),
            ))
        raw = payload.get("storage")
        storage = None
        if isinstance(raw, dict):
            try:
                storage = Storage(int(raw.get("used_bytes", 0)), int(raw.get("budget_bytes", 0)),
                                  int(raw.get("free_bytes", 0)), int(raw.get("min_free_bytes") or 0))
            except (TypeError, ValueError):
                storage = None  # старый мост без поля — сторож просто молчит о диске
        return cameras, storage

    # --- запрос медиа -----------------------------------------------------
    def request_media(self, request_id: str, camera_id: str, kind: str,
                      requested_at: str, center_at: str | None = None) -> str:
        body = {
            "request_id": request_id,
            "camera_id": camera_id,
            "kind": kind,
            "requested_at": requested_at,
        }
        if kind == "clip":
            body["center_at"] = center_at or requested_at
            body["duration_sec"] = self.cfg.clip_duration_sec
        payload = self._json("POST", "/v1/media-requests", json=body)
        return str(payload.get("status") or "accepted")

    # --- управление камерой -----------------------------------------------
    def set_camera_state(self, camera_id: str, action: str, title: str | None = None) -> None:
        """Пауза/возобновление/переименование/снятие. Адреса и пароли камеры
        этим путём не меняются: реестр моста только на чтение."""
        body: dict = {"action": action}
        if title is not None:
            body["title"] = title
        self._json("POST", f"/v1/cameras/{urllib.parse.quote(camera_id)}/state", json=body)

    # --- заведение и правка камер ------------------------------------------
    # Эти вызовы — единственное место, где через бота проходит пароль камеры.
    # Он идёт транзитом в тело одного POST и нигде не сохраняется: ни в БД
    # бота, ни в журнале, ни в тексте ответа. Обратно приходит только токен.
    def scan_start(self, networks: str | None = None) -> dict:
        body = {"networks": networks} if networks else {}
        return self._json("POST", "/v1/discovery/scans", json=body)

    def scan_status(self, scan_id: str) -> dict:
        return self._json("GET", f"/v1/discovery/scans/{urllib.parse.quote(scan_id)}")

    def probe(self, host: str, username: str, password: str, detect_url: str = "") -> dict:
        body = {"host": host, "username": username, "password": password}
        if detect_url:
            body["detect_url"] = detect_url
        return self._json("POST", "/v1/discovery/probes", json=body)

    def add_camera(self, camera_id: str, title: str, site: str, probe_token: str) -> dict:
        return self._json("POST", "/v1/cameras",
                          json={"camera_id": camera_id, "title": title, "site": site,
                                "probe_token": probe_token})

    def update_camera(self, camera_id: str, **fields) -> dict:
        return self._json("POST", f"/v1/cameras/{urllib.parse.quote(camera_id)}/config",
                          json=fields)

    def delete_camera(self, camera_id: str) -> dict:
        return self._json("POST", f"/v1/cameras/{urllib.parse.quote(camera_id)}/delete",
                          json={"confirm": True})

    def camera_config(self, camera_id: str) -> dict:
        """Запись реестра без пароля: адреса приходят с затёртым userinfo."""
        payload = self._json("GET", f"/v1/cameras/{urllib.parse.quote(camera_id)}/config")
        camera = payload.get("camera")
        return camera if isinstance(camera, dict) else {}

    # --- модель детектора людей -------------------------------------------
    def detector_models(self) -> dict:
        """Семейства, найденные файлы моделей, активная модель и исход последней смены."""
        return self._json("GET", "/v1/detector/models")

    def switch_detector_model(self, family: str, model: str) -> str:
        """Заявка на смену; перезагрузку и откат делает движок. Ответ — request_id."""
        payload = self._json("POST", "/v1/detector/model", json={"family": family, "model": model})
        return str(payload.get("request_id") or "")

    def detector_thresholds(self) -> dict:
        """Итоговый порог по каждой камере с YOLO и состояние автокалибровки."""
        return self._json("GET", "/v1/detector/thresholds")

    def set_detector_threshold(self, camera_id: str, value: float | None) -> dict:
        """Ручной порог камеры; None — снять, вернуть автокалибровку."""
        return self._json("POST", "/v1/detector/threshold", json={"camera_id": camera_id, "value": value})

    def calibrate_detector(self, camera_id: str | None = None) -> dict:
        """«Откалибровать»: все камеры с YOLO или одну."""
        return self._json("POST", "/v1/detector/calibrate", json={"camera_id": camera_id or ""})

    # --- загрузка медиа ---------------------------------------------------
    def is_bridge_url(self, url: str) -> bool:
        """Ссылка обязана вести на тот же хост, порт и схему Bridge, что и в конфиге."""
        try:
            got, want = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.cfg.bridge_base_url)
        except ValueError:
            return False
        return (got.scheme == want.scheme and got.scheme in ("https", "http")
                and (got.hostname, got.port) == (want.hostname, want.port))

    def download(self, url: str, *, kind: str, expected_sha256: str,
                 declared_bytes: int | None = None) -> Downloaded:
        if not self.is_bridge_url(url):
            raise MediaRejected("unavailable", "media URL не принадлежит Bridge")
        limit = self.cfg.max_bytes_for(kind)
        if declared_bytes is not None and declared_bytes > limit:
            raise MediaRejected("media_too_large", f"{declared_bytes} > {limit}")

        digest = hashlib.sha256()
        size = 0
        # Расширение — часть контракта с Telegram: по имени файла выводится mime,
        # и с «.bin» Android показывал клип неизвестным файлом (23.09.2026).
        suffix = ".mp4" if kind == "clip" else ".jpg"
        fd, path = tempfile.mkstemp(dir=str(self.cfg.runtime_dir), prefix="cctv-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as handle, self.client.stream("GET", url) as response:
                if response.status_code != 200:
                    # В стриме тело ещё не прочитано: без read() любой .json() бросает
                    # ResponseNotRead и настоящий код ошибки маскировался в unavailable.
                    response.read()
                    code = self._error_code(response)
                    # 404 на выдаче медиа — истёкшая/чужая ссылка, а не пропавшая камера:
                    # текст «камера не найдена в реестре» здесь вводил бы в заблуждение.
                    raise BridgeError("unavailable" if code == "not_found" else code,
                                      f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "application/octet-stream")
                for chunk in response.iter_bytes(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise MediaRejected("media_too_large", f"поток превысил {limit} байт")
                    digest.update(chunk)
                    handle.write(chunk)
            actual = digest.hexdigest()
            if expected_sha256 and actual.lower() != expected_sha256.lower():
                raise MediaRejected("unavailable", "sha256 не совпал")
            if declared_bytes is not None and size != declared_bytes:
                raise MediaRejected("unavailable", "объявленный размер не совпал")
        except BaseException:
            _unlink(path)
            raise
        return Downloaded(path=path, size=size, sha256=digest.hexdigest(),
                          content_type=content_type.split(";")[0].strip())

    # --- внутреннее -------------------------------------------------------
    def _json(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise BridgeError("timeout", str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BridgeError("unavailable", type(exc).__name__) from exc
        if response.status_code >= 400:
            raise BridgeError(self._error_code(response), f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BridgeError("unavailable", "ответ Bridge не JSON") from exc
        if not isinstance(payload, dict):
            raise BridgeError("unavailable", "ответ Bridge не объект")
        return payload

    @staticmethod
    def _error_code(response) -> str:
        try:
            code = str((response.json() or {}).get("error", ""))
        except Exception:  # тело ошибки может быть чем угодно — это не повод падать
            code = ""
        return code if code in KNOWN_ERRORS else "unavailable"


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
