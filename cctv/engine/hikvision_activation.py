#!/usr/bin/env python3
"""Активация новых камер Hikvision и подготовка их к записи.

Новая Hikvision приходит «неактивированной»: пароля у неё нет, и пока его не
задать, она не отдаёт ни RTSP, ни ONVIF. Задать пароль можно только через
ISAPI, и не открытым текстом: камера даёт одноразовый ключ (challenge),
зашифрованный нашим RSA-ключом, и принимает пароль, зашифрованный этим ключом
по AES. Протоколов два, оба восстановлены из веб-клиента камер:

* V3 (`/SDK/activateStatus` → supportVersion=3, прошивки G5 V5.7.x и новее):
  RSA e=65537 строго 3072 бит, `GetChallengeV3` → AES-CBC со случайным iv →
  `StartActivateV3`. Проверен на живой G5 20.09.2026.
* прежний (supportVersion нет или < 3): `/ISAPI/Security/challenge` → AES-ECB →
  `PUT /ISAPI/System/activate`. Собран по той же схеме challenge, живой камерой
  со старой прошивкой не проверен.

Криптография — на стандартной библиотеке: в образе нет ни `cryptography`, ни
OpenSSL-биндингов, а тянуть их ради одной операции заведения камеры незачем.
AES проверяется в тестах эталонными векторами FIPS-197/SP 800-38A.

Активация необратима (снять пароль можно только сбросом камеры кнопкой), а
неверный пароль в логине Hikvision считает попыткой подбора и после нескольких
блокирует вход. Поэтому каждая проверка нового пароля — один запрос, без
автоповторов (`urllib` с HTTPDigestAuthHandler повторяет логин до пяти раз —
здесь Digest считается вручную). Пароли не попадают ни в журнал, ни в тексты
ошибок: наружу — только коды стадий.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import string
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import camera_discovery as discovery

HTTP_TIMEOUT = 8.0
ADMIN_USER = "admin"
V3_KEY_BITS = 3072  # GetChallengeV3 на 2048 отвечает «Invalid JSON Content»
LEGACY_KEY_BITS = (1024, 2048)  # прежний протокол: веб-клиент слал 1024
RSA_E = 65537
# Правила пароля Hikvision: 8–16 печатных ASCII без пробела, не меньше двух
# классов символов (цифры, строчные, прописные, спецсимволы), без имени
# пользователя внутри. Один класс камера отвергает как «слабый».
PASSWORD_MIN, PASSWORD_MAX = 8, 16
# Спецсимволы генератора — те, что не ломают ни XML, ни URL, ни shell-цитаты.
GENERATED_SPECIALS = "-_.+=#%"
GENERATED_LENGTH = 14
ONVIF_USER = "dozorcam"
ONVIF_USER_TYPE = "operator"  # GetProfiles/GetStreamUri хватает; не администратор


class ActivationError(RuntimeError):
    """Сбой с кодом стадии. `code` — для человека через каталог, без секретов."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


# --- пароль ----------------------------------------------------------------
def password_problem(password: str, user: str = ADMIN_USER) -> str | None:
    """Код причины, по которой камера пароль не примет; None — подходит."""
    if not isinstance(password, str):
        return "password_length"
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return "password_length"
    if any(ch not in string.printable or ch in string.whitespace for ch in password):
        return "password_charset"
    classes = sum(any(ch in group for ch in password) for group in (
        string.digits, string.ascii_lowercase, string.ascii_uppercase, string.punctuation))
    if classes < 2:
        return "password_weak"
    if user and user.lower() in password.lower():
        return "password_has_user"
    return None


def generate_password(length: int = GENERATED_LENGTH) -> str:
    """Стойкий пароль по правилам Hikvision: все четыре класса символов."""
    length = max(PASSWORD_MIN, min(PASSWORD_MAX, length))
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, GENERATED_SPECIALS)
    alphabet = "".join(pools)
    while True:
        chars = [secrets.choice(pool) for pool in pools]
        chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
        for i in range(len(chars) - 1, 0, -1):  # Фишер — Йейтс на secrets
            j = secrets.randbelow(i + 1)
            chars[i], chars[j] = chars[j], chars[i]
        password = "".join(chars)
        if password_problem(password) is None:
            return password


# --- AES (FIPS-197) ---------------------------------------------------------
def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a, b = _xtime(a), b >> 1
    return out


def _make_sbox() -> tuple[list[int], list[int]]:
    sbox, inv = [0] * 256, [0] * 256
    for x in range(256):
        # обратный в GF(2^8) перебором — 256×256 операций один раз при импорте
        y = 0 if x == 0 else next(c for c in range(1, 256) if _gmul(x, c) == 1)
        s = y
        for shift in range(1, 5):
            s ^= ((y << shift) | (y >> (8 - shift))) & 0xFF
        s ^= 0x63
        sbox[x], inv[s] = s, x
    return sbox, inv


SBOX, INV_SBOX = _make_sbox()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) not in (16, 24, 32):
        raise ValueError("AES-ключ — 16, 24 или 32 байта")
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        word = list(words[i - 1])
        if i % nk == 0:
            word = word[1:] + word[:1]
            word = [SBOX[b] for b in word]
            word[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            word = [SBOX[b] for b in word]
        words.append([a ^ b for a, b in zip(words[i - nk], word)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(rounds + 1)]


def _encrypt_block(block: bytes, keys: list[list[int]]) -> bytes:
    s = [b ^ k for b, k in zip(block, keys[0])]
    for rnd in range(1, len(keys)):
        s = [SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]  # ShiftRows (столбцы по 4)
        if rnd != len(keys) - 1:
            mixed = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                mixed += [_xtime(a[0]) ^ _xtime(a[1]) ^ a[1] ^ a[2] ^ a[3],
                          a[0] ^ _xtime(a[1]) ^ _xtime(a[2]) ^ a[2] ^ a[3],
                          a[0] ^ a[1] ^ _xtime(a[2]) ^ _xtime(a[3]) ^ a[3],
                          _xtime(a[0]) ^ a[0] ^ a[1] ^ a[2] ^ _xtime(a[3])]
            s = mixed
        s = [b ^ k for b, k in zip(s, keys[rnd])]
    return bytes(s)


def _decrypt_block(block: bytes, keys: list[list[int]]) -> bytes:
    s = [b ^ k for b, k in zip(block, keys[-1])]
    for rnd in range(len(keys) - 2, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]  # InvShiftRows
        s = [INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, keys[rnd])]
        if rnd:
            mixed = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                mixed += [_gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9),
                          _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13),
                          _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11),
                          _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)]
            s = mixed
    return bytes(s)


def pkcs7_pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    n = data[-1] if data else 0
    if not 1 <= n <= 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("неверное дополнение PKCS7")
    return data[:-n]


def aes_encrypt(key: bytes, data: bytes, *, iv: bytes | None = None, pad: bool = True) -> bytes:
    """AES-CBC при заданном iv, иначе ECB. Дополнение PKCS7, если не сказано иначе."""
    keys = _expand_key(key)
    data = pkcs7_pad(data) if pad else data
    if len(data) % 16:
        raise ValueError("длина без дополнения не кратна блоку")
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        if prev is not None:
            block = bytes(a ^ b for a, b in zip(block, prev))
        enc = _encrypt_block(block, keys)
        out += enc
        if iv is not None:
            prev = enc
    return bytes(out)


def aes_decrypt(key: bytes, data: bytes, *, iv: bytes | None = None, pad: bool = True) -> bytes:
    keys = _expand_key(key)
    if not data or len(data) % 16:
        raise ValueError("шифртекст не кратен блоку")
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        dec = _decrypt_block(block, keys)
        if prev is not None:
            dec = bytes(a ^ b for a, b in zip(dec, prev))
            prev = block
        out += dec
    return pkcs7_unpad(bytes(out)) if pad else bytes(out)


# --- RSA -------------------------------------------------------------------
_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _probably_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if candidate % RSA_E != 1 and _probably_prime(candidate):
            return candidate


@dataclass(frozen=True)
class RsaKey:
    n: int
    d: int
    bits: int

    @classmethod
    def generate(cls, bits: int) -> "RsaKey":
        while True:
            p, q = _prime(bits // 2), _prime(bits - bits // 2)
            n = p * q
            if p == q or n.bit_length() != bits:
                continue
            phi = (p - 1) * (q - 1)
            try:
                d = pow(RSA_E, -1, phi)
            except ValueError:
                continue
            return cls(n=n, d=d, bits=bits)

    @property
    def public_b64(self) -> str:
        """Как у веб-клиента: base64 от hex-строки модуля (`n.toString(16)`)."""
        return base64.b64encode(format(self.n, "x").encode()).decode()

    def decrypt_challenge(self, challenge_b64: str) -> str:
        """base64 → hex шифртекста → сырой RSA → снять PKCS#1 v1.5 type 2 → строка."""
        try:
            cipher_hex = base64.b64decode(challenge_b64, validate=False).decode("ascii").strip()
            value = int(cipher_hex, 16)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise ActivationError("challenge_format") from None
        if not 0 < value < self.n:
            raise ActivationError("challenge_format")
        plain = pow(value, self.d, self.n).to_bytes((self.bits + 7) // 8, "big")
        # Разбор как в jsbn: ведущие нули, 0x02, ненулевая набивка, 0x00, данные.
        i = 0
        while i < len(plain) and plain[i] == 0:
            i += 1
        if i >= len(plain) or plain[i] != 2:
            raise ActivationError("challenge_decrypt")
        try:
            end = plain.index(0, i + 1)
        except ValueError:
            raise ActivationError("challenge_decrypt") from None
        try:
            text = plain[end + 1:].decode("ascii").strip()
        except UnicodeDecodeError:
            raise ActivationError("challenge_decrypt") from None
        if not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{48}|[0-9a-fA-F]{64}", text):
            raise ActivationError("challenge_decrypt")
        return text


def rsa_encrypt_pkcs1(n: int, data: bytes, e: int = RSA_E) -> int:
    """Шифрование PKCS#1 v1.5 type 2 — так камера прячет challenge (нужно эмулятору)."""
    size = (n.bit_length() + 7) // 8
    pad_len = size - 3 - len(data)
    if pad_len < 8:
        raise ValueError("данные длиннее ключа")
    padding = bytes(secrets.choice(range(1, 256)) for _ in range(pad_len))
    return pow(int.from_bytes(b"\x00\x02" + padding + b"\x00" + data, "big"), e, n)


def encrypt_password(challenge: str, password: str, iv: bytes | None) -> str:
    """Пароль под ключом challenge: hex(шифртекста) → base64, как у веб-клиента."""
    key = bytes.fromhex(challenge)
    plain = (challenge[:16] + password).encode()
    cipher = aes_encrypt(key, plain, iv=iv)
    return base64.b64encode(cipher.hex().encode()).decode()


def decrypt_password(challenge: str, encrypted: str, iv: bytes | None) -> str:
    """Обратное к `encrypt_password` — для эмулятора камеры в тестах."""
    cipher = bytes.fromhex(base64.b64decode(encrypted).decode())
    plain = aes_decrypt(bytes.fromhex(challenge), cipher, iv=iv).decode()
    if not plain.startswith(challenge[:16]):
        raise ValueError("префикс challenge не совпал")
    return plain[16:]


# --- HTTP к камере ------------------------------------------------------------
@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _send(method: str, url: str, body: bytes | None, headers: dict, timeout: float) -> Reply:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Reply(response.status, response.read(256 * 1024),
                         {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as exc:
        return Reply(exc.code, exc.read(256 * 1024) if exc.fp else b"",
                     {k.lower(): v for k, v in (exc.headers or {}).items()})
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
        raise ActivationError("no_answer") from None


def http(method: str, base: str, path: str, *, body: bytes | str | None = None,
         content_type: str = "application/xml", auth: tuple[str, str] | None = None,
         timeout: float = HTTP_TIMEOUT) -> Reply:
    """Один запрос к камере. С `auth` — Digest ровно одной попыткой логина.

    Первый запрос уходит без пароля (вызов-разведка ради nonce), второй — с
    ответом Digest. Повторно 401 — значит, пароль не принят: больше не пробуем,
    иначе собственными повторами довели бы камеру до блокировки входа.
    """
    url = base.rstrip("/") + path
    payload = body.encode() if isinstance(body, str) else body
    headers = {"Content-Type": content_type} if payload is not None else {}
    reply = _send(method, url, payload, headers, timeout)
    if reply.status != 401 or auth is None:
        return reply
    challenge = reply.headers.get("www-authenticate", "")
    if challenge.lower().startswith("digest"):
        parts = urllib.parse.urlsplit(url)
        uri = parts.path + (f"?{parts.query}" if parts.query else "")
        headers["Authorization"] = discovery._digest(challenge, method, uri, *auth)
    else:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return _send(method, url, payload, headers, timeout)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_values(text: str) -> dict[str, str]:
    """Плоский словарь «локальное имя тега → текст» (первое вхождение)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    values: dict[str, str] = {}
    for node in root.iter():
        values.setdefault(_local(node.tag), (node.text or "").strip())
    return values


def _status_ok(reply: Reply) -> bool:
    """ISAPI отвечает 200 и JSON/XML со statusCode 1 — только это успех."""
    if reply.status != 200:
        return False
    text = reply.text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return False
        code = data.get("statusCode", 1) if isinstance(data, dict) else 0
        return str(code) == "1"
    values = _xml_values(text) if text else {}
    return values.get("statuscode", "1") == "1"


# --- состояние активации ------------------------------------------------------
@dataclass(frozen=True)
class Status:
    activated: bool | None  # None — не Hikvision или ответ не разобран
    protocol: str = ""      # "v3" | "legacy"

    def as_dict(self) -> dict:
        return {"activated": self.activated, "protocol": self.protocol}


def activation_status(base: str, *, timeout: float = HTTP_TIMEOUT) -> Status:
    """`GET /SDK/activateStatus` — отвечает без пароля, и до, и после активации."""
    try:
        reply = http("GET", base, "/SDK/activateStatus", timeout=timeout)
    except ActivationError:
        return Status(None)
    if reply.status != 200:
        return Status(None)
    values = _xml_values(reply.text)
    flag = (values.get("activated") or values.get("isactivated") or "").lower()
    if flag not in ("true", "false"):
        return Status(None)
    try:
        version = int(values.get("supportversion") or 0)
    except ValueError:
        version = 0
    return Status(flag == "true", "v3" if version >= 3 else "legacy")


# --- активация ---------------------------------------------------------------
class KeyCache:
    """RSA-ключ на пакет: 3072 бита на чистом Python — секунды, а не на каждую камеру."""

    def __init__(self) -> None:
        self._keys: dict[int, RsaKey] = {}

    def get(self, bits: int) -> RsaKey:
        if bits not in self._keys:
            self._keys[bits] = RsaKey.generate(bits)
        return self._keys[bits]


def activate(base: str, password: str, protocol: str, *, keys: KeyCache | None = None,
             timeout: float = HTTP_TIMEOUT) -> None:
    """Задать пароль admin неактивированной камере. Исключение — с кодом стадии.

    Успех ответа камеры здесь ещё не означает, что пароль принят: проверяет
    вызывающий — повторным `activation_status` и одним логином новым паролем.
    """
    problem = password_problem(password)
    if problem:
        raise ActivationError(problem)
    keys = keys or KeyCache()
    if protocol == "v3":
        _activate_v3(base, password, keys, timeout)
    else:
        _activate_legacy(base, password, keys, timeout)


def _activate_v3(base: str, password: str, keys: KeyCache, timeout: float) -> None:
    key = keys.get(V3_KEY_BITS)
    reply = http("POST", base, "/ISAPI/System/activate/GetChallengeV3?format=json",
                 body=json.dumps({"publicKey": key.public_b64}),
                 content_type="application/json", timeout=timeout)
    if reply.status != 200:
        raise ActivationError("challenge_refused")
    try:
        challenge_b64 = str(json.loads(reply.text).get("challenge") or "")
    except (ValueError, AttributeError):
        raise ActivationError("challenge_format") from None
    challenge = key.decrypt_challenge(challenge_b64)
    iv = secrets.token_bytes(16)
    body = json.dumps({"password": encrypt_password(challenge, password, iv), "iv": iv.hex()})
    reply = http("POST", base, "/ISAPI/System/activate/StartActivateV3?format=json",
                 body=body, content_type="application/json", timeout=timeout)
    if not _status_ok(reply):
        raise ActivationError("activate_refused")


def _activate_legacy(base: str, password: str, keys: KeyCache, timeout: float) -> None:
    challenge_b64 = ""
    for bits in LEGACY_KEY_BITS:
        key = keys.get(bits)
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<PublicKey version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                f"<key>{key.public_b64}</key></PublicKey>")
        reply = http("POST", base, "/ISAPI/Security/challenge", body=body, timeout=timeout)
        if reply.status == 200:
            challenge_b64 = _xml_values(reply.text).get("key", "")
            if challenge_b64:
                break
    if not challenge_b64:
        raise ActivationError("challenge_refused")
    challenge = key.decrypt_challenge(challenge_b64)
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<ActivateInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<password>{encrypt_password(challenge, password, None)}</password></ActivateInfo>")
    reply = http("PUT", base, "/ISAPI/System/activate", body=body, timeout=timeout)
    if not _status_ok(reply):
        raise ActivationError("activate_refused")


# --- после активации ------------------------------------------------------------
def verify_login(base: str, user: str, password: str, *,
                 timeout: float = HTTP_TIMEOUT) -> dict:
    """Один логин новым паролем. 200 на смену пароля ничего не доказывает —
    доказывает только вход (урок самоблокировки 28.08.2026)."""
    reply = http("GET", base, "/ISAPI/System/deviceInfo", auth=(user, password), timeout=timeout)
    if reply.status == 401:
        raise ActivationError("login_refused")
    if reply.status != 200:
        raise ActivationError("login_failed")
    values = _xml_values(reply.text)
    return {"model": values.get("model", ""), "serial": values.get("serialnumber", "")}


def enable_onvif(base: str, admin: tuple[str, str], *, timeout: float = HTTP_TIMEOUT) -> None:
    """Включить ONVIF. PUT камера принимает только телом ровно из живого GET
    (с теми же переносами строк), поэтому меняется одно значение в нём."""
    reply = http("GET", base, "/ISAPI/System/Network/Integrate", auth=admin, timeout=timeout)
    if reply.status != 200:
        raise ActivationError("onvif_enable")
    text = reply.text
    block = re.search(r"(<ONVIF\b[^>]*>)(.*?)(</ONVIF>)", text, re.S)
    if block is None:
        raise ActivationError("onvif_enable")
    if re.search(r"<enable>\s*true\s*</enable>", block.group(2)):
        return
    inner, count = re.subn(r"<enable>\s*false\s*</enable>", "<enable>true</enable>", block.group(2), 1)
    if not count:
        raise ActivationError("onvif_enable")
    body = text[:block.start(2)] + inner + text[block.end(2):]
    reply = http("PUT", base, "/ISAPI/System/Network/Integrate", body=body, auth=admin,
                 timeout=timeout)
    if not _status_ok(reply):
        raise ActivationError("onvif_enable")


def ensure_onvif_user(base: str, admin: tuple[str, str], user: str, password: str, *,
                      timeout: float = HTTP_TIMEOUT) -> None:
    """Завести ONVIF-пользователя. Его стор у Hikvision отдельный от admin: именно
    через него ходят ONVIF и RTSP с `?profile=` — и он же запасной путь, если
    основной пароль когда-нибудь потеряется."""
    reply = http("GET", base, "/ISAPI/Security/ONVIF/users", auth=admin, timeout=timeout)
    if reply.status != 200:
        raise ActivationError("onvif_user")
    existing: dict[str, str] = {}
    try:
        root = ET.fromstring(reply.text) if reply.text.strip() else None
    except ET.ParseError:
        root = None
    for node in (root.iter() if root is not None else []):
        if _local(node.tag) == "user":
            fields = {_local(child.tag): (child.text or "").strip() for child in node}
            if fields.get("username"):
                existing[fields["username"]] = fields.get("id", "")
    ids = [int(v) for v in existing.values() if v.isdigit()]
    user_id = existing.get(user) or str(max(ids, default=0) + 1)
    body = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<User version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<id>{user_id}</id><userName>{discovery._xml(user)}</userName>"
            f"<password>{discovery._xml(password)}</password>"
            f"<userType>{ONVIF_USER_TYPE}</userType></User>")
    if user in existing:
        reply = http("PUT", base, f"/ISAPI/Security/ONVIF/users/{user_id}", body=body,
                     auth=admin, timeout=timeout)
    else:
        reply = http("POST", base, "/ISAPI/Security/ONVIF/users", body=body, auth=admin,
                     timeout=timeout)
    if not _status_ok(reply):
        raise ActivationError("onvif_user")


# --- SADP ----------------------------------------------------------------------
SADP_GROUP = ("239.255.255.250", 37020)


def sadp_discover(*, timeout: float = 2.0) -> dict[str, dict]:
    """Hikvision SADP: устройство само говорит, активировано ли оно, и где оно.

    Ответ приходит и от камеры на заводском адресе в чужой подсети —
    тогда по IPv4 её не достать, но человеку важно знать, что она есть.
    Работает только в одной L2-сети с камерами.
    """
    probe = ('<?xml version="1.0" encoding="utf-8"?><Probe>'
             f"<Uuid>{secrets.token_hex(16).upper()}</Uuid><Types>inquiry</Types></Probe>").encode()
    found: dict[str, dict] = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.5)
            sock.sendto(probe, SADP_GROUP)
            import time as _time

            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                try:
                    data, _addr = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                parsed = parse_sadp(data)
                if parsed:
                    found[parsed["host"]] = parsed
    except OSError:
        return {}
    return found


def parse_sadp(data: bytes) -> dict | None:
    values = _xml_values(data.decode("utf-8", "replace"))
    host = values.get("ipv4address", "")
    flag = values.get("activated", "").lower()
    if not host or flag not in ("true", "false"):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.version != 4 or not address.is_private or address.is_unspecified:
        return None  # опрашиваем только приватные адреса, как и WS-Discovery
    return {"host": host, "activated": flag == "true",
            "model": values.get("devicedescription") or values.get("devicetype", ""),
            "mac": values.get("mac", "")}
