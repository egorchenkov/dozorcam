#!/usr/bin/env bash
# Замер ресурсов движка Dozorcam на этой машине: N синтетических камер, лимиты docker.
#
#   scripts/sizing-bench.sh [-n камер] [-s quiet|busy] [-c cpus] [-m память] [-w прогрев_с]
#                           [-d замер_с] [-i образ] [-o results.jsonl]
#
# Камеры — mediamtx на loopback (порт 38680): основной поток 1920x1080 15 к/с H.264
# 4 Мбит/с CBR и детекторный 640x360 25 к/с 512 кбит/с — как у типичной 2 Мп камеры.
# quiet — неподвижная сцена (гейт движения закрыт, YOLO не зовётся: обычная ночь и день),
# busy — движение во всём кадре без пауз (YOLO на каждом кадре выборки: худший случай).
# Движок — тот же образ и те же флаги, что в compose.yml (read-only, cap_drop ALL, tmpfs
# буфера), плюс --cpus/--memory. Бот не нужен: события уходят в пустой порт.
#
# Пишет одну строку JSON на прогон: среднее ядер за окно замера (cpu.stat cgroup),
# память cgroup (anon — процессы, shmem — буфер в tmpfs, peak), буфер на камеру за минуту,
# OOM, отставание детектора и падения рекордера по журналу. Порты 18890/18891/38654/38680
# не должны быть заняты; прод и стенд рядом не трогает. CCTV_BENCH_BUFFER_TMPFS — размер tmpfs
# буфера (по умолчанию 4g; меньше — проверка переполнения), CCTV_BENCH_LOG — копия журнала.
set -euo pipefail

N=1 SCENE=quiet CPUS=2 MEM=4g WARM=60 DUR=120 OUT=
IMAGE=${CCTV_BENCH_IMAGE:-ghcr.io/egorchenkov/dozorcam:latest}
MTX_IMAGE=bluenviron/mediamtx:1.21.1-ffmpeg
while getopts n:s:c:m:w:d:i:o: opt; do
  case $opt in
    n) N=$OPTARG ;; s) SCENE=$OPTARG ;; c) CPUS=$OPTARG ;; m) MEM=$OPTARG ;;
    w) WARM=$OPTARG ;; d) DUR=$OPTARG ;; i) IMAGE=$OPTARG ;; o) OUT=$OPTARG ;;
    *) sed -n '2,4p' "$0" >&2; exit 64 ;;
  esac
done
case $SCENE in quiet|busy) ;; *) echo "scene: quiet|busy" >&2; exit 64 ;; esac

PFX=dzbench
MTX_PORT=38680
WORK=$(mktemp -d)
cleanup() {
  docker rm -fv "$PFX-engine" "$PFX-mtx" >/dev/null 2>&1 || true
  docker volume rm "$PFX-state" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
cleanup_quiet() { docker rm -fv "$PFX-engine" "$PFX-mtx" >/dev/null 2>&1 || true; }
cleanup_quiet

# --- видео: один файл на поток, все камеры крутят его по кругу (-c copy) ---------------
mkdir -p "$WORK/media" "$WORK/config"
chmod 0755 "$WORK" "$WORK/media" "$WORK/config"
src() {  # $1 — размер, $2 — к/с
  if [ "$SCENE" = busy ]; then echo "testsrc2=s=$1:r=$2"; else echo "testsrc2=s=$1:r=$2,trim=end_frame=1,loop=loop=-1:size=1"; fi
}
enc() {  # $1 — выход, $2 — размер, $3 — к/с, $4 — битрейт
  docker run --rm --user "$(id -u):$(id -g)" -v "$WORK/media:/media" --entrypoint ffmpeg "$MTX_IMAGE" -nostdin -v error -y \
    -f lavfi -i "$(src "$2" "$3")" -t 30 -c:v libx264 -preset veryfast -profile:v main -bf 0 \
    -g $(($3 * 2)) -keyint_min $(($3 * 2)) -sc_threshold 0 -b:v "$4" -minrate "$4" -maxrate "$4" \
    -bufsize "$4" -x264-params nal-hrd=cbr -pix_fmt yuv420p -movflags +faststart "/media/$1"
}
enc main.mp4 1920x1080 15 4M
enc detect.mp4 640x360 25 512k
chmod 0644 "$WORK/media/"*.mp4

{
  cat <<YAML
logLevel: warn
api: no
metrics: no
pprof: no
playback: no
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
rtspTransports: [tcp]
rtspAddress: 127.0.0.1:$MTX_PORT
rtspAuthMethods: [digest]
authInternalUsers:
  - user: any
    ips: ["127.0.0.1/32"]
    permissions: [{action: publish}]
  - user: bench
    pass: bench
    ips: ["127.0.0.1/32"]
    permissions: [{action: read}]
pathDefaults:
  runOnInitRestart: yes
paths:
YAML
  for i in $(seq "$N"); do
    for p in "cam$i:main" "cam$i-detect:detect"; do
      # $RTSP_PORT и $MTX_PATH подставляет сам mediamtx, а не shell.
      # shellcheck disable=SC2016
      printf '  %s:\n    runOnInit: ffmpeg -nostdin -loglevel error -re -stream_loop -1 -i /media/%s.mp4 -c copy -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH\n' "${p%%:*}" "${p#*:}"
    done
  done
} > "$WORK/mediamtx.yml"
chmod 0644 "$WORK/mediamtx.yml"

python3 - "$N" "$MTX_PORT" > "$WORK/config/cameras.json" <<'PY'
import json, sys
n, port = int(sys.argv[1]), sys.argv[2]
base = f"rtsp://bench:bench@127.0.0.1:{port}"  # прокси движка требует логин, как у камеры
print(json.dumps({"cameras": [{
    "camera_id": f"cam{i}", "title": f"Bench {i}", "site": "Bench",
    "rtsp_url": f"{base}/cam{i}", "detect_rtsp_url": f"{base}/cam{i}-detect",
    "person_detection": True, "detect_substream": True} for i in range(1, n + 1)]}, indent=1))
PY
chmod 0644 "$WORK/config/cameras.json"

docker run -d --name "$PFX-mtx" --network host --read-only --cap-drop ALL --init \
  -v "$WORK/mediamtx.yml:/mediamtx.yml:ro" -v "$WORK/media:/media:ro" --tmpfs /tmp:size=16m \
  "$MTX_IMAGE" >/dev/null
for _ in $(seq 30); do
  docker run --rm --network host --entrypoint ffprobe "$MTX_IMAGE" -v error -rtsp_transport tcp \
    -show_entries stream=width -of csv=p=0 "rtsp://bench:bench@127.0.0.1:$MTX_PORT/cam$N-detect" >/dev/null 2>&1 && break
  sleep 1
done

# --- движок: флаги compose.yml + лимиты ------------------------------------------------
docker run -d --name "$PFX-engine" --network host --init --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --cpus "$CPUS" --memory "$MEM" --memory-swap "$MEM" \
  -e CCTV_PORT=18890 -e CCTV_EVENTS_URL=http://127.0.0.1:18891/v1/events \
  -e CCTV_RTSP_PROXY_PORT=38654 -e CCTV_HUMAN_GATE_MODE=shadow -e CCTV_LANG=en \
  -e CCTV_BUFFER_MAX_BYTES=419430400 -e CCTV_MAX_TOTAL_BYTES=5368709120 \
  -e CCTV_STORAGE_BUDGET_BYTES=5368709120 -e CCTV_MIN_FREE_BYTES=67108864 \
  -v "$WORK/config:/etc/cctv:ro" -v "$PFX-state:/var/lib/cctv/state" \
  --tmpfs "/var/lib/cctv/buffer:size=${CCTV_BENCH_BUFFER_TMPFS:-4g},uid=10001,gid=10001,mode=0700" \
  --tmpfs /var/lib/cctv/spool:size=512m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /run/cctv:size=8m,uid=10001,gid=10001,mode=0700 --tmpfs /tmp:size=64m,mode=1777 \
  "$IMAGE" engine >/dev/null

id=$(docker inspect -f '{{.Id}}' "$PFX-engine")
cg=$(find /sys/fs/cgroup -maxdepth 4 -type d -name "*$id*" | head -1)
[ -n "$cg" ] || { echo "cgroup v2 of the engine container not found" >&2; exit 1; }
cpu_us() { awk '/^usage_usec/ {print $2}' "$cg/cpu.stat"; }
stat() { awk -v k="$1" '$1 == k {print $2}' "$cg/memory.stat"; }
buf_bytes() { docker exec "$PFX-engine" du -sb /var/lib/cctv/buffer 2>/dev/null | awk '{print $1}'; }

sleep "$WARM"
c0=$(cpu_us); b0=$(buf_bytes); t0=$(date +%s.%N)
sleep "$DUR"
c1=$(cpu_us); b1=$(buf_bytes); t1=$(date +%s.%N)
running=$(docker inspect -f '{{.State.Running}}' "$PFX-engine")
docker logs "$PFX-engine" > "$WORK/engine.log" 2>&1
[ -z "${CCTV_BENCH_LOG:-}" ] || cp "$WORK/engine.log" "$CCTV_BENCH_LOG"
oom=$(awk '$1 == "oom_kill" {print $2}' "$cg/memory.events" 2>/dev/null || echo 0)

python3 - <<PY | tee -a "${OUT:-/dev/null}"
import json, re
log = open("$WORK/engine.log", errors="replace").read()
grab = lambda key: sum(int(v) for v in re.findall(rf"^gate_stats .*\\b{key}=(\\d+)", log, re.M))
cores = ($c1 - $c0) / 1e6 / ($t1 - $t0)
mib = lambda v: round(int(v or 0) / 2**20, 1)
print(json.dumps({
  "arch": "$(uname -m)", "cpu": "$(awk -F: '/model name|^CPU part/ {gsub(/^ +/, "", $2); print $2; exit}' /proc/cpuinfo)",
  "image": "$IMAGE", "cameras": $N, "scene": "$SCENE", "cpus_limit": "$CPUS", "mem_limit": "$MEM",
  "cores": round(cores, 3), "cores_per_camera": round(cores / $N, 3),
  "anon_mib": mib("$(stat anon)"), "shmem_mib": mib("$(stat shmem)"),
  "current_mib": mib("$(cat "$cg/memory.current")"), "peak_mib": mib("$(cat "$cg/memory.peak" 2>/dev/null || echo 0)"),
  "buffer_mib_per_camera_minute": round((int("${b1:-0}") - int("${b0:-0}")) / 2**20 / $N / (($t1 - $t0) / 60), 1),
  "running": "$running" == "true", "oom_kill": int("${oom:-0}"),
  "yolo_frames": grab("scanned"), "gate_skipped_frames": grab("skipped") + grab("shadow_skipped"),
  "detector_skipped_segments": len(re.findall(r"^detector_skipped_segments ", log, re.M)),
  "recorder_failed": len(re.findall(r"^recorder_failed ", log, re.M)),
}))
PY
