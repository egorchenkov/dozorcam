#!/usr/bin/env python3
"""Площадки: несколько движков за одним ботом, у каждого свои камеры.

Площадка — набор камер со своим движком (мостом). Бот говорит с мостом каждой
площадки тем же клиентом Bridge и сводит их в одну группу: реестр — общий
список камер, а запрос медиа, пауза, правка и порог камеры уходят мосту её
площадки. Темы по-прежнему по camera_id, поэтому камера, переехавшая на
другую площадку, публикует события в прежнюю тему. Движки о площадках и друг
о друге не знают: каждый отдаёт свой реестр и шлёт события в приёмник бота.

`Sites` повторяет интерфейс `Bridge`: ядру бота всё равно, одна площадка или
несколько. Без CCTV_SITES точка входа берёт прежний `Bridge`, и одномоторная
установка не меняется ни в чём.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import threading

from .bridge import Bridge, BridgeError, Camera, MediaRejected, Storage

# Камеры недоступной площадки остаются в списке с этим статусом: сторож скажет
# «нет кадров» в их темах, а сверка реестра их не снимет.
UNREACHABLE = "unreachable"


def connect(cfg, log=lambda _m: None):
    """Клиент мостов для ядра бота: площадки из CCTV_SITES, без них — прежний один Bridge."""
    return Sites(cfg, log=log) if cfg.sites else Bridge(cfg)


class Sites:
    def __init__(self, cfg, bridges: dict[str, Bridge] | None = None, log=lambda _m: None) -> None:
        self.cfg = cfg
        self.log = log
        if bridges is None:
            bridges = {site.name: Bridge(dataclasses.replace(cfg, bridge_base_url=site.bridge_url))
                       for site in cfg.sites}
        self.bridges = dict(bridges)
        self.names = tuple(self.bridges)
        # Куда заводится новая камера: выбор в /add; по умолчанию — первая площадка.
        self.add_site = self.names[0]
        self._lock = threading.Lock()
        self._owner: dict[str, str] = {}
        self._last: dict[str, list[Camera]] = {}
        # Заявки моста (поиск, активация, проба) живут на мосту, который их принял.
        self._issued: dict[str, str] = {}

    @property
    def multi(self) -> bool:
        return len(self.names) > 1

    def close(self) -> None:
        for bridge in self.bridges.values():
            bridge.close()

    # --- реестр -----------------------------------------------------------
    def cameras(self) -> list[Camera]:
        return self.registry()[0]

    def registry(self) -> tuple[list[Camera], Storage | None]:
        """Камеры всех площадок; молчащая площадка — её прежние камеры как недоступные.

        Ошибка — только если молчат все: тогда это та же «мост не отвечает», что
        и в одномоторной установке. Камера с тем же id на второй площадке не
        принимается — её события ушли бы в чужую тему.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.names)) as pool:
            answers = dict(zip(self.names, pool.map(self._registry_of, self.names)))
        errors = [answer for answer in answers.values() if isinstance(answer, BridgeError)]
        if len(errors) == len(self.names):
            raise errors[0]
        cameras: list[Camera] = []
        owner: dict[str, str] = {}
        storages: list[Storage] = []
        for name, answer in answers.items():
            if isinstance(answer, BridgeError):
                with self._lock:
                    found = [dataclasses.replace(camera, status=UNREACHABLE) for camera in self._last.get(name, [])]
            else:
                found, storage = answer
                with self._lock:
                    self._last[name] = list(found)
                if storage is not None:
                    storages.append(storage)
            for camera in found:
                if camera.camera_id in owner:
                    if owner[camera.camera_id] != name:
                        self.log(f"площадки: камера {camera.camera_id} есть и на {owner[camera.camera_id]}, "
                                 f"и на {name} — обслуживается первой")
                    continue
                owner[camera.camera_id] = name
                if self.multi and not camera.site:
                    # Камера без своей локации — в разделе карты своей площадки.
                    camera = dataclasses.replace(camera, site=name)
                cameras.append(camera)
        with self._lock:
            self._owner = owner
        return cameras, _tightest(storages)

    def _registry_of(self, name: str):
        try:
            return self.bridges[name].registry()
        except BridgeError as exc:
            self.log(f"площадка {name}: реестр недоступен ({exc.code})")
            return exc

    def site_of(self, camera_id: str) -> str | None:
        with self._lock:
            return self._owner.get(camera_id)

    def for_camera(self, camera_id: str) -> Bridge:
        """Мост площадки камеры; незнакомая камера — после свежего реестра."""
        if not self.multi:
            return self.bridges[self.names[0]]
        name = self.site_of(camera_id)
        if name is None:
            self.registry()
            name = self.site_of(camera_id)
        if name is None:
            raise BridgeError("not_found", camera_id)
        return self.bridges[name]

    def choose(self, name: str) -> bool:
        """Площадка для новых камер (/add). False — такой площадки нет."""
        if name not in self.bridges:
            return False
        self.add_site = name
        return True

    # --- камера: всегда мост её площадки ------------------------------------
    def request_media(self, request_id: str, camera_id: str, kind: str,
                      requested_at: str, center_at: str | None = None) -> str:
        return self.for_camera(camera_id).request_media(request_id, camera_id, kind, requested_at, center_at)

    def set_camera_state(self, camera_id: str, action: str, title: str | None = None) -> None:
        self.for_camera(camera_id).set_camera_state(camera_id, action, title)

    def update_camera(self, camera_id: str, **fields) -> dict:
        return self.for_camera(camera_id).update_camera(camera_id, **fields)

    def delete_camera(self, camera_id: str) -> dict:
        return self.for_camera(camera_id).delete_camera(camera_id)

    def camera_config(self, camera_id: str) -> dict:
        return self.for_camera(camera_id).camera_config(camera_id)

    def set_detector_threshold(self, camera_id: str, value: float | None) -> dict:
        return self.for_camera(camera_id).set_detector_threshold(camera_id, value)

    # --- заведение: площадка, выбранная в /add ------------------------------
    def _remember(self, key: str, reply: dict, name: str) -> dict:
        value = str(reply.get(key) or "")
        if value:
            with self._lock:
                self._issued[value] = name
        return reply

    def _issuer(self, value: str) -> str:
        with self._lock:
            return self._issued.get(value, self.add_site)

    def scan_start(self, networks: str | None = None) -> dict:
        name = self.add_site
        return self._remember("scan_id", self.bridges[name].scan_start(networks), name)

    def scan_status(self, scan_id: str) -> dict:
        return self.bridges[self._issuer(scan_id)].scan_status(scan_id)

    def activation_start(self, hosts: list[str], password: str | None = None,
                         network: dict | None = None) -> dict:
        name = self.add_site
        return self._remember("activation_id", self.bridges[name].activation_start(hosts, password, network), name)

    def activation_status(self, activation_id: str) -> dict:
        name = self._issuer(activation_id)
        status = self.bridges[name].activation_status(activation_id)
        for result in status.get("results") or []:
            if isinstance(result, dict):
                self._remember("probe_token", result, name)
        return status

    def probe(self, host: str, username: str, password: str, detect_url: str = "") -> dict:
        """Проба новой камеры; заведённую бот пробует мостом её площадки (for_camera)."""
        name = self.add_site
        return self._remember("probe_token", self.bridges[name].probe(host, username, password, detect_url), name)

    def add_camera(self, camera_id: str, title: str, site: str, probe_token: str) -> dict:
        # Токен пробы годен только на мосту, который проверял камеру.
        return self.bridges[self._issuer(probe_token)].add_camera(camera_id, title, site, probe_token)

    # --- детектор: одна модель на всю установку -----------------------------
    def detector_models(self) -> dict:
        """Каталог и исход смены — первой площадки: выбор модели общий для всех."""
        return self.bridges[self.names[0]].detector_models()

    def switch_detector_model(self, family: str, model: str) -> str:
        """Смена модели на каждой площадке; ответ — заявка первой (её исход ждёт бот)."""
        first = self.bridges[self.names[0]].switch_detector_model(family, model)
        for name in self.names[1:]:
            try:
                self.bridges[name].switch_detector_model(family, model)
            except BridgeError as exc:
                self.log(f"площадка {name}: смена модели не принята ({exc.code})")
        return first

    def detector_thresholds(self) -> dict:
        """Пороги камер всех площадок одним списком; модель и границы — первой ответившей."""
        parts: list[tuple[str, dict]] = []
        error: BridgeError | None = None
        for name in self.names:
            try:
                parts.append((name, self.bridges[name].detector_thresholds()))
            except BridgeError as exc:
                error = exc
        if not parts:
            raise error or BridgeError("unavailable")
        view = dict(parts[0][1])
        view["cameras"] = [item for name, part in parts for item in part.get("cameras") or []
                           if isinstance(item, dict) and self.site_of(str(item.get("camera_id"))) in (name, None)]
        return view

    def calibrate_detector(self, camera_id: str | None = None) -> dict:
        if camera_id:
            return self.for_camera(camera_id).calibrate_detector(camera_id)
        replies = []
        for name in self.names:
            try:
                replies.append(self.bridges[name].calibrate_detector(None))
            except BridgeError as exc:
                self.log(f"площадка {name}: калибровка не начата ({exc.code})")
        if not replies:
            raise BridgeError("unavailable")
        return {**replies[0], "frames": max(int(reply.get("frames") or 0) for reply in replies)}

    # --- медиа: ссылка ведёт на мост своей площадки -------------------------
    def is_bridge_url(self, url: str) -> bool:
        return any(bridge.is_bridge_url(url) for bridge in self.bridges.values())

    def download(self, url: str, *, kind: str, expected_sha256: str,
                 declared_bytes: int | None = None):
        bridge = next((bridge for bridge in self.bridges.values() if bridge.is_bridge_url(url)), None)
        if bridge is None:
            raise MediaRejected("unavailable", "media URL does not belong to any site")
        return bridge.download(url, kind=kind, expected_sha256=expected_sha256, declared_bytes=declared_bytes)


def _tightest(storages: list[Storage]) -> Storage | None:
    """Диск, о котором сторожу важнее сказать: переполненный, иначе с наименьшим запасом."""
    if not storages:
        return None
    return min(storages, key=lambda s: (not s.over_budget, s.free_bytes - (s.min_free_bytes or 0)))
