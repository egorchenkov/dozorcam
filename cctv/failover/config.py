"""Раздел ``[failover]`` каталога конфига — единственный источник настроек слоя.

Каталог конфига один на все узлы, поэтому всё, что слой знает о соседях, лежит
здесь (config.toml и secrets.toml). Из окружения узла берётся только его имя —
CCTV_NODE. Раздела нет — слоя нет: роль всегда активна, как в одноузловой
установке.

    [failover]
    check_sec = 60                     # проверка пригодности узлов
    [[failover.nodes]]
    name = "dacha"
    priority = 1                       # меньше — главнее; он активен с первого старта
    address = "192.0.2.10"             # агенты: http://<address>:8782 (engine), :8783 (bot)
    # engine_agent = "https://192.0.2.10:8782"   # или адреса агентов явно
    # bot_agent = "https://192.0.2.10:8783"
    # bridge_url = "https://192.0.2.10:8780"     # мост этого узла — для бота другого узла
    # events_url = "https://192.0.2.10:8781/v1/events"  # приёмник бота — для движка

secrets.toml: ``[failover] secret = "…"`` — общий ключ подписи запросов между агентами.
Адрес агента без TLS допускается только на loopback (два экземпляра на одной машине);
между машинами — https с сертификатами ``tls_cert``/``tls_key``/``tls_ca``.
"""
from __future__ import annotations

import ipaddress
import re
import urllib.parse
from dataclasses import dataclass
from typing import MutableMapping

from .. import i18n, settings

ROLES = ("engine", "bot")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
AGENT_PORTS = {"engine": 8782, "bot": 8783}
DEFAULT_CHECK_SEC = 60
SECRET_MIN_LEN = 16
SECTION_KEYS = ("nodes", "check_sec", "tls_cert", "tls_key", "tls_ca")
NODE_KEYS = ("name", "priority", "address", "engine_agent", "bot_agent", "bridge_url", "events_url")
SECRET_KEYS = ("secret",)


class FailoverError(i18n.CodedError, RuntimeError):
    """Раздел [failover] противоречив — роль не запускается (ключи failover.*)."""

    prefix = "failover"


@dataclass(frozen=True)
class Node:
    name: str
    priority: int
    engine_agent: str
    bot_agent: str
    bridge_url: str = ""
    events_url: str = ""

    def agent(self, role: str) -> str:
        return self.engine_agent if role == "engine" else self.bot_agent


@dataclass(frozen=True)
class Failover:
    me: str
    nodes: tuple[Node, ...]
    secret: bytes
    check_sec: int = DEFAULT_CHECK_SEC
    tls_cert: str = ""
    tls_key: str = ""
    tls_ca: str = ""

    def node(self, name: str) -> Node | None:
        return next((node for node in self.nodes if node.name == name), None)

    @property
    def ordered(self) -> tuple[Node, ...]:
        """Узлы по приоритету: меньше число — главнее, при равенстве — по имени."""
        return tuple(sorted(self.nodes, key=lambda node: (node.priority, node.name)))

    @property
    def top(self) -> str:
        return self.ordered[0].name

    def rank(self, name: str) -> int:
        names = [node.name for node in self.ordered]
        return names.index(name) if name in names else len(names)

    @property
    def tls(self) -> bool:
        return bool(self.tls_cert)


def _url(value, key: str, *, need_port: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FailoverError("bad_value", key=key)
    url = value.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme not in ("http", "https") or not parts.hostname or (need_port and port is None):
        raise FailoverError("bad_url", key=key, url=url)
    return url


def _loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _node(raw, index: int, scheme: str) -> Node:
    where = f"failover.nodes[{index}]"
    if not isinstance(raw, dict):
        raise FailoverError("bad_value", key=where)
    unknown = sorted(set(raw) - set(NODE_KEYS))
    if unknown:
        raise FailoverError("unknown_key", key=f"{where}.{unknown[0]}")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise FailoverError("bad_name", value=repr(name))
    priority = raw.get("priority", index + 1)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise FailoverError("bad_value", key=f"{where}.priority")
    agents = {}
    address = raw.get("address")
    for role in ROLES:
        explicit = raw.get(f"{role}_agent")
        if explicit is not None:
            agents[role] = _url(explicit, f"{where}.{role}_agent", need_port=True)
        elif isinstance(address, str) and address.strip():
            host = address.strip()
            host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            agents[role] = f"{scheme}://{host}:{AGENT_PORTS[role]}"
        else:
            raise FailoverError("no_agent", node=name, role=role)
    links = {key: _url(raw[key], f"{where}.{key}", need_port=False) if raw.get(key) else ""
             for key in ("bridge_url", "events_url")}
    return Node(name, priority, agents["engine"], agents["bot"], **links)


def load(env: MutableMapping[str, str] | None = None) -> Failover | None:
    """Раздел [failover] каталога конфига; None — слой не настроен (один узел)."""
    import os

    env = os.environ if env is None else env
    root = settings.config_dir(env)
    section = settings._read_toml(root / settings.CONFIG_FILE).get("failover")
    if not section:
        return None
    if not isinstance(section, dict):
        raise FailoverError("bad_value", key="failover")
    unknown = sorted(set(section) - set(SECTION_KEYS))
    if unknown:
        raise FailoverError("unknown_key", key=f"failover.{unknown[0]}")
    tls = {key: str(section.get(key) or "").strip() for key in ("tls_cert", "tls_key", "tls_ca")}
    if any(tls.values()) and not all(tls.values()):
        raise FailoverError("tls_triplet")
    raw_nodes = section.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise FailoverError("no_nodes")
    scheme = "https" if tls["tls_cert"] else "http"
    nodes = tuple(_node(raw, index, scheme) for index, raw in enumerate(raw_nodes))
    names = [node.name for node in nodes]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate:
        raise FailoverError("duplicate", name=duplicate)
    agents = [node.agent(role) for node in nodes for role in ROLES]
    if len(set(agents)) != len(agents):
        raise FailoverError("agent_clash")
    for url in agents:
        # Без TLS — только loopback (два экземпляра на одной машине), как у связи мост↔бот.
        if url.startswith("http://") and not _loopback(url):
            raise FailoverError("needs_tls", url=url)
        if url.startswith("https://") and not tls["tls_cert"]:
            raise FailoverError("tls_triplet")

    check_sec = section.get("check_sec", DEFAULT_CHECK_SEC)
    if not isinstance(check_sec, int) or isinstance(check_sec, bool) or not 5 <= check_sec <= 3600:
        raise FailoverError("bad_value", key="failover.check_sec")

    secret_section = settings._read_toml(root / settings.SECRETS_FILE).get("failover") or {}
    unknown = sorted(set(secret_section) - set(SECRET_KEYS)) if isinstance(secret_section, dict) else []
    if unknown:
        raise FailoverError("unknown_key", key=f"secrets.failover.{unknown[0]}")
    secret = str(secret_section.get("secret") or "") if isinstance(secret_section, dict) else ""
    if len(secret) < SECRET_MIN_LEN:
        raise FailoverError("no_secret", length=SECRET_MIN_LEN)

    me = (env.get("CCTV_NODE") or "").strip()
    if not me:
        raise FailoverError("no_node")
    if me not in names:
        raise FailoverError("unknown_node", name=me, names=", ".join(names))
    return Failover(me=me, nodes=nodes, secret=secret.encode(), check_sec=check_sec, **tls)
