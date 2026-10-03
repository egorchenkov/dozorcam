#!/usr/bin/env python3
"""Loopback RTSP relay that keeps camera credentials out of ffmpeg argv.

ffmpeg cannot read RTSP credentials from a protected file.  The relay performs
the Digest handshake with the camera and exposes an unauthenticated, loopback
only URL to local consumers.  It is intentionally not a general-purpose proxy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from .. import settings


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    path: str
    username: str
    password: str


def upstream(url: str) -> Upstream:
    parsed = urlsplit(url)
    if parsed.scheme != "rtsp" or not parsed.hostname:
        raise ValueError("only complete rtsp URLs are supported")
    username, password = unquote(parsed.username or ""), unquote(parsed.password or "")
    if not username or not password:
        raise ValueError("RTSP URL must contain credentials")
    endpoint = urlunsplit(("rtsp", f"{parsed.hostname}:{parsed.port or 554}", parsed.path, parsed.query, ""))
    return Upstream(parsed.hostname, parsed.port or 554, endpoint, username, password)


def rewrite_request(data: bytes, local: str, remote: Upstream, auth: str | None = None) -> bytes:
    head, sep, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        return data
    lines[0] = lines[0].replace(local.encode(), remote.path.encode(), 1)
    lines = [line for line in lines if not line.lower().startswith(b"authorization:")]
    if auth:
        lines.append(f"Authorization: {auth}".encode())
    return b"\r\n".join(lines) + sep + body


def digest_auth(request: bytes, challenge: bytes, remote: Upstream, local: str, count: int) -> str | None:
    """Build RFC 2617 Digest response without logging a secret."""
    match = re.search(br"WWW-Authenticate:\s*Digest\s+([^\r\n]+)", challenge, re.I)
    if not match:
        return None
    attrs = {key.lower(): value for key, value in re.findall(r'(\w+)="?([^",]+)"?', match.group(1).decode("utf-8", "replace"))}
    realm, nonce = attrs.get("realm"), attrs.get("nonce")
    if not realm or not nonce:
        return None
    method, uri = rewrite_request(request, local, remote).split(b"\r\n", 1)[0].decode("ascii", "replace").split()[:2]
    ha1 = hashlib.md5(f"{remote.username}:{realm}:{remote.password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    qop = attrs.get("qop", "")
    pieces = [f'username="{remote.username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in qop:
        nc, cnonce = f"{count:08x}", secrets.token_hex(8)
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        pieces.extend(["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"'])
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    pieces.append(f'response="{response}"')
    if attrs.get("opaque"):
        pieces.append(f'opaque="{attrs["opaque"]}"')
    return "Digest " + ", ".join(pieces)


def recv_message(sock: socket.socket) -> bytes:
    """Читает одно RTSP-сообщение целиком: заголовки и тело по Content-Length.

    Остановка на первом \\r\\n\\r\\n оставляла тело (SDP из DESCRIBE, HTML из 401)
    в сокете; следующий recv начинался с этого хвоста и ffmpeg получал битый ответ
    («Invalid data»). Дочитываем ровно объявленное тело, чтобы граница сообщений не плыла.
    """
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        block = sock.recv(4096)
        if not block:
            return data
        data += block
    head, _, body = data.partition(b"\r\n\r\n")
    match = re.search(br"Content-Length:\s*(\d+)", head, re.I)
    if not match:
        return data
    total = int(match.group(1))
    while len(body) < total:
        block = sock.recv(min(65536, total - len(body)))
        if not block:
            break
        body += block
    return head + b"\r\n\r\n" + body


def relay(source: socket.socket, target: socket.socket) -> None:
    try:
        while block := source.recv(65536):
            target.sendall(block)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve_client(client: socket.socket, remote: Upstream, local: str) -> None:
    try:
        with socket.create_connection((remote.host, remote.port), timeout=8) as server:
            challenge, count = None, 0
            while True:
                request = recv_message(client)
                if not request:
                    return
                auth = digest_auth(request, challenge, remote, local, count + 1) if challenge else None
                server.sendall(rewrite_request(request, local, remote, auth))
                response = recv_message(server)
                if b" 401 " in response.split(b"\r\n", 1)[0]:
                    challenge = response
                    auth = digest_auth(request, challenge, remote, local, count + 1)
                    if auth is None:
                        print("rtsp_auth_failed reason=unsupported_challenge", flush=True)
                        return
                    server.sendall(rewrite_request(request, local, remote, auth))
                    response = recv_message(server)
                    print("rtsp_auth_retry scheme=digest", flush=True)
                count += 1
                client.sendall(response)
                method = request.split(b" ", 1)[0].upper()
                if method == b"PLAY" and b" 200 " in response.split(b"\r\n", 1)[0]:
                    outbound = threading.Thread(target=relay, args=(client, server), daemon=True)
                    outbound.start()
                    relay(server, client)
                    return
    except OSError:
        print("rtsp_proxy_connection_failed", flush=True)
    finally:
        client.close()


def accept_loop(listener: socket.socket, remote: Upstream, local: str) -> None:
    while True:
        client, _ = listener.accept()
        threading.Thread(target=serve_client, args=(client, remote, local), daemon=True).start()


def listen_for(url: str, port: int, name: str) -> str:
    """Открывает loopback-листенер для одного upstream и отдаёт его локальный URL."""
    remote = upstream(url)
    local = f"rtsp://127.0.0.1:{port}/{name}"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    threading.Thread(target=accept_loop, args=(listener, remote, local), daemon=True).start()
    return local


def main() -> None:
    path = os.environ.get("CCTV_CAMERA_CONFIG") or str(settings.config_dir() / settings.CAMERAS_FILE)
    config = json.loads(open(path).read())
    port = int(os.environ.get("CCTV_RTSP_PROXY_PORT", "18554"))
    for camera in config.get("cameras", []):
        camera["rtsp_url"] = listen_for(camera["rtsp_url"], port, camera["camera_id"])
        port += 1
        # Детектору — отдельный порт на свой upstream (substream 102). Общий с рекордером
        # порт заставлял оба тянуть основной профиль, а камера отдаёт main лишь одному
        # клиенту: детектор слепнул, а его попытки роняли запись. Камера тянет main+sub
        # одновременно, поэтому разные профили на разных портах не конфликтуют.
        detect = camera.get("detect_rtsp_url")
        if detect:
            camera["detect_rtsp_url"] = listen_for(detect, port, f"{camera['camera_id']}-detect")
            port += 1
    output = os.environ.get("CCTV_PROXY_CAMERA_CONFIG", "/run/cctv/cameras.proxy.json")
    with open(output, "w") as file:
        json.dump(config, file)
    os.chmod(output, 0o600)
    threading.Event().wait()


if __name__ == "__main__":
    main()
