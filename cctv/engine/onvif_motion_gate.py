"""ONVIF PullPoint motion gate for CCTV Bridge.

YOLO inference is the expensive part of person detection (~80 ms/frame on this
host); the camera's own motion analytics is nearly free and already reachable
by ONVIF (confirmed live on two Hikvision cameras 04.09.2026 — see
``docs/why-server-side-detection.md``). This module turns that cheap signal
into a gate: frames still get decoded and the recorder buffer cursor still
advances, but the classifier only runs while the camera itself reports
motion. If the ONVIF subscription cannot be established or dies, the gate
reports itself open — a broken second protocol must not blind the detector
that was already working before this feature existed.
"""
from __future__ import annotations

import base64
import collections
import datetime
import hashlib
import re
import secrets
import threading
import time
import urllib.request

_MOTION_TOPICS = ("MotionAlarm", "CellMotionDetector", "FieldDetector")
_NOTIFICATION_SPLIT = re.compile(r"<[\w]+:NotificationMessage>")
_TRUE_ITEM = re.compile(r'Name="Is(?:Motion|Inside)"\s+Value="true"')
_STATE_ITEM = re.compile(r'Name="Is(?:Motion|Inside)"\s+Value="(true|false)"')
_KEY_ITEM = re.compile(r'Name="(Rule|ObjectId|VideoSourceConfigurationToken)"\s+Value="([^"]*)"')
_PROPERTY_OPERATION = re.compile(r'PropertyOperation="(\w+)"')
_SUBSCRIPTION_ADDRESS = re.compile(r"<[^>]*Address[^>]*>(http[^<]+)<")


def _wsse_header(user: str, password: str) -> str:
    nonce = secrets.token_bytes(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<UsernameToken><Username>{user}</Username>"
        f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
        f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security>"
    )


def soap_call(url: str, user: str, password: str, body: str, extra_header: str = "", timeout: float = 15) -> str:
    """Один WS-Security SOAP-запрос; вынесена, чтобы тест мог подменить сеть."""
    envelope = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:wsa="http://www.w3.org/2005/08/addressing">'
        f"<s:Header>{extra_header}{_wsse_header(user, password)}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    )
    request = urllib.request.Request(url, envelope.encode(), {"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def has_active_motion(pull_response: str, topics: tuple[str, ...] = _MOTION_TOPICS) -> bool:
    """True, если хотя бы одно NotificationMessage несёт активный топик из ``topics``.

    Каждое сообщение разбирается отдельно: PullMessages пачками возвращает и
    Motion, и Tamper, и Relay в одном ответе, и глобальный поиск по всему
    тексту принял бы Motion-топик одного сообщения за True другого.

    ``topics`` сужает, что считать сигналом: гейт по людям слушает только
    ``FieldDetector`` (вторжение с целью human на Hikvision G2/G5), потому что
    ``MotionAlarm`` той же камеры ночью срабатывает раз в секунду на куст в ИК.
    """
    for block in _NOTIFICATION_SPLIT.split(pull_response)[1:]:
        if any(topic in block for topic in topics) and _TRUE_ITEM.search(block):
            return True
    return False


def motion_states(pull_response: str, topics: tuple[str, ...] = _MOTION_TOPICS) -> list[bool]:
    """Смены состояния (true/false) по топикам ``topics`` в порядке прихода.

    В отличие от ``has_active_motion`` видит и ``false``: камера держит цель
    «внутри» десятки секунд (active…inactive, p50 47 с, max 264 с), а повторный
    true не шлёт, так что по одним true состояние восстановить нельзя.
    """
    states = []
    for block in _NOTIFICATION_SPLIT.split(pull_response)[1:]:
        if any(topic in block for topic in topics):
            found = _STATE_ITEM.search(block)
            if found:
                states.append(found.group(1) == "true")
    return states


def state_messages(pull_response: str, topics: tuple[str, ...] = _MOTION_TOPICS) -> list[tuple[str, bool, str]]:
    """Как ``motion_states``, но с ключом цели: (правило/ObjectId, активно, операция).

    FieldDetector Hikvision шлёт IsInside отдельно на каждую цель (ObjectId) —
    две цели в кадре дают два true в одну секунду. Без ключа inactive одной цели
    закрыл бы интервал, пока вторая ещё в зоне.
    """
    messages = []
    for block in _NOTIFICATION_SPLIT.split(pull_response)[1:]:
        if any(topic in block for topic in topics):
            found = _STATE_ITEM.search(block)
            if found:
                key = "/".join(value for _, value in _KEY_ITEM.findall(block))
                operation = _PROPERTY_OPERATION.search(block)
                messages.append((key, found.group(1) == "true", operation.group(1) if operation else ""))
    return messages


class OnvifMotionGate:
    """Журнал моментов, когда камера сама видела движение.

    Гейт отвечает на два разных вопроса, и это принципиально:

    * ``is_open()`` — «шевелится ли ПРЯМО СЕЙЧАС» (для живого RTSP-кадра);
    * ``active_between()`` — «шевелилось ли В ТО ВРЕМЯ, когда снят ЭТОТ кадр».

    Детектор людей читает не живой поток, а закрытые сегменты буфера recorder'а,
    то есть кадр отстаёт от стенных часов на 6–11 с. Сравнивать такой кадр с
    «сейчас» — значит регулярно промахиваться мимо собственного события, поэтому
    для буфера используется только ``active_between``.

    Пока подписка не поднялась, оба ответа — True: сломанный второй протокол не
    имеет права ослеплять детектор, который работал и без него.
    """

    def __init__(self, camera_id: str, events_url: str, user: str, password: str,
                 hold_seconds: float = 20.0, remember_seconds: float = 900.0,
                 topics: tuple[str, ...] = _MOTION_TOPICS, label: str = "onvif_gate",
                 state_cap_seconds: float = 300.0) -> None:
        self.camera_id = camera_id
        self.events_url = events_url
        self.user, self.password = user, password
        self.hold_seconds = hold_seconds
        self.remember_seconds = remember_seconds
        # Какие топики считать сигналом и как подписывать строки журнала: один
        # класс обслуживает и гейт по движению (VMD), и гейт по людям
        # (FieldDetector) — это разные подписки с разной ценой ошибки.
        self.topics, self.label = topics, label
        self._lock = threading.Lock()
        self._motion_times: collections.deque[float] = collections.deque()
        self._last_motion_at = 0.0
        self._motion_count = 0
        self._healthy = False
        # Журнал смен состояния (момент, активно) — для телеметрии «по состоянию»,
        # решений гейта не касается.
        self._states: collections.deque[tuple[float, bool]] = collections.deque()
        # Цели «в зоне» (ключ → момент последнего true). inactive по ONVIF приходит
        # не всегда: 03–04.10.2026 у прода интервал не закрылся ни разу за 30+ ч,
        # хотя Initialized false при переподписке приходил. Поэтому цель без
        # подтверждения дольше state_cap_seconds считается ушедшей (камера держит
        # active…inactive p50 47 с, max 264 с — по ISAPI 28.09–02.10).
        self._active_keys: dict[str, float] = {}
        self.state_cap_seconds = state_cap_seconds

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @property
    def motion_count(self) -> int:
        """Сколько motion-сигналов пришло за всё время — прямой признак того,
        что VMD камеры вообще армирован, а не выключен в её настройках."""
        with self._lock:
            return self._motion_count

    def note_motion(self, at: float | None = None) -> None:
        """Запомнить момент движения; вынесена ради тестов и keepalive-эскалации."""
        moment = time.time() if at is None else at
        with self._lock:
            self._motion_times.append(moment)
            self._last_motion_at = max(self._last_motion_at, moment)
            self._motion_count += 1
            self._prune(moment)

    def note_state(self, active: bool, at: float | None = None, key: str = "") -> None:
        """Смена состояния цели ``key``; в журнал идёт агрегат «есть ли кто в зоне».

        Каждый true пишется (он продлевает интервал), false — только когда зона
        опустела. Пустой ключ — старое поведение без ключей (тесты, VMD).
        """
        moment = time.time() if at is None else at
        with self._lock:
            stale = moment - self.state_cap_seconds
            for old_key in [k for k, seen in self._active_keys.items() if seen < stale]:
                del self._active_keys[old_key]
            if active:
                self._active_keys[key] = moment
                self._states.append((moment, True))
            else:
                self._active_keys.pop(key, None)
                if not self._active_keys:
                    self._states.append((moment, False))
            cutoff = moment - self.remember_seconds
            # Последнюю запись до границы оставляем: она задаёт состояние на границе.
            while len(self._states) > 1 and self._states[1][0] < cutoff:
                self._states.popleft()

    def reset_state(self, at: float | None = None) -> None:
        """Новая подписка: камера заново пришлёт Initialized по текущим целям,
        а цели старой подписки, не дождавшиеся inactive, забываются."""
        moment = time.time() if at is None else at
        with self._lock:
            if self._active_keys:
                self._active_keys.clear()
                self._states.append((moment, False))

    def state_active_near(self, shot: float, margin: float = 60.0) -> bool:
        """Был ли кадр внутри интервала active…inactive камеры (±margin).

        Интервал без inactive тянется не дальше state_cap_seconds после
        последнего true, а не «до сейчас»: иначе одно потерянное inactive
        превращает всю дальнейшую телеметрию в 100 % «камера видит».
        Пока подписка не поднялась — True, как и в остальных ответах гейта.
        """
        with self._lock:
            if not self._healthy:
                return True
            start = last_true = None
            for moment, active in self._states:
                if start is not None and moment - last_true > self.state_cap_seconds:
                    if start - margin <= shot <= last_true + self.state_cap_seconds + margin:
                        return True
                    start = None
                if active:
                    if start is None:
                        start = moment
                    last_true = moment
                elif start is not None:
                    if start - margin <= shot <= moment + margin:
                        return True
                    start = None
            if start is None:
                return False
            return start - margin <= shot <= last_true + self.state_cap_seconds + margin

    def _prune(self, reference: float) -> None:
        cutoff = reference - self.remember_seconds
        while self._motion_times and self._motion_times[0] < cutoff:
            self._motion_times.popleft()

    def is_open(self) -> bool:
        with self._lock:
            if not self._healthy:
                return True
            return time.time() - self._last_motion_at < self.hold_seconds

    def active_between(self, start: float, end: float) -> bool:
        """Было ли движение в окне [start, end] — окно кадра, а не «сейчас»."""
        with self._lock:
            if not self._healthy:
                return True
            return any(start <= moment <= end for moment in self._motion_times)

    def _run(self) -> None:
        while True:
            try:
                self._subscribe_and_pull()
            except Exception as error:
                with self._lock:
                    self._healthy = False
                print(f"{self.label}_error camera={self.camera_id} reason={error}", flush=True)
                time.sleep(10)

    def _subscribe_and_pull(self) -> None:
        body = (
            '<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl">'
            "<InitialTerminationTime>PT120S</InitialTerminationTime></CreatePullPointSubscription>"
        )
        response = soap_call(self.events_url, self.user, self.password, body)
        match = _SUBSCRIPTION_ADDRESS.search(response)
        if not match:
            raise RuntimeError("no_subscription_address")
        address = match.group(1)
        self.reset_state()
        with self._lock:
            self._healthy = True
        print(f"{self.label}_subscribed camera={self.camera_id} topics={','.join(self.topics)}", flush=True)
        while True:
            header = (
                f'<wsa:To s:mustUnderstand="1">{address}</wsa:To>'
                '<wsa:Action s:mustUnderstand="1">'
                "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest</wsa:Action>"
            )
            pull_body = (
                '<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">'
                "<Timeout>PT30S</Timeout><MessageLimit>50</MessageLimit></PullMessages>"
            )
            response = soap_call(address, self.user, self.password, pull_body, header, timeout=40)
            for key, active, operation in state_messages(response, self.topics):
                self.note_state(active, key=key)
                print(f"{self.label}_state camera={self.camera_id} key={key} active={int(active)} op={operation}", flush=True)
            if has_active_motion(response, self.topics):
                self.note_motion()
                print(f"{self.label}_motion camera={self.camera_id} total={self.motion_count}", flush=True)
