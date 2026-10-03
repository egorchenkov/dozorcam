#!/usr/bin/env bash
# Hermetic smoke/contract test: no camera, RTSP credential, or persistent key needed.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
py="${CCTV_TEST_PYTHON:-python3}"
work="$(mktemp -d)"
cleanup() { [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true; rm -rf "$work"; }
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/ca.key" -out "$work/ca.crt" -subj /CN=test-ca -days 1 >/dev/null 2>&1
for name in server client; do
  openssl req -newkey rsa:2048 -nodes -keyout "$work/$name.key" -out "$work/$name.csr" -subj "/CN=$name" >/dev/null 2>&1
  openssl x509 -req -in "$work/$name.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -out "$work/$name.crt" -days 1 >/dev/null 2>&1
done
ffmpeg -y -loglevel error -f lavfi -i testsrc=size=320x240:rate=15 -t 8 -pix_fmt yuv420p "$work/source.mp4"
mkdir -p "$work/store/buffer/city"
ffmpeg -y -loglevel error -i "$work/source.mp4" -c copy -f segment -segment_time 5 -strftime 1 "$work/store/buffer/city/$(date -u +%Y-%m-%dT%H:%M:%SZ).ts"
cat > "$work/cameras.json" <<JSON
{"cameras":[{"camera_id":"city","title":"Город","site":"test","rtsp_url":"$work/source.mp4"}]}
JSON
PYTHONPATH="$root" "$py" - "$work" <<'PY'
import json, pathlib, sys
from cctv.engine.cctv_bridge import Bridge
p = pathlib.Path(sys.argv[1]); bridge = Bridge(json.loads((p / "cameras.json").read_text()), p / "store", "https://localhost:9443")
camera = bridge.cameras["city"]
frame, _ = bridge.snapshot(camera)
segment = next((p / "store/buffer/city").glob("*.ts"))
clip, _ = bridge.clip(camera, segment.stem.replace("Z", "+00:00"))
assert frame[:2] == b"\xff\xd8" and len(clip) > 1000
PY
# Регрессия TZ: сегменты пишет реальный segment_command() при неUTC-поясе, а центр окна
# берётся из настоящего UTC-времени — так ловится сдвиг имени сегмента, который прежний
# тест сокращал, вычисляя центр из самого имени файла.
TZ="Asia/Tbilisi" PYTHONPATH="$root" "$py" - "$work" <<'TZCHECK'
import datetime, json, pathlib, subprocess, sys
from cctv.engine.cctv_bridge import Bridge
from cctv.engine.cctv_pipeline import segment_command

work = pathlib.Path(sys.argv[1])
target = work / "tzstore/buffer/city"
target.mkdir(parents=True)
command, env = segment_command(str(work / "source.mp4"), target)
subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, env=env, check=True)
names = sorted(p.stem for p in target.glob("*.ts"))
assert names, "recorder wrote no segments"
stamp = datetime.datetime.strptime(names[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
skew = abs((datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds())
assert skew < 120, f"segment name is not UTC: skew {skew:.0f}s"

bridge = Bridge(json.loads((work / "cameras.json").read_text()), work / "tzstore", "https://localhost")
centre = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
clip, _ = bridge.clip(bridge.cameras["city"], centre)
assert len(clip) > 1000, "clip not assembled from live-named buffer"

# Звук 23.09.2026: G.711 с камеры доходит до MP4 клипа как AAC, data-дорожка отсекается.
source = work / "source-audio.mkv"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "8", "-pix_fmt", "yuv420p",
                "-c:a", "pcm_mulaw", str(source)], check=True)
audio_target = work / "audiostore/buffer/city"
audio_target.mkdir(parents=True)
command, env = segment_command(str(source), audio_target)
subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, env=env, check=True)
audio_bridge = Bridge(json.loads((work / "cameras.json").read_text()), work / "audiostore", "https://localhost")
clip, _ = audio_bridge.clip(audio_bridge.cameras["city"], centre)
(work / "audio-clip.mp4").write_bytes(clip)
streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                          str(work / "audio-clip.mp4")], capture_output=True, text=True, check=True).stdout.split()
assert streams == ["h264", "aac"], f"клип без звука: {streams}"
TZCHECK

# Регрессии 24.08.2026: детектор движения и глубина буфера. Каждая проверка бьёт
# в сам дефект, а не в его следствие.
PYTHONPATH="$root" "$py" - "$work" <<'MOTION'
import datetime, json, pathlib, sys
from cctv.engine import cctv_pipeline as pipeline
from cctv.engine.cctv_bridge import Bridge, BridgeError

work = pathlib.Path(sys.argv[1])

# 1. Шкала score. Прежде сравнивалась доля 0..1 с порогом 8 и движение не срабатывало
#    ни разу: даже полностью разные кадры давали 1.0 < 8.
if pipeline.cv2 is None:
    # Молчаливый пропуск скрыл бы ровно тот дефект, из-за которого детектор простаивал.
    print("SKIP: проверки детектора — у этого интерпретатора нет cv2", file=sys.stderr)
else:
    numpy = __import__("numpy")
    dark = numpy.zeros((120, 160), dtype="uint8")
    bright = numpy.full((120, 160), 200, dtype="uint8")
    assert pipeline.motion_score(dark, bright) > pipeline.MOTION_THRESHOLD, "движение не поднимает score выше порога"
    # Шум покоя целого substream по живым замерам 29.08.2026: p95 = 0.06 %, max = 0.11 %
    # (прежние ~1.7 % мерились на рваном потоке — там шумел распад картинки, не сцена).
    noisy = dark.copy(); noisy.reshape(-1)[:int(dark.size * 0.0022)] = 200  # 2x измеренного максимума
    assert pipeline.motion_score(dark, noisy) < pipeline.MOTION_THRESHOLD, "шум фона перебивает порог"

    # 2. Кадр ISAPI-fallback уже одноканальный: cvtColor на нём убивал поток детектора.
    assert pipeline.to_gray(dark).ndim == 2
    assert pipeline.to_gray(numpy.zeros((120, 160, 3), dtype="uint8")).ndim == 2

# 3. Глубина буфера. 24 сегмента по 5 с = 2 минуты, и кнопка «Клип вокруг кадра»
#    под уже отправленным фото приходила к пустому окню.
assert pipeline.BUFFER_SEGMENTS * 5 >= 600, "буфер короче 10 минут"
target = work / "prunestore/buffer/city"
target.mkdir(parents=True)
for i in range(pipeline.BUFFER_SEGMENTS + 20):
    (target / f"seg{i:04d}.ts").write_bytes(b"x")
pipeline.prune_buffer(target)
assert len(list(target.glob("*.ts"))) == pipeline.BUFFER_SEGMENTS

# 4. Пустое окно — это clip_window_empty, а не «сервис недоступен»: бот обязан
#    отличить устаревший кадр от отказа Bridge.
bridge = Bridge(json.loads((work / "cameras.json").read_text()), work / "store", "https://localhost")
long_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).isoformat()
try:
    bridge.clip(bridge.cameras["city"], long_ago)
except BridgeError as exc:
    assert exc.code == "clip_window_empty", f"неожиданный код: {exc.code}"
else:
    raise AssertionError("клип собрался из окна вне буфера")

# 5. Пустой сегмент не попадает в окно клипа. Рекордер каждые ~130 с упирается в
#    свой timeout=, обрывает ffmpeg и оставляет сегмент нулевой длины; ещё один
#    всегда пуст, пока пишется. concat на таком файле обрывал клип на середине, а
#    если пустой сегмент оказывался первым в окне — падал целиком, и запрос клипа
#    приходил как unavailable (воспроизведено в рантайме 01.09.2026, 1 из 4).
holes = work / "holestore/buffer/city"
holes.mkdir(parents=True)
real = next((work / "store/buffer/city").glob("*.ts")).read_bytes()
centre = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
name = lambda offset: (centre + datetime.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ") + ".ts"
# Пустой сегмент ПЕРВЫЙ в окне — тот случай, в котором concat падал целиком.
(holes / name(-10)).write_bytes(b"")
for offset in (-5, 0, 5):
    (holes / name(offset)).write_bytes(real)
(holes / name(10)).write_bytes(b"")           # и «пишется прямо сейчас» в хвосте окна
hole_bridge = Bridge(json.loads((work / "cameras.json").read_text()), work / "holestore", "https://localhost")
clip_body, _ = hole_bridge.clip(hole_bridge.cameras["city"], centre.isoformat())
assert len(clip_body) > 1000, "клип не собрался из окна с пустыми сегментами"
assert clip_body[4:8] == b"ftyp", "результат не MP4"

# 6. Снимок не должен звать несуществующий -rw_timeout: с ним ffmpeg выходил сразу
#    и основной RTSP-путь всегда молча уступал ISAPI-fallback.
source = pathlib.Path(pipeline.__file__).with_name("cctv_bridge.py").read_text()
assert '"-rw_timeout"' not in source, "в Bridge вернулась опция -rw_timeout"   # в комментарии она упоминается
MOTION

# Регрессии 24.08.2026 (вторая волна): токены между процессами, свежесть статуса,
# геометрия кадров детектора, перекладка pending-записей.
PYTHONPATH="$root" "$py" - "$work" <<'CROSS'
import json, os, pathlib, sys, time
from cctv.engine import cctv_pipeline as pipeline
from cctv.engine.cctv_bridge import Bridge, SEGMENT_FRESH_SEC

work = pathlib.Path(sys.argv[1])
config = json.loads((work / "cameras.json").read_text())

# 1. Токен, выданный одним процессом (детектор), обязан выкупаться другим (HTTP-сервер).
#    Прежде токены жили в памяти выдавшего процесса и каждое медиа движения кончалось 404.
issuer = Bridge(config, work / "store", "https://localhost")
server = Bridge(config, work / "store", "https://localhost")
token = issuer.issue_token(b"cross-process-media", "image/jpeg")
blob = server.take_token(token)
assert blob is not None and blob.body == b"cross-process-media" and blob.content_type == "image/jpeg", \
    "токен не пережил границу процессов"
assert server.take_token(token) is None, "токен выкупился дважды"
assert server.take_token("../etc/passwd") is None and server.take_token("zz" * 16) is None

# 2. Истёкший блоб не отдаётся и выметается при следующей выдаче.
stale = issuer.issue_token(b"stale", "image/jpeg")
meta = work / "store/media" / f"{stale}.json"
meta.write_text(json.dumps({"content_type": "image/jpeg", "expires_at": time.time() - 1}))
assert issuer.take_token(stale) is None, "истёкший токен отдан"
stale2 = issuer.issue_token(b"stale2", "image/jpeg")
meta2 = work / "store/media" / f"{stale2}.json"
meta2.write_text(json.dumps({"content_type": "image/jpeg", "expires_at": time.time() - 1}))
issuer.issue_token(b"fresh", "image/jpeg")
assert not meta2.exists() and not (work / "store/media" / f"{stale2}.bin").exists(), \
    "истёкший блоб не выметен — раньше такие копились без ограничения"

# 3. Статус камеры online только при свежем сегменте: старый буфер сам себя не чистит,
#    и умерший рекордер прежде выглядел живой камерой.
seg = next((work / "store/buffer/city").glob("*.ts"))
os.utime(seg, (time.time(), time.time()))
assert Bridge(config, work / "store", "https://x").registry()["cameras"][0]["status"] == "online"
old = time.time() - SEGMENT_FRESH_SEC * 10
os.utime(seg, (old, old))
assert Bridge(config, work / "store", "https://x").registry()["cameras"][0]["status"] == "unavailable", \
    "камера online по протухшему буферу"
os.utime(seg, (time.time(), time.time()))

# 4. Кадры разной геометрии не сравниваются: absdiff на них убивал поток детектора.
class Shaped:
    def __init__(self, shape): self.shape = shape
assert not pipeline.frames_comparable(None, Shaped((120, 160)))
assert not pipeline.frames_comparable(Shaped((120, 160)), Shaped((60, 80)))
assert pipeline.frames_comparable(Shaped((120, 160)), Shaped((120, 160)))
CROSS

# Регрессии 01.09.2026 (камера дачи): схема авторизации snapshot-fallback и
# индивидуальный порог движения. Обе проверки герметичны — камера не нужна.
PYTHONPATH="$root" "$py" - "$work" <<'PY'
import dataclasses, http.server, json, pathlib, sys, threading
from cctv.engine import cctv_pipeline as pipeline
from cctv.engine.cctv_bridge import Bridge, Camera

work = pathlib.Path(sys.argv[1])

# 1. Snapshot-fallback обязан пройти Basic. Tantos отдаёт на 401 ДВА заголовка
#    WWW-Authenticate, Basic первым; digest-only opener читает лишь первый,
#    схему не узнаёт и навсегда получает 401 — fallback для такой камеры мёртв.
class Camera401(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if not (self.headers.get("Authorization") or "").startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="QVHTTPSERVICE"')
            self.send_header("WWW-Authenticate", 'Digest realm="QVHTTPSERVICE",qop="auth",nonce="deadbeef"')
            self.send_header("content-length", "0"); self.end_headers(); return
        body = b"\xff\xd8jpeg-body"
        self.send_response(200); self.send_header("content-type", "image/jpeg")
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Camera401)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}/onvif/Snapshot"
bridge = Bridge(json.loads((work / "cameras.json").read_text()), work / "store", "https://localhost")
# RTSP-путь заведомо неработоспособен: проверяем именно fallback.
camera = Camera("basicauth", "t", "t", str(work / "no-such-source.mp4"), url, "u", "p")
body, _ = bridge.snapshot(camera)
server.shutdown()
assert body.startswith(b"\xff\xd8"), "snapshot-fallback не прошёл Basic-авторизацию"

# 2. Порог движения — свойство камеры, а не узла: у дачи шум покоя на порядок
#    выше, чем у городской камеры, и один общий порог означал бы либо ложные события,
#    либо слепоту. Значение из конфигурации обязано доезжать до детектора.
parsed = Bridge._cameras({"cameras": [
    {"camera_id": "a", "title": "a", "rtsp_url": "rtsp://x/1"},
    {"camera_id": "b", "title": "b", "rtsp_url": "rtsp://x/2", "motion_threshold": 2.5}]})
assert parsed[0].motion_threshold is None and parsed[1].motion_threshold == 2.5
pick = lambda cam: cam.motion_threshold if cam.motion_threshold else pipeline.MOTION_THRESHOLD
assert pick(parsed[0]) == pipeline.MOTION_THRESHOLD and pick(parsed[1]) == 2.5, \
    "индивидуальный порог камеры не доезжает до детектора"
source = pathlib.Path(pipeline.__file__).read_text()
assert "score >= threshold" in source, "детектор снова сравнивает с общим порогом"
PY

# Свободный порт берём у ядра: тест не должен спорить с запущенным мостом за порт.
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export CCTV_CAMERA_CONFIG="$work/cameras.json" CCTV_STORAGE_ROOT="$work/store" CCTV_PUBLIC_URL="https://localhost:$port" CCTV_BIND=127.0.0.1 CCTV_PORT="$port"
# Режим по умолчанию — loopback без TLS: тот же контракт открытым http.
PYTHONPATH="$root" "$py" -m cctv.engine.cctv_bridge & pid=$!
for _ in {1..20}; do curl -s --connect-timeout 1 http://127.0.0.1:$port/v1/cameras >/dev/null 2>&1 && break; sleep .1; done
response="$(curl -s http://127.0.0.1:$port/v1/cameras)"
[[ "$response" == *'"camera_id":"city"'* && "$response" != *source.mp4* ]]
kill "$pid"; wait "$pid" 2>/dev/null || true; pid=
# Открытый http за пределы loopback — отказ старта, а не тихая дыра.
if CCTV_BIND=0.0.0.0 PYTHONPATH="$root" "$py" -m cctv.engine.cctv_bridge >/dev/null 2>&1; then
  echo "FAIL: bridge started on 0.0.0.0 without TLS"; exit 1
fi
# CCTV_INTERNAL_TLS=1 без сертификатов — тоже отказ.
if CCTV_INTERNAL_TLS=1 PYTHONPATH="$root" "$py" -m cctv.engine.cctv_bridge >/dev/null 2>&1; then
  echo "FAIL: bridge started with CCTV_INTERNAL_TLS=1 and no certificates"; exit 1
fi

export CCTV_INTERNAL_TLS=1
export CCTV_SERVER_CERT="$work/server.crt" CCTV_SERVER_KEY="$work/server.key" CCTV_CLIENT_CA="$work/ca.crt"
PYTHONPATH="$root" "$py" -m cctv.engine.cctv_bridge & pid=$!
for _ in {1..20}; do curl -sk --connect-timeout 1 https://localhost:$port/v1/cameras >/dev/null 2>&1 && break; sleep .1; done
if curl -sk --connect-timeout 2 https://localhost:$port/v1/cameras >/dev/null 2>&1; then echo "FAIL: accepted client without mTLS"; exit 1; fi
response="$(curl -sk --cert "$work/client.crt" --key "$work/client.key" --cacert "$work/ca.crt" https://localhost:$port/v1/cameras)"
[[ "$response" == *'"camera_id":"city"'* && "$response" != *source.mp4* ]]
curl -sk --cert "$work/client.crt" --key "$work/client.key" --cacert "$work/ca.crt" -H 'content-type: application/json' -d '{"request_id":"11111111-1111-1111-1111-111111111111","camera_id":"city","kind":"snapshot","requested_at":"2026-08-24T00:00:00Z"}' https://localhost:$port/v1/media-requests | grep -q 'accepted'
curl -sk --cert "$work/client.crt" --key "$work/client.key" --cacert "$work/ca.crt" -H 'content-type: application/json' -d '{"request_id":"11111111-1111-1111-1111-111111111111","camera_id":"city","kind":"snapshot","requested_at":"2026-08-24T00:00:00Z"}' https://localhost:$port/v1/media-requests | grep -q 'accepted'
"$py" -m py_compile "$root/cctv/engine/cctv_bridge.py" "$root/cctv/engine/cctv_pipeline.py"
echo 'PASS: snapshot+event clip, snapshot Basic-auth fallback, per-camera motion threshold, UTC segment naming + clip window, motion score scale, grayscale fallback, buffer depth, clip_window_empty, empty segment skipped in clip window, cross-process media tokens, token expiry+sweep, registry freshness, frame geometry guard, loopback http, non-loopback refusal, mTLS rejection, registry redaction, request idempotency, Python syntax'
