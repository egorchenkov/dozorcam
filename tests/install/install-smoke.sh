#!/bin/sh
# install-smoke: установщик и обёртка dozorcam на этом коммите, без сети и без бота.
#
# Образ коммита — в локальный реестр (pull по digest как у человека), файлы релиза —
# scripts/release-assets.sh, Telegram и GitHub Releases — мок tests/install/mock_api.py.
# Цикл: install.sh → файлы и .env → compose config → оба healthy → SETUP в журнале и
# ссылка `dozorcam code` → повторная установка с буфером на диске → backup → uninstall
# --volumes → restore (тот же код владельца) →
# update на «последний релиз» → update на образ с ломаным healthcheck и автооткат →
# uninstall. Запуск: в CI (джоб install-smoke) и руками рядом с другой установкой:
#
#   SMOKE_PORT_BASE=18780 tests/install/install-smoke.sh   # свои порты и имя проекта
#   SMOKE_SOURCE_IMAGE=<образ> ...                        # не собирать, взять готовый
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
version=$(python3 "$root/scripts/site-sync.py" --print-version)
work=${SMOKE_WORK:-$(mktemp -d)}
reg_port=${SMOKE_REGISTRY_PORT:-5000}
api_port=${SMOKE_API_PORT:-8099}
image=localhost:$reg_port/dozorcam
token=12345:smoke_test_token_not_a_real_one_000
next=98.0.0     # «последний релиз»: тот же образ, должен обновиться
broken=99.0.0   # healthcheck всегда падает: должен откатиться на $next
dir=$work/dozorcam
registry=dozorcam-smoke-registry

dcs() { docker compose --project-directory "$dir" -f "$dir/compose.yml" "$@"; }
log() { printf '\n### %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
  status=$?
  if [ "$status" != 0 ] && [ -f "$dir/compose.yml" ]; then
    dcs logs --tail 80 >&2 || true
    sed -n '1,200p' "$work/api.log" >&2 2>/dev/null || true
  fi
  [ -f "$dir/compose.yml" ] && dcs down -v >/dev/null 2>&1
  [ -n "${mock_pid:-}" ] && kill "$mock_pid" 2>/dev/null
  docker rm -fv "$registry" >/dev/null 2>&1  # -v: анонимный том registry с образами, иначе он остаётся на диске
  exit "$status"
}
trap cleanup EXIT

log "registry and image $image:$version"
docker rm -fv "$registry" >/dev/null 2>&1 || true
docker run -d --name "$registry" -p "127.0.0.1:$reg_port:5000" registry:2 >/dev/null
if [ -n "${SMOKE_SOURCE_IMAGE:-}" ]; then
  docker tag "$SMOKE_SOURCE_IMAGE" "$image:$version"
else
  docker build -t "$image:$version" "$root"
fi
docker tag "$image:$version" "$image:$next"
printf 'FROM %s\nHEALTHCHECK --interval=3s --timeout=2s --start-period=0s --retries=1 CMD ["false"]\n' \
  "$image:$version" | docker build -q -t "$image:$broken" - >/dev/null
for tag in "$version" "$next" "$broken"; do
  docker push -q "$image:$tag" >/dev/null
done

log "release files"
for tag in "$version" "$next" "$broken"; do
  "$root/scripts/release-assets.sh" "$work/dist/$tag"
  if [ "$tag" != "$version" ]; then
    { printf '# Changelog\n\n## [%s] — smoke\n\n- smoke release %s\n\n' "$tag" "$tag"
      sed '1d' "$root/CHANGELOG.md"; } >"$work/dist/$tag/CHANGELOG.md"
    (cd "$work/dist/$tag" && sha256sum compose.yml env.example CHANGELOG.md install.sh dozorcam >SHA256SUMS)
  fi
done

log "mock Telegram/GitHub on 127.0.0.1:$api_port"
python3 "$root/tests/install/mock_api.py" --port "$api_port" --token "$token" --latest "$next" \
  --log "$work/api.log" &
mock_pid=$!
sleep 1

if [ -n "${SMOKE_PORT_BASE:-}" ]; then
  # Рядом с другой установкой (network_mode: host): свои порты и имя проекта.
  mkdir -p "$dir"
  (umask 077; cat >"$dir/.env" <<EOF
COMPOSE_PROJECT_NAME=dozorcam-smoke
CCTV_BRIDGE_PORT=$SMOKE_PORT_BASE
CCTV_EVENTS_PORT=$((SMOKE_PORT_BASE + 1))
CCTV_RTSP_PROXY_PORT=$((SMOKE_PORT_BASE + 10))
EOF
  )
fi

export DOZORCAM_GITHUB_API="http://127.0.0.1:$api_port/github"
export DOZORCAM_HEALTH_TIMEOUT=240
log "install.sh (non-interactive)"
DOZORCAM_DIR=$dir DOZORCAM_VERSION=$version DOZORCAM_DOWNLOAD_URL="file://$work/dist/$version" \
  DOZORCAM_IMAGE=$image DOZORCAM_TELEGRAM_API="http://127.0.0.1:$api_port" DOZORCAM_TOKEN=$token \
  DOZORCAM_LANG=en DOZORCAM_TZ=Asia/Kathmandu \
  setsid sh "$root/scripts/install.sh" </dev/null | tee "$work/install.out"

log "files and .env"
for name in compose.yml .env .env.example dozorcam; do
  [ -f "$dir/$name" ] || fail "no $name"
done
[ -x "$dir/dozorcam" ] || fail "dozorcam is not executable"
[ -d "$dir/config" ] || fail "no config/"
[ "$(stat -c %a "$dir/.env")" = 600 ] || fail ".env is not 0600"
want() { grep -qx "$1" "$dir/.env" || fail ".env has no line $1"; }
want "CCTV_BOT_TOKEN=$token"
want "CCTV_LANG=en"
want "CCTV_TZ=Asia/Kathmandu"
want "CCTV_IMAGE=$image"
grep -Eq "^CCTV_IMAGE_TAG=$version@sha256:[0-9a-f]{64}$" "$dir/.env" || fail "image is not pinned by digest"
grep -Eq '^CCTV_BUFFER_TMPFS=[0-9]+m$' "$dir/.env" || fail "no buffer tmpfs limit"
# Лимит на поток, а не «половина буфера»: иначе две камеры 4 Мбит/с переполняют tmpfs 512m.
want "CCTV_BUFFER_MAX_BYTES=209715200"
dcs config -q || fail "docker compose config is invalid"

dz() { setsid sh "$dir/dozorcam" "$@" </dev/null; }
healthy() {
  for service in engine bot; do
    cid=$(dcs ps -q "$service")
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$cid")" = healthy ] || fail "$service is not healthy"
  done
}

log "containers, SETUP, owner link"
healthy
dcs logs bot | grep -q 'SETUP: owner code' || fail "no SETUP line in bot logs"
for service in engine bot; do
  dcs exec -T "$service" printenv CCTV_TZ | grep -qx Asia/Kathmandu ||
    fail "$service: CCTV_TZ is not passed"
done
grep -q "/bot<token>/getUpdates" "$work/api.log" || fail "bot does not poll the mock (CCTV_TELEGRAM_API ignored)"
grep -q 'https://t.me/dozorcam_smoke_bot?start=' "$work/install.out" || fail "install.sh printed no owner link"
code1=$(dz code | sed -n 's|.*https://t.me/dozorcam_smoke_bot?start=\([A-Z0-9]*\).*|\1|p' | head -n 1)
[ -n "$code1" ] || fail "dozorcam code printed no link"
dz status

log "restart applies .env"
printf 'CCTV_UPDATE_CHECK=0\n' >>"$dir/.env"
dz restart
healthy
dcs exec -T bot printenv CCTV_UPDATE_CHECK | grep -qx 0 || fail "dozorcam restart did not apply .env"
code0=$(dz code | sed -n 's|.*https://t.me/dozorcam_smoke_bot?start=\([A-Z0-9]*\).*|\1|p' | head -n 1)
[ "$code0" = "$code1" ] || fail "restart lost the bot state (code $code1 → $code0)"

log "buffer on disk: install.sh again with DOZORCAM_BUFFER=disk"
# Повторная установка в том же каталоге: .env и состояние остаются, буфер переезжает в том
# engine-buffer (RAM — только процессы); писать в него должен uid движка.
DOZORCAM_DIR=$dir DOZORCAM_VERSION=$version DOZORCAM_DOWNLOAD_URL="file://$work/dist/$version" \
  DOZORCAM_IMAGE=$image DOZORCAM_TELEGRAM_API="http://127.0.0.1:$api_port" DOZORCAM_TOKEN=$token \
  DOZORCAM_LANG=en DOZORCAM_TZ=Asia/Kathmandu DOZORCAM_BUFFER=disk \
  setsid sh "$root/scripts/install.sh" </dev/null | tee "$work/install-disk.out"
want "CCTV_BUFFER_DIR=/var/lib/cctv/buffer-disk"
want "CCTV_BUFFER_TMPFS=64m"
grep -q "SSD only" "$work/install-disk.out" || fail "install.sh did not warn about disk writes"
healthy
dcs exec -T engine printenv CCTV_BUFFER_DIR | grep -qx /var/lib/cctv/buffer-disk || fail "engine: CCTV_BUFFER_DIR is not passed"
dcs exec -T engine sh -c 'touch /var/lib/cctv/buffer-disk/.probe && rm /var/lib/cctv/buffer-disk/.probe' ||
  fail "engine cannot write to the disk buffer"
code3=$(dz code | sed -n 's|.*https://t.me/dozorcam_smoke_bot?start=\([A-Z0-9]*\).*|\1|p' | head -n 1)
[ "$code3" = "$code1" ] || fail "reinstall lost the bot state (code $code1 → $code3)"

log "backup → uninstall --volumes → restore"
dz backup "$work/backup.tar.gz"
tar -tzf "$work/backup.tar.gz" >"$work/backup.list"
for member in ./.env ./compose.yml ./dozorcam-backup.txt ./volumes/engine-state.tar ./volumes/bot-state.tar; do
  grep -qx "$member" "$work/backup.list" || fail "backup has no $member"
done
grep -q engine-buffer "$work/backup.list" && fail "backup took the video buffer volume"
[ "$(stat -c %a "$work/backup.tar.gz")" = 600 ] || fail "backup is not 0600"
healthy
project=$(dcs config | sed -n 's/^name: *//p' | head -n 1)
dz uninstall -y --volumes
[ -z "$(dcs ps -aq)" ] || fail "containers left after uninstall"
docker volume inspect "${project}_bot-state" >/dev/null 2>&1 && fail "bot-state volume left after uninstall --volumes"
docker volume inspect "${project}_engine-buffer" >/dev/null 2>&1 && fail "engine-buffer volume left after uninstall"
[ -f "$dir/.env" ] || fail "uninstall removed .env"
dz restore -y "$work/backup.tar.gz"
healthy
code2=$(dz code | sed -n 's|.*https://t.me/dozorcam_smoke_bot?start=\([A-Z0-9]*\).*|\1|p' | head -n 1)
[ "$code1" = "$code2" ] || fail "restore lost the bot state (code $code1 → $code2)"

log "update to the latest release ($next)"
DOZORCAM_DOWNLOAD_URL="file://$work/dist/$next" dz update -y | tee "$work/update.out"
grep -q "smoke release $next" "$work/update.out" || fail "update did not show the changelog"
grep -Eq "^CCTV_IMAGE_TAG=$next@sha256:" "$dir/.env" || fail "update did not pin $next"
healthy

log "update to a broken image ($broken) rolls back"
if DOZORCAM_DOWNLOAD_URL="file://$work/dist/$broken" dz update -y "$broken"; then
  fail "update to a broken image reported success"
fi
grep -Eq "^CCTV_IMAGE_TAG=$next@sha256:" "$dir/.env" || fail "rollback did not restore $next in .env"
healthy

log "uninstall"
dz uninstall -y --volumes
[ -z "$(dcs ps -aq)" ] || fail "containers left after uninstall"
log "install-smoke: OK"
