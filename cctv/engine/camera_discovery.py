#!/usr/bin/env python3
"""Поиск камер в сети и определение их параметров без участия человека.

Модуль знает три вещи и ничего сверх: какие адреса в разрешённых сетях вообще
похожи на камеру, кто это (производитель/модель), и какие у неё потоки. Пароль
приходит сюда параметром, живёт в памяти вызывающего процесса и не попадает ни
в журнал, ни в возвращаемые тексты: наружу URL уходит с затёртым userinfo.

Основной опрос точечный: TCP-развёртка разрешённых сетей, затем unicast-ONVIF по
найденным адресам — камеры за WireGuard мультикаст не видят (маршрутизируемый туннель
239.255.255.250 на удалённую LAN не переносит). Для домашней установки в одной LAN с
камерами к нему добавлен multicast WS-Discovery (`ws_discover`), а сети без
CCTV_DISCOVERY_NETWORKS берутся с собственных интерфейсов (`local_networks`).
Камеру вне поиска (нестандартный порт, RTSP-сервер без ONVIF) заводят по адресу:
`probe` принимает и `rtsp://…` целиком.
"""
from __future__ import annotations

import base64
import concurrent.futures
import datetime as dt
import hashlib
import ipaddress
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .. import i18n

# 554 — RTSP, остальные — типовые порты веб-интерфейса и ONVIF-службы устройств.
SCAN_PORTS = (554, 80, 8000, 8899, 8080)
ONVIF_PORTS = (80, 8000, 8899, 8080, 5000)
# Где спрашивать у Hikvision состояние активации (без пароля).
ACTIVATION_WEB_PORTS = (80, 8080)
ONVIF_PATHS = ("/onvif/device_service", "/onvif/device", "/onvif/Device", "/onvif/services")
# Развёртка сети — не инструмент разведки: сети только приватные и не крупнее
# /20, иначе одна опечатка в конфиге превращала бы мост в сканер интернета.
MAX_SCAN_HOSTS = 4096
SCAN_TIMEOUT = 1.0
PROBE_TIMEOUT = 6.0

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
WSSE = ("http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-secext-1.0.xsd")
WSU = ("http://docs.oasis-open.org/wss/2004/01/"
       "oasis-200401-wss-wssecurity-utility-1.0.xsd")
PASSWORD_DIGEST_TYPE = ("http://docs.oasis-open.org/wss/2004/01/"
                        "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")

# Шаблоны нужны там, где ONVIF молчит или врёт: у части устройств GetStreamUri
# отдаёт адрес, недоступный снаружи их собственной подсети. Порядок в паре —
# основной поток, затем поток для детектора.
TEMPLATES: dict[str, dict] = {
    "hikvision": {
        "main": "rtsp://{host}:{port}/Streaming/Channels/101",
        "sub": "rtsp://{host}:{port}/Streaming/Channels/102",
        "snapshot": "http://{host}/ISAPI/Streaming/channels/101/picture",
    },
    "dahua": {
        "main": "rtsp://{host}:{port}/cam/realmonitor?channel=1&subtype=0",
        "sub": "rtsp://{host}:{port}/cam/realmonitor?channel=1&subtype=1",
        "snapshot": "http://{host}/cgi-bin/snapshot.cgi",
    },
    "qualvision": {  # Tantos и прочие NVT на этой платформе
        "main": "rtsp://{host}:{port}/stream?mode=real&idc=1&ids=1",
        "sub": "rtsp://{host}:{port}/stream?mode=real&idc=1&ids=2",
        "snapshot": "http://{host}/onvif/Snapshot",
    },
    "generic": {
        "main": "rtsp://{host}:{port}/live/main",
        "sub": "rtsp://{host}:{port}/live/sub",
        "snapshot": None,
    },
}
VENDOR_HINTS = (
    ("hikvision", "hikvision"), ("ds-2", "hikvision"), ("dahua", "dahua"),
    ("tantos", "qualvision"), ("qualvision", "qualvision"), ("nvt", "qualvision"),
    ("hisharp", "generic"), ("axis", "generic"),
)


class DiscoveryError(i18n.CodedError, RuntimeError):
    """Опрос не удался. Ключ каталога (discovery.*) — для показа человеку, без секретов."""

    prefix = "discovery"


@dataclass
class Candidate:
    host: str
    ports: list[int] = field(default_factory=list)
    vendor: str = ""
    model: str = ""
    onvif_url: str = ""
    rtsp_banner: str = ""
    # Без пароля: None — не знаем, False — новая, ждёт первого пароля.
    # activation: "v3"/"legacy" — Hikvision, бот активирует сам; "manual" —
    # марка без автоактивации: камера ждёт настройки или отвечает одним
    # веб-интерфейсом (ни RTSP, ни ONVIF) — бот покажет инструкцию по марке.
    activated: bool | None = None
    activation: str = ""
    brand: str = ""  # марка для инструкции (vendor_setup.BRAND_HINTS)

    def label(self) -> str:
        name = " ".join(p for p in (self.vendor, self.model) if p).strip()
        return f"{self.host} · {name}" if name else self.host

    def as_dict(self) -> dict:
        return {"host": self.host, "ports": sorted(self.ports), "vendor": self.vendor,
                "model": self.model, "onvif": bool(self.onvif_url), "label": self.label(),
                "activated": self.activated, "activation": self.activation,
                "brand": self.brand}


@dataclass
class Profile:
    token: str
    name: str
    encoding: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    stream_uri: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def as_dict(self) -> dict:
        return {"name": self.name, "encoding": self.encoding, "width": self.width,
                "height": self.height, "fps": self.fps}


@dataclass
class Detected:
    """Определённые параметры камеры. `main`/`sub`/`snapshot` — с userinfo."""
    host: str
    vendor: str = ""
    model: str = ""
    serial: str = ""
    source: str = "onvif"
    main_url: str = ""
    sub_url: str = ""
    snapshot_url: str = ""
    profiles: list[Profile] = field(default_factory=list)
    verified: bool = False

    def summary(self) -> dict:
        """Показ человеку: пароль стёрт, оставлены только сведения о потоках."""
        return {"host": self.host, "vendor": self.vendor, "model": self.model,
                "source": self.source, "verified": self.verified,
                "main_url": mask(self.main_url), "sub_url": mask(self.sub_url),
                "snapshot_url": mask(self.snapshot_url),
                "profiles": [p.as_dict() for p in self.profiles]}


def mask(url: str | None) -> str:
    """Затереть userinfo: в чат и журнал URL уходит без пароля."""
    if not url:
        return ""
    return re.sub(r"://[^/@]+@", "://***@", url)


TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slugify(title: str) -> str:
    """camera_id из имени: латиница, цифры, дефис. Пусто — вызывающий подставит своё."""
    table = TRANSLIT
    out = []
    for char in (title or "").strip().lower():
        if char in table:
            out.append(table[char])
        elif char.isascii() and char.isalnum():
            out.append(char)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")[:48]
    return slug if slug and slug[0].isalnum() else ""


# --- развёртка сети -------------------------------------------------------
def parse_networks(raw: str | list[str]) -> list[ipaddress.IPv4Network]:
    chunks = raw.replace(",", " ").split() if isinstance(raw, str) else list(raw)
    networks, total = [], 0
    for chunk in chunks:
        try:
            # Уже разобранную сеть пропускаем как есть: мост проверяет список
            # один раз и передаёт сюда объекты, а не строки.
            network = (chunk if isinstance(chunk, ipaddress.IPv4Network)
                       else ipaddress.ip_network(str(chunk).strip(), strict=False))
        except ValueError as exc:
            raise DiscoveryError("not_network", value=str(chunk)) from exc
        if network.version != 4:
            raise DiscoveryError("ipv4_only")
        if not (network.is_private or network.is_loopback):
            raise DiscoveryError("not_private", network=str(network))
        total += network.num_addresses
        if total > MAX_SCAN_HOSTS:
            raise DiscoveryError("range_too_large")
        networks.append(network)
    if not networks:
        raise DiscoveryError("no_networks")
    return networks


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(networks, *, ports: tuple[int, ...] = SCAN_PORTS, timeout: float = SCAN_TIMEOUT,
         workers: int = 96, extra_hosts: list[str] | None = None) -> list[Candidate]:
    """Кандидаты — адреса с открытым RTSP, откликнувшейся службой ONVIF или
    неактивированная Hikvision (у новой нет ни того, ни другого до пароля).

    `extra_hosts` — ответившие на WS-Discovery/SADP: их опрашиваем, даже если
    они вне развёртки (другая подсеть той же LAN).
    """
    nets = parse_networks(networks) if isinstance(networks, (str, list)) else networks
    targets = [str(ip) for net in nets
               for ip in (net.hosts() if net.num_addresses > 2 else net)]
    targets += [host for host in (extra_hosts or []) if host not in targets]
    found: dict[str, Candidate] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(_port_open, host, port, timeout): (host, port)
                for host in targets for port in ports}
        for job in concurrent.futures.as_completed(jobs):
            host, port = jobs[job]
            try:
                if not job.result():
                    continue
            except Exception:  # отказ одного сокета не должен рвать весь опрос
                continue
            found.setdefault(host, Candidate(host=host)).ports.append(port)
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for candidate in pool.map(lambda c: identify(c, timeout=timeout), found.values()):
            if (554 in candidate.ports or candidate.onvif_url or candidate.activated is not None
                    or candidate.activation):
                candidates.append(candidate)
    candidates.sort(key=lambda c: tuple(int(part) for part in c.host.split(".")))
    return candidates


def identify(candidate: Candidate, *, timeout: float = SCAN_TIMEOUT) -> Candidate:
    """Догадаться, кто это, ничего не зная о логине: баннер RTSP и ONVIF без авторизации."""
    if 554 in candidate.ports:
        candidate.rtsp_banner = rtsp_banner(candidate.host, timeout=timeout)
    for port in ONVIF_PORTS:
        if port not in candidate.ports:
            continue
        for path in ONVIF_PATHS:
            url = f"http://{candidate.host}:{port}{path}"
            try:
                # GetSystemDateAndTime по спецификации отвечает без авторизации:
                # это самый дешёвый способ отличить камеру от чужой веб-морды.
                root = soap_call(url, "<tds:GetSystemDateAndTime/>", timeout=timeout)
            except Exception:
                continue
            if root is not None:
                candidate.onvif_url = url
                break
        if candidate.onvif_url:
            break
    hint = f"{candidate.rtsp_banner}".lower()
    for needle, vendor in VENDOR_HINTS:
        if needle in hint:
            candidate.vendor = vendor
            break
    web = next((port for port in ACTIVATION_WEB_PORTS if port in candidate.ports), None)
    if web is not None and candidate.vendor in ("", "hikvision"):
        # /SDK/activateStatus отвечает без пароля и только у Hikvision: так
        # видна новая камера, которой ещё нечем пустить нас в ONVIF и RTSP.
        from . import hikvision_activation as hik

        status = hik.activation_status(f"http://{candidate.host}:{web}", timeout=timeout)
        if status.activated is not None:
            candidate.vendor = candidate.brand = "hikvision"
            candidate.activated, candidate.activation = status.activated, status.protocol
    if candidate.activated is None:
        try:
            _identify_brand(candidate, web, timeout=timeout)
        except Exception:  # чужая прошивка не должна ронять опрос сети
            pass
    return candidate


def _identify_brand(candidate: Candidate, web: int | None, *, timeout: float) -> None:
    """Марка и «ждёт настройки» для тех, кого не опознала ветка Hikvision.

    Только то, что камера отдаёт без пароля; входа не пробуем (см. vendor_setup)."""
    from . import vendor_setup

    base = f"http://{candidate.host}:{web}" if web is not None else ""
    page = vendor_setup.web_fingerprint(base, timeout=timeout) if base else ""
    brand = vendor_setup.brand_from_text(candidate.rtsp_banner, page)
    if not brand:
        return
    candidate.brand = brand
    candidate.vendor = candidate.vendor or brand
    if brand in vendor_setup.AUTO_ACTIVATION:
        return
    state = vendor_setup.needs_setup(brand, base, timeout=timeout) if base else None
    if state is not None:
        candidate.activated = not state
    # Одна веб-морда без RTSP и ONVIF у камеры известной марки — почти всегда
    # новая, ещё без пароля (потоки поднимаются после настройки).
    web_only = 554 not in candidate.ports and not candidate.onvif_url
    if state is True or (state is None and web_only):
        candidate.activation = "manual"


def rtsp_banner(host: str, port: int = 554, *, timeout: float = SCAN_TIMEOUT) -> str:
    """Заголовок Server из ответа на OPTIONS — авторизация для этого не нужна."""
    request = (f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n"
               f"User-Agent: cctv-discovery\r\n\r\n").encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            data = sock.recv(2048)
    except OSError:
        return ""
    match = re.search(rb"(?im)^(?:Server|WWW-Authenticate)\s*:\s*([^\r\n]+)", data)
    return match.group(1).decode("utf-8", "replace").strip() if match else ""


# --- ONVIF ----------------------------------------------------------------
def _security(user: str | None, password: str | None) -> str:
    if not user:
        return ""
    nonce = secrets.token_bytes(16)
    created = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + (password or "").encode()).digest()
    ).decode()
    return (f'<s:Header><Security s:mustUnderstand="1" xmlns="{WSSE}">'
            f'<UsernameToken><Username>{_xml(user)}</Username>'
            f'<Password Type="{PASSWORD_DIGEST_TYPE}">{digest}</Password>'
            f'<Nonce>{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{WSU}">{created}</Created>'
            f"</UsernameToken></Security></s:Header>")


def _xml(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def soap_call(url: str, body: str, *, user: str | None = None, password: str | None = None,
              timeout: float = PROBE_TIMEOUT) -> ET.Element | None:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{_security(user, password)}<s:Body>{body}</s:Body></s:Envelope>"
    ).encode()
    request = urllib.request.Request(
        url, data=envelope,
        headers={"Content-Type": "application/soap+xml; charset=utf-8",
                 "Content-Length": str(len(envelope))})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        payload = exc.read(512 * 1024)
        if exc.code in (401, 403):
            raise DiscoveryError("auth_failed") from None
    except Exception as exc:  # сеть, TLS, мусор в ответе — всё это «не ONVIF»
        raise DiscoveryError("onvif_no_answer", reason=type(exc).__name__) from None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise DiscoveryError("onvif_unparsed") from None
    fault = root.find(".//s:Fault", NS)
    if fault is not None:
        text = " ".join(t.strip() for t in fault.itertext() if t.strip())
        if re.search(r"(?i)auth|password|credential|sender not authorized", text):
            raise DiscoveryError("auth_failed")
        raise DiscoveryError("onvif_refused")
    return root


def onvif_service(host: str, *, timeout: float = SCAN_TIMEOUT) -> str:
    candidate = identify(Candidate(host=host, ports=list(ONVIF_PORTS)), timeout=timeout)
    return candidate.onvif_url


def with_credentials(url: str, user: str, password: str) -> str:
    """Вшить логин и пароль в URL: и proxy, и ffmpeg берут их только оттуда."""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return url
    userinfo = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
    netloc = f"{userinfo}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _profiles(root: ET.Element) -> list[Profile]:
    result = []
    for node in root.iter():
        if not node.tag.endswith("}Profiles"):
            continue
        token = node.attrib.get("token", "")
        name = (node.findtext("tt:Name", default="", namespaces=NS) or token).strip()
        encoder = node.find("tt:VideoEncoderConfiguration", NS)
        profile = Profile(token=token, name=name or token)
        if encoder is not None:
            profile.encoding = (encoder.findtext("tt:Encoding", default="", namespaces=NS) or "").upper()
            resolution = encoder.find("tt:Resolution", NS)
            if resolution is not None:
                profile.width = int(resolution.findtext("tt:Width", default="0", namespaces=NS) or 0)
                profile.height = int(resolution.findtext("tt:Height", default="0", namespaces=NS) or 0)
            fps = encoder.findtext(".//tt:FrameRateLimit", default="0", namespaces=NS)
            profile.fps = int(float(fps or 0))
        if token:
            result.append(profile)
    return result


def probe(host: str, user: str, password: str, *, service_url: str = "",
          timeout: float = PROBE_TIMEOUT, vendor_hint: str = "",
          detect_url: str = "") -> Detected:
    """Определить параметры камеры по её же ответам. Пароль наружу не выходит.

    `host` — IP камеры либо готовый `rtsp://…` (тогда ONVIF не спрашиваем, поток
    берём как есть); `detect_url` — необязательный второй поток для детектора.
    """
    if "://" in host:
        return probe_url(host, user, password, detect_url=detect_url, timeout=timeout)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise DiscoveryError("bad_address") from None
    detected = Detected(host=host, vendor=vendor_hint)
    service = service_url or onvif_service(host, timeout=min(timeout, 2.0))
    if service:
        try:
            _probe_onvif(detected, service, user, password, timeout)
        except DiscoveryError as exc:
            if exc.code == "auth_failed":
                raise
            detected.source = "template"
    else:
        detected.source = "template"
    if not detected.main_url:
        _probe_templates(detected, user, password, timeout)
    if not detected.main_url:
        raise DiscoveryError("no_stream")
    detected.verified = rtsp_ok(detected.main_url, timeout=timeout)
    if detected.sub_url and not rtsp_ok(detected.sub_url, timeout=timeout):
        detected.sub_url = ""
    return detected


def probe_url(url: str, user: str, password: str, *, detect_url: str = "",
              timeout: float = PROBE_TIMEOUT) -> Detected:
    """Поток, заданный человеком: проверить его и (если дан) поток детектора."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "rtsp" or not parts.hostname:
        raise DiscoveryError("stream_not_rtsp")
    detected = Detected(host=parts.hostname, source="manual")
    detected.main_url = with_credentials(url, user, password)
    code = rtsp_describe(detected.main_url, timeout=timeout)[0]
    if code == 401:
        raise DiscoveryError("auth_failed")
    detected.verified = code == 200
    if detect_url:
        sub = urllib.parse.urlsplit(detect_url)
        if sub.scheme != "rtsp" or sub.hostname != parts.hostname:
            raise DiscoveryError("detect_other_camera")
        candidate = with_credentials(detect_url, user, password)
        detected.sub_url = candidate if rtsp_ok(candidate, timeout=timeout) else ""
    return detected


def ws_discover(*, timeout: float = 3.0) -> list[str]:
    """Адреса камер, ответивших на multicast WS-Discovery Probe (ONVIF NVT).

    Работает только в одной L2-сети с камерами (контейнер — в сети хоста).
    Отвечают XML со списком XAddrs; берём из них приватные IPv4.
    """
    message_id = f"uuid:{secrets.token_hex(16)}"
    probe_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header><w:MessageID>{message_id}</w:MessageID>'
        '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        '</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
        '</d:Probe></e:Body></e:Envelope>').encode()
    hosts: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.5)
            sock.sendto(probe_xml, ("239.255.255.250", 3702))
            deadline = dt.datetime.now().timestamp() + timeout
            while dt.datetime.now().timestamp() < deadline:
                try:
                    data, _addr = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                for match in re.finditer(rb"https?://([0-9.]+)[:/]", data):
                    host = match.group(1).decode()
                    try:
                        if ipaddress.ip_address(host).is_private:
                            hosts.add(host)
                    except ValueError:
                        continue
    except OSError:
        return []
    return sorted(hosts, key=lambda h: tuple(int(p) for p in h.split(".")))


def local_networks() -> list[str]:
    """Приватные /24 собственных адресов узла — сети поиска по умолчанию.

    Без них домашний пользователь упирался бы в «сети не заданы» на первом
    же /add. Берётся адрес, с которого узел ходит наружу (маршрут по
    умолчанию, без отправки пакетов), и адреса из имени хоста.
    """
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET: пакет не уходит, только выбор маршрута
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    networks = []
    for address in sorted(addresses):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            network = str(ipaddress.ip_network(f"{address}/24", strict=False))
            if network not in networks:
                networks.append(network)
    return networks


def _probe_onvif(detected: Detected, service: str, user: str, password: str,
                 timeout: float) -> None:
    root = soap_call(service, "<tds:GetDeviceInformation/>", user=user, password=password,
                     timeout=timeout)
    if root is not None:
        detected.vendor = (root.findtext(".//tds:Manufacturer", default="", namespaces=NS)
                           or detected.vendor).strip()
        detected.model = root.findtext(".//tds:Model", default="", namespaces=NS).strip()
        detected.serial = root.findtext(".//tds:SerialNumber", default="", namespaces=NS).strip()
    media = service
    caps = soap_call(service, "<tds:GetCapabilities><tds:Category>All</tds:Category>"
                              "</tds:GetCapabilities>", user=user, password=password,
                     timeout=timeout)
    if caps is not None:
        for node in caps.iter():
            if node.tag.endswith("}Media"):
                xaddr = node.findtext("tt:XAddr", default="", namespaces=NS)
                if xaddr:
                    # XAddr нередко приходит с внутренним адресом устройства —
                    # доверяем только хосту, к которому уже достучались; порт без
                    # явного в XAddr — тот же, что у службы, ответившей нам.
                    parts = urllib.parse.urlsplit(xaddr)
                    port = parts.port or urllib.parse.urlsplit(service).port
                    port = f":{port}" if port else ""
                    media = urllib.parse.urlunsplit(
                        ("http", f"{detected.host}{port}", parts.path, "", ""))
                break
    profiles_root = soap_call(media, "<trt:GetProfiles/>", user=user, password=password,
                              timeout=timeout)
    profiles = _profiles(profiles_root) if profiles_root is not None else []
    for profile in profiles:
        try:
            uri_root = soap_call(
                media,
                "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                f"</trt:StreamSetup><trt:ProfileToken>{_xml(profile.token)}</trt:ProfileToken>"
                "</trt:GetStreamUri>", user=user, password=password, timeout=timeout)
        except DiscoveryError:
            continue
        if uri_root is None:
            continue
        uri = uri_root.findtext(".//tt:Uri", default="", namespaces=NS).strip()
        if uri:
            profile.stream_uri = _rehost(uri, detected.host)
    usable = [p for p in profiles if p.stream_uri]
    if not usable:
        return
    detected.profiles = usable
    ordered = sorted(usable, key=lambda p: p.pixels, reverse=True)
    detected.main_url = with_credentials(ordered[0].stream_uri, user, password)
    if len(ordered) > 1:
        detected.sub_url = with_credentials(ordered[-1].stream_uri, user, password)
    try:
        snap = soap_call(media, "<trt:GetSnapshotUri><trt:ProfileToken>"
                                f"{_xml(ordered[0].token)}</trt:ProfileToken>"
                                "</trt:GetSnapshotUri>",
                         user=user, password=password, timeout=timeout)
        uri = snap.findtext(".//tt:Uri", default="", namespaces=NS).strip() if snap is not None else ""
        if uri:
            detected.snapshot_url = _rehost(uri, detected.host)
    except DiscoveryError:
        detected.snapshot_url = ""


def _rehost(url: str, host: str) -> str:
    """Заменить хост в адресе от камеры на тот, по которому она реально доступна."""
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return url
    netloc = host if not parts.port else f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _probe_templates(detected: Detected, user: str, password: str, timeout: float) -> None:
    vendor = (detected.vendor or "").lower()
    order = [name for name in TEMPLATES if name != "generic" and name in vendor]
    order += [name for name in TEMPLATES if name not in order]
    for name in order:
        template = TEMPLATES[name]
        main = with_credentials(template["main"].format(host=detected.host, port=554),
                                user, password)
        if not rtsp_ok(main, timeout=timeout):
            continue
        detected.source = "template"
        detected.vendor = detected.vendor or name
        detected.main_url = main
        sub = with_credentials(template["sub"].format(host=detected.host, port=554),
                               user, password)
        detected.sub_url = sub if rtsp_ok(sub, timeout=timeout) else ""
        if template["snapshot"]:
            detected.snapshot_url = template["snapshot"].format(host=detected.host)
        return


# --- проверка потока -------------------------------------------------------
def _digest(header: str, method: str, uri: str, user: str, password: str) -> str:
    attrs = {k.lower(): v for k, v in re.findall(r'(\w+)="?([^",]+)"?', header)}
    realm, nonce = attrs.get("realm", ""), attrs.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    pieces = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in attrs.get("qop", ""):
        nc, cnonce = "00000001", secrets.token_hex(8)
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        pieces += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    pieces.append(f'response="{response}"')
    return "Digest " + ", ".join(pieces)


def rtsp_describe(url: str, *, timeout: float = PROBE_TIMEOUT) -> tuple[int, str]:
    """DESCRIBE с Digest/Basic. Возвращает код и SDP; пароль не логируется."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "rtsp" or not parts.hostname:
        return 0, ""
    user = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    host, port = parts.hostname, parts.port or 554
    target = urllib.parse.urlunsplit(("rtsp", f"{host}:{port}", parts.path, parts.query, ""))

    def request(seq: int, auth: str | None) -> bytes:
        head = [f"DESCRIBE {target} RTSP/1.0", f"CSeq: {seq}", "Accept: application/sdp",
                "User-Agent: cctv-discovery"]
        if auth:
            head.append(f"Authorization: {auth}")
        return ("\r\n".join(head) + "\r\n\r\n").encode()

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request(1, None))
            data = sock.recv(8192)
            code = _rtsp_code(data)
            if code == 401 and user:
                challenge = re.search(rb"(?im)^WWW-Authenticate:\s*([^\r\n]+)", data)
                header = challenge.group(1).decode("utf-8", "replace") if challenge else ""
                if header.lower().startswith("digest"):
                    auth = _digest(header, "DESCRIBE", target, user, password)
                else:
                    token = base64.b64encode(f"{user}:{password}".encode()).decode()
                    auth = f"Basic {token}"
                sock.sendall(request(2, auth))
                data = sock.recv(16384)
                code = _rtsp_code(data)
            body = data.partition(b"\r\n\r\n")[2].decode("utf-8", "replace")
            return code, body
    except OSError:
        return 0, ""


def _rtsp_code(data: bytes) -> int:
    match = re.match(rb"RTSP/1\.\d (\d{3})", data or b"")
    return int(match.group(1)) if match else 0


def rtsp_ok(url: str, *, timeout: float = PROBE_TIMEOUT) -> bool:
    return rtsp_describe(url, timeout=timeout)[0] == 200


def codec_of(sdp: str) -> str:
    match = re.search(r"(?im)^a=rtpmap:\d+\s+([A-Za-z0-9]+)/", sdp or "")
    return match.group(1).upper() if match else ""
