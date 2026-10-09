"""Проверка новой версии: раз в сутки последний релиз из GitHub Releases.

Единственный исходящий запрос бота не к Telegram (решение владельца 09.10.2026:
по умолчанию включён, выключатель — CCTV_UPDATE_CHECK=0). Наружу уходит только
GET без параметров; ни версия установки, ни что-либо о камерах не передаются.
Ответ — тег релиза; его бот показывает на карте и в /version строкой
«Доступна X — dozorcam update». Сбой сети — не новость: молча ждём следующих суток.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from .. import __version__

DEFAULT_URL = "https://api.github.com/repos/egorchenkov/dozorcam/releases/latest"
CHECK_EVERY_SEC = 24 * 3600
# После сбоя повтор раньше суток, но не чаще: GitHub без токена — 60 запросов в час на IP.
RETRY_AFTER_FAIL_SEC = 3600
FETCH_TIMEOUT_SEC = 10
LATEST_KEY = "update_latest"
CHECKED_KEY = "update_checked_at"
TRIED_KEY = "update_tried_at"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse(version: str | None) -> tuple[int, int, int] | None:
    """«0.3.0» или «v0.3.0» → (0, 3, 0); пререлизы и мусор — None (не предлагаем)."""
    match = VERSION_RE.match((version or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def newer(candidate: str | None, current: str = __version__) -> bool:
    found, mine = parse(candidate), parse(current)
    return found is not None and mine is not None and found > mine


def fetch_latest(url: str = DEFAULT_URL, *, timeout: float = FETCH_TIMEOUT_SEC) -> str:
    """Тег последнего релиза без «v». Исключение — сбой (вызывающий его глотает)."""
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "dozorcam-update-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(256 * 1024))
    tag = str(payload.get("tag_name") or "").strip()
    if parse(tag) is None:
        raise ValueError("release tag is not a version")
    return tag.lstrip("v")


class UpdateChecker:
    """Состояние проверки — в service-таблице бота: переживает перезапуск, и
    перезапуски чаще суток не превращаются в запросы чаще суток."""

    def __init__(self, state, *, enabled: bool, url: str = DEFAULT_URL, fetch=fetch_latest,
                 now=time.time, current: str = __version__) -> None:
        self.state = state
        self.enabled = enabled
        self.url = url or DEFAULT_URL
        self.fetch = fetch
        self.now = now
        self.current = current

    def _stamp(self, key: str) -> float:
        try:
            return float(self.state.get_service(key) or 0)
        except ValueError:
            return 0.0

    def due(self) -> bool:
        if not self.enabled:
            return False
        now = self.now()
        if now - self._stamp(CHECKED_KEY) < CHECK_EVERY_SEC:
            return False
        return now - self._stamp(TRIED_KEY) >= RETRY_AFTER_FAIL_SEC

    def check(self) -> bool:
        """Спросить GitHub, если пора. True — узнали новую версию, которой раньше не знали."""
        if not self.due():
            return False
        before = self.available()
        self.state.set_service(TRIED_KEY, str(self.now()))
        try:
            latest = self.fetch(self.url)
        except Exception:  # сеть, лимит, мусор — следующая попытка через час
            return False
        self.state.set_service(LATEST_KEY, latest)
        self.state.set_service(CHECKED_KEY, str(self.now()))
        return self.available() is not None and self.available() != before

    def latest(self) -> str | None:
        return self.state.get_service(LATEST_KEY) if self.enabled else None

    def available(self) -> str | None:
        """Версия новее установленной, если проверка включена и её знает."""
        latest = self.latest()
        return latest if newer(latest, self.current) else None

    def checked_at(self) -> float | None:
        stamp = self._stamp(CHECKED_KEY)
        return stamp or None
