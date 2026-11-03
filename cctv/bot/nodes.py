"""/nodes — панель узлов: бот показывает то, что пришлёт супервизор, и только.

Бот не знает ни узлов, ни ролей, ни соседей. Супервизор контейнера, если у
установки несколько узлов, передаёт процессу бота адрес панели на loopback
(CCTV_NODES_URL) и ключ к ней (CCTV_NODES_TOKEN). Панель отвечает готовым
текстом и кнопками; нажатие кнопки уходит обратно её строкой. Адреса нет —
панели нет: /nodes отвечает, что узел у установки один.
"""
from __future__ import annotations

import httpx

PREFIX = "nd:"
TIMEOUT_SEC = 15


class NodesPanel:
    def __init__(self, url: str = "", token: str = "", *, transport=None) -> None:
        self.url, self.token = (url or "").rstrip("/"), token or ""
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    async def _request(self, method: str, payload: dict | None = None, lang: str = "") -> dict | None:
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SEC, transport=self.transport) as client:
                if method == "GET":
                    reply = await client.get(f"{self.url}/v1/panel", params={"lang": lang}, headers=headers)
                else:
                    reply = await client.post(f"{self.url}/v1/panel", json=payload, headers=headers)
            reply.raise_for_status()
            data = reply.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None

    async def view(self, lang: str) -> dict | None:
        return await self._request("GET", lang=lang)

    async def press(self, data: str, lang: str) -> dict | None:
        return await self._request("POST", {"press": data[len(PREFIX):] if data.startswith(PREFIX) else data,
                                            "lang": lang})


def markup(view: dict):
    """Кнопки панели → InlineKeyboardMarkup; данные кнопки — с префиксом бота."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for row in view.get("buttons") or []:
        buttons = [InlineKeyboardButton(str(item.get("text") or "")[:64],
                                        callback_data=(PREFIX + str(item.get("data") or ""))[:64])
                   for item in row if isinstance(item, dict) and item.get("data")]
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(rows) if rows else None
