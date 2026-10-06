"""Эмулятор новой камеры Hikvision для тестов активации.

Отвечает так, как отвечала живая G5 (V3) 20.09.2026, и по той же схеме — за
прежнюю прошивку (legacy): `/SDK/activateStatus` без пароля, challenge,
зашифрованный присланным RSA-ключом, расшифровка пароля AES и проверка правил.
После активации — Digest на ISAPI с блокировкой входа после неудач, капризный
`Integrate` (PUT только телом ровно из GET), отдельный стор ONVIF-пользователей,
ONVIF SOAP с WSSE PasswordDigest и RTSP DESCRIBE с Digest по ONVIF-стору.

Режимы сбоя (`fail`): reject — камера отвергает активацию; lost_reply —
активирует, но ответ теряется (500); wrong_password — «OK», но пароль на
камере не тот (вход новым паролем не проходит); no_onvif — Integrate не
принимает PUT.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cctv.engine import hikvision_activation as hik

REALM = "IP Camera(EMU)"
LOCK_AFTER = 5
INTEGRATE_FALSE = ('<?xml version="1.0" encoding="UTF-8"?>\n<Integrate version="2.0">\n'
                   "<CGI>\n<enable>true</enable>\n<certificateType>digest</certificateType>\n</CGI>\n"
                   "<ONVIF>\n<enable>{onvif}</enable>\n<certificateType>digest/wsse</certificateType>\n"
                   "</ONVIF>\n</Integrate>\n")
SDP = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=Media\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class HikEmulator:
    def __init__(self, ip: str, http_port: int, *, protocol: str = "v3", fail: str = "",
                 model: str = "DS-2CD2543G2-IS") -> None:
        self.ip, self.http_port, self.protocol, self.fail, self.model = ip, http_port, protocol, fail, model
        self.rtsp_port = free_port()
        self.activated = False
        self.admin_password: str | None = None
        self.onvif_enabled = False
        self.onvif_users: dict[str, tuple[str, str]] = {}  # имя → (id, пароль)
        self.failed_logins = 0
        self.activation_calls = 0
        self.challenges: list[str] = []
        self.log: list[str] = []
        self._challenge = ""
        self._nonces: set[str] = set()
        self._lock = threading.Lock()

    # --- жизненный цикл -------------------------------------------------
    def start(self) -> "HikEmulator":
        emulator = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                emulator.handle(self, "GET")

            def do_POST(self):
                emulator.handle(self, "POST")

            def do_PUT(self):
                emulator.handle(self, "PUT")

            def log_message(self, *_args):
                pass

        self.http = ThreadingHTTPServer((self.ip, self.http_port), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.rtsp = socket.socket()
        self.rtsp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rtsp.bind((self.ip, self.rtsp_port))
        self.rtsp.listen(8)
        threading.Thread(target=self._rtsp_loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.rtsp.close()

    @property
    def base(self) -> str:
        return f"http://{self.ip}:{self.http_port}"

    @property
    def locked(self) -> bool:
        return self.failed_logins >= LOCK_AFTER

    # --- HTTP ---------------------------------------------------------------
    def handle(self, req: BaseHTTPRequestHandler, method: str) -> None:
        length = int(req.headers.get("content-length") or 0)
        body = req.rfile.read(length).decode() if length else ""
        parts = urllib.parse.urlsplit(req.path)
        path = parts.path
        self.log.append(f"{method} {path}")
        if path == "/SDK/activateStatus" and method == "GET":
            version = "<supportVersion>3</supportVersion>" if self.protocol == "v3" else ""
            return self._send(req, 200, f'<?xml version="1.0"?><ActivateStatus>'
                                        f"<Activated>{str(self.activated).lower()}</Activated>"
                                        f"{version}</ActivateStatus>")
        if path.startswith("/onvif"):
            return self._onvif(req, body)
        if not self.activated:
            return self._activation(req, method, path, body)
        user = self._digest_user(req, method, req.path, self._admin_store())
        if user is None:
            return self._unauthorized(req)
        if path == "/ISAPI/System/deviceInfo" and method == "GET":
            return self._send(req, 200, f"<DeviceInfo><deviceName>IP CAMERA</deviceName>"
                                        f"<model>{self.model}</model>"
                                        f"<serialNumber>{self.model}EMU0001</serialNumber></DeviceInfo>")
        if path == "/ISAPI/System/Network/Integrate":
            if method == "GET":
                return self._send(req, 200, INTEGRATE_FALSE.format(
                    onvif=str(self.onvif_enabled).lower()))
            if self.fail == "no_onvif" or body != INTEGRATE_FALSE.format(onvif="true"):
                return self._status(req, 400, 6, "badXmlFormat")
            self.onvif_enabled = True
            return self._status(req, 200, 1, "OK")
        if path == "/ISAPI/Security/ONVIF/users" and method == "GET":
            users = "".join(f"<User><id>{uid}</id><userName>{name}</userName>"
                            f"<userType>operator</userType></User>"
                            for name, (uid, _pw) in self.onvif_users.items())
            return self._send(req, 200, f"<UserList>{users}</UserList>")
        if path.startswith("/ISAPI/Security/ONVIF/users") and method in ("POST", "PUT"):
            fields = {k.lower(): v for k, v in re.findall(r"<(\w+)>([^<]*)</\1>", body)}
            if hik.password_problem(fields.get("password", ""), fields.get("username", "")):
                return self._status(req, 400, 6, "badParameters")
            self.onvif_users[fields["username"]] = (fields.get("id", "1"), fields["password"])
            return self._status(req, 200, 1, "OK")
        return self._send(req, 404, "<ResponseStatus><statusCode>4</statusCode></ResponseStatus>")

    def _activation(self, req, method: str, path: str, body: str) -> None:
        if self.protocol == "v3" and path == "/ISAPI/System/activate/GetChallengeV3":
            data = json.loads(body or "{}")
            n = int(base64.b64decode(data.get("publicKey", "")).decode() or "0", 16)
            if n.bit_length() != hik.V3_KEY_BITS:
                return self._status(req, 400, 6, "Invalid JSON Content", as_json=True)
            return self._send(req, 200, json.dumps({"challenge": self._new_challenge(n)}),
                              "application/json")
        if self.protocol == "v3" and path == "/ISAPI/System/activate/StartActivateV3":
            data = json.loads(body or "{}")
            return self._finish(req, data.get("password", ""), bytes.fromhex(data.get("iv", "")),
                                as_json=True)
        if self.protocol == "legacy" and path == "/ISAPI/Security/challenge" and method == "POST":
            key = re.search(r"<key>([^<]+)</key>", body)
            n = int(base64.b64decode(key.group(1)).decode(), 16) if key else 0
            if n.bit_length() < 1024:
                return self._status(req, 400, 6, "badParameters")
            return self._send(req, 200, f"<Challenge><key>{self._new_challenge(n)}</key></Challenge>")
        if self.protocol == "legacy" and path == "/ISAPI/System/activate" and method == "PUT":
            match = re.search(r"<password>([^<]+)</password>", body)
            return self._finish(req, match.group(1) if match else "", None, as_json=False)
        # V3-камера на старый PUT отвечает так, как отвечала живая G5.
        return self._status(req, 400, 6, "badParameters", as_json=False)

    def _new_challenge(self, n: int) -> str:
        self._challenge = secrets.token_hex(16)
        self.challenges.append(self._challenge)
        cipher = hik.rsa_encrypt_pkcs1(n, self._challenge.encode())
        return base64.b64encode(format(cipher, "x").encode()).decode()

    def _finish(self, req, encrypted: str, iv: bytes | None, *, as_json: bool) -> None:
        self.activation_calls += 1
        try:
            password = hik.decrypt_password(self._challenge, encrypted, iv)
        except Exception:
            return self._status(req, 400, 6, "badParameters", as_json=as_json)
        if self.fail == "reject" or hik.password_problem(password):
            return self._status(req, 400, 6, "badParameters", as_json=as_json)
        self.activated = True
        self.admin_password = password + "!" if self.fail == "wrong_password" else password
        if self.fail == "lost_reply":
            return self._send(req, 500, "")
        return self._status(req, 200, 1, "OK", as_json=as_json)

    # --- авторизация ----------------------------------------------------------
    def _admin_store(self) -> dict[str, str]:
        return {"admin": self.admin_password} if self.admin_password else {}

    def _onvif_store(self) -> dict[str, str]:
        return {name: pw for name, (_uid, pw) in self.onvif_users.items()}

    def challenge_header(self) -> str:
        nonce = secrets.token_hex(8)
        self._nonces.add(nonce)
        return f'Digest realm="{REALM}", qop="auth", nonce="{nonce}"'

    def check_digest(self, header: str, method: str, uri: str, store: dict[str, str]) -> str | None:
        """Имя пользователя при верном Digest; неверный — счётчик неудач (блокировка)."""
        if not header.lower().startswith("digest"):
            return None
        attrs = {k.lower(): v for k, v in re.findall(r'(\w+)="?([^",]+)"?', header)}
        with self._lock:
            if self.locked:
                return None
            user = attrs.get("username", "")
            password = store.get(user)
            ok = False
            if password is not None and attrs.get("nonce") in self._nonces:
                ha1 = _md5(f"{user}:{REALM}:{password}")
                ha2 = _md5(f"{method}:{attrs.get('uri', '')}")
                expected = _md5(f"{ha1}:{attrs['nonce']}:{attrs.get('nc', '')}:"
                                f"{attrs.get('cnonce', '')}:auth:{ha2}")
                ok = expected == attrs.get("response") and attrs.get("uri") == uri
            if not ok:
                self.failed_logins += 1
                return None
            return user

    def _digest_user(self, req, method: str, uri: str, store: dict[str, str]) -> str | None:
        header = req.headers.get("authorization") or ""
        return self.check_digest(header, method, uri, store) if header else None

    def _unauthorized(self, req) -> None:
        data = b""
        req.send_response(401)
        req.send_header("WWW-Authenticate", self.challenge_header())
        req.send_header("content-length", "0")
        req.end_headers()
        req.wfile.write(data)

    # --- ONVIF ---------------------------------------------------------------
    def _wsse_ok(self, body: str) -> bool:
        user = re.search(r"<Username>([^<]*)</Username>", body)
        digest = re.search(r"<Password[^>]*>([^<]*)</Password>", body)
        nonce = re.search(r"<Nonce>([^<]*)</Nonce>", body)
        created = re.search(r"<Created[^>]*>([^<]*)</Created>", body)
        if not (user and digest and nonce and created):
            return False
        password = self._onvif_store().get(user.group(1))
        if password is None:
            return False
        expected = base64.b64encode(hashlib.sha1(
            base64.b64decode(nonce.group(1)) + created.group(1).encode() + password.encode()
        ).digest()).decode()
        return expected == digest.group(1)

    def _onvif(self, req, body: str) -> None:
        if not self.onvif_enabled:
            return self._send(req, 404, "")
        if "GetSystemDateAndTime" in body:
            return self._soap(req, "<tds:GetSystemDateAndTimeResponse/>")
        if not self._wsse_ok(body):
            return self._soap(req, "<s:Fault><s:Reason><s:Text>Sender not Authorized</s:Text>"
                                   "</s:Reason></s:Fault>", status=400)
        if "GetDeviceInformation" in body:
            return self._soap(req, "<tds:GetDeviceInformationResponse>"
                                   "<tds:Manufacturer>HIKVISION</tds:Manufacturer>"
                                   f"<tds:Model>{self.model}</tds:Model>"
                                   "<tds:SerialNumber>EMU0001</tds:SerialNumber>"
                                   "</tds:GetDeviceInformationResponse>")
        if "GetCapabilities" in body:
            return self._soap(req, "<tds:GetCapabilitiesResponse><tds:Capabilities>"
                                   f"<tt:Media><tt:XAddr>http://192.0.2.64/onvif/Media</tt:XAddr>"
                                   "</tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>")
        if "GetProfiles" in body:
            profiles = "".join(
                f'<trt:Profiles token="Profile_{i}"><tt:Name>{name}</tt:Name>'
                f"<tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>"
                f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
                f"<tt:RateControl><tt:FrameRateLimit>{fps}</tt:FrameRateLimit></tt:RateControl>"
                f"</tt:VideoEncoderConfiguration></trt:Profiles>"
                for i, name, w, h, fps in ((1, "mainStream", 2688, 1520, 25),
                                           (2, "subStream", 1280, 720, 12)))
            return self._soap(req, f"<trt:GetProfilesResponse>{profiles}</trt:GetProfilesResponse>")
        if "GetStreamUri" in body:
            index = "2" if "Profile_2" in body else "1"
            # Как живая камера: адрес с её «заводским» хостом — мост переписывает.
            uri = (f"rtsp://192.0.2.64:{self.rtsp_port}/Streaming/Channels/10{index}"
                   f"?transportmode=unicast&amp;profile=Profile_{index}")
            return self._soap(req, f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri>"
                                   "</trt:MediaUri></trt:GetStreamUriResponse>")
        if "GetSnapshotUri" in body:
            uri = f"http://192.0.2.64:{self.http_port}/onvif-http/snapshot?Profile_1"
            return self._soap(req, f"<trt:GetSnapshotUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri>"
                                   "</trt:MediaUri></trt:GetSnapshotUriResponse>")
        return self._soap(req, "<s:Fault><s:Reason><s:Text>not supported</s:Text></s:Reason></s:Fault>",
                          status=400)

    def _soap(self, req, inner: str, status: int = 200) -> None:
        envelope = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
                    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
                    'xmlns:tt="http://www.onvif.org/ver10/schema">'
                    f"<s:Body>{inner}</s:Body></s:Envelope>")
        self._send(req, status, envelope, "application/soap+xml")

    # --- RTSP ------------------------------------------------------------------
    def _rtsp_loop(self) -> None:
        while True:
            try:
                conn, _ = self.rtsp.accept()
            except OSError:
                return
            threading.Thread(target=self._rtsp_conn, args=(conn,), daemon=True).start()

    def _rtsp_conn(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            for _ in range(3):
                try:
                    data = conn.recv(8192).decode("utf-8", "replace")
                except OSError:
                    return
                if not data:
                    return
                first = data.split("\r\n", 1)[0].split()
                cseq = re.search(r"(?im)^CSeq:\s*(\d+)", data)
                seq = cseq.group(1) if cseq else "1"
                auth = re.search(r"(?im)^Authorization:\s*([^\r\n]+)", data)
                target = first[1] if len(first) > 1 else ""
                # Как у Hikvision: `?profile=` маршрутизирует на ONVIF-стор.
                store = self._onvif_store() if "profile=" in target else self._admin_store()
                if auth and self.check_digest(auth.group(1), "DESCRIBE", target, store):
                    conn.sendall((f"RTSP/1.0 200 OK\r\nCSeq: {seq}\r\nContent-Type: application/sdp\r\n"
                                  f"Content-Length: {len(SDP)}\r\n\r\n{SDP}").encode())
                    return
                conn.sendall((f"RTSP/1.0 401 Unauthorized\r\nCSeq: {seq}\r\n"
                              f"WWW-Authenticate: {self.challenge_header()}\r\n\r\n").encode())

    # --- ответы -----------------------------------------------------------------
    def _status(self, req, http_code: int, code: int, text: str, *, as_json: bool = False) -> None:
        if as_json:
            return self._send(req, http_code, json.dumps({"statusCode": code, "statusString": text}),
                              "application/json")
        self._send(req, http_code, f'<?xml version="1.0"?><ResponseStatus><statusCode>{code}</statusCode>'
                                   f"<statusString>{text}</statusString></ResponseStatus>")

    def _send(self, req, code: int, text: str, ctype: str = "application/xml") -> None:
        data = text.encode()
        req.send_response(code)
        req.send_header("content-type", ctype)
        req.send_header("content-length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)
