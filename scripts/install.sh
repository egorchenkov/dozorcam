#!/bin/sh
# Dozorcam: установка одной командой (Linux amd64/arm64, POSIX sh):
#
#   curl -fsSL https://egorchenkov.github.io/dozorcam/install.sh | sh
#
# Что делает: проверяет Docker и compose (нет — предлагает get.docker.com, y/N);
# скачивает из релиза по тегу compose.yml, .env.example и обёртку dozorcam в
# ~/dozorcam (git не нужен), сверяет их с SHA256SUMS релиза; спрашивает токен бота
# и проверяет его в Telegram (getMe); язык — из $LANG, часовой пояс — с хоста
# (CCTV_TZ), лимиты буфера в RAM — по памяти машины; закрепляет образ по digest,
# запускает, ждёт healthcheck и печатает ссылку владельца t.me/<бот>?start=<код> с QR.
#
# Без вопросов (CI, автоматизация) — всё переменными:
#   DOZORCAM_TOKEN=<токен>  DOZORCAM_LANG=en|ru  DOZORCAM_TZ=Europe/Berlin
#   DOZORCAM_DIR=~/dozorcam  DOZORCAM_VERSION=0.3.0 (иначе последний релиз)
#   DOZORCAM_INSTALL_DOCKER=1 (поставить Docker без вопроса)
# Для стенда и CI: DOZORCAM_DOWNLOAD_URL (каталог с файлами релиза, file:// тоже),
#   DOZORCAM_IMAGE (свой реестр), DOZORCAM_TELEGRAM_API (свой Bot API или мок),
#   DOZORCAM_REPO, DOZORCAM_GITHUB_API.
# Повторный запуск в том же каталоге оставляет .env (токен, настройки) и обновляет
# только то, что спрашивает; переход на новую версию — `~/dozorcam/dozorcam update`.
set -eu

REPO=${DOZORCAM_REPO:-egorchenkov/dozorcam}
GITHUB_API=${DOZORCAM_GITHUB_API:-https://api.github.com}
TELEGRAM_API=${DOZORCAM_TELEGRAM_API:-https://api.telegram.org}
DIR=${DOZORCAM_DIR:-$HOME/dozorcam}
FILES="compose.yml env.example dozorcam"  # .env.example в релизе — env.example

case "${DOZORCAM_LANG:-${LC_ALL:-${LANG:-}}}" in ru*) UI=ru ;; *) UI=en ;; esac

say() { if [ "$UI" = ru ]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi; }
warn() { say "$1" "$2" >&2; }
die() { warn "Error: $1" "Ошибка: $2"; exit 1; }
step() { printf '\n==> '; say "$1" "$2"; }

has_tty() { (: </dev/tty) 2>/dev/null; }

ask() {  # $1 en, $2 ru — да/нет, по умолчанию «нет»; без терминала — «нет»
  has_tty || return 1
  if [ "$UI" = ru ]; then printf '%s [y/N] ' "$2" >/dev/tty; else printf '%s [y/N] ' "$1" >/dev/tty; fi
  read -r answer </dev/tty || return 1
  case $answer in y*|Y*|д*|Д*) return 0 ;; esac
  return 1
}

prompt() {  # $1 en, $2 ru, $3 умолчание — строка с терминала; без терминала — умолчание
  if ! has_tty; then printf '%s\n' "$3"; return; fi
  if [ "$UI" = ru ]; then printf '%s' "$2" >/dev/tty; else printf '%s' "$1" >/dev/tty; fi
  [ -n "$3" ] && printf ' [%s]' "$3" >/dev/tty
  printf ': ' >/dev/tty
  read -r reply </dev/tty || reply=
  printf '%s\n' "${reply:-$3}"
}

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"
  elif command -v sudo >/dev/null 2>&1; then sudo "$@"
  else die "need root for: $*" "нужны права root для: $*"; fi
}

env_get() {
  [ -f "$DIR/.env" ] || return 0
  KEY=$1 awk 'index($0, ENVIRON["KEY"] "=") == 1 { value = substr($0, length(ENVIRON["KEY"]) + 2)
                                                  sub(/[ \t]+#.*$/, "", value) }
              END { if (value != "") print value }' "$DIR/.env"
}

env_set() {  # $1 ключ, $2 значение: заменить строку (или её закомментированный образец), иначе дописать
  KEY=$1 VALUE=$2 awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"] }
    { lines[NR] = $0 }
    index($0, k "=") == 1 && !active { active = NR }
    (index($0, "# " k "=") == 1 || index($0, "#" k "=") == 1) && !sample { sample = NR }
    END {
      at = active ? active : sample
      for (i = 1; i <= NR; i++) {
        if (i == at) print k "=" v
        else if (!(index(lines[i], k "=") == 1)) print lines[i]
      }
      if (!at) print k "=" v
    }' "$DIR/.env" >"$DIR/.env.tmp"
  cat "$DIR/.env.tmp" >"$DIR/.env"
  rm -f "$DIR/.env.tmp"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# --- 1. машина и Docker -------------------------------------------------------------
step "Checking the machine" "Проверяю машину"
[ "$(uname -s)" = Linux ] || die "Dozorcam runs on Linux only" "Dozorcam работает только на Linux"
case $(uname -m) in
  x86_64|amd64|aarch64|arm64) ;;
  *) die "$(uname -m) is not supported: images exist for amd64 and arm64 (64-bit) only" \
         "$(uname -m) не поддерживается: образы есть только для amd64 и arm64 (64 бит)" ;;
esac
command -v curl >/dev/null 2>&1 || die "curl is required" "нужен curl"

if ! command -v docker >/dev/null 2>&1; then
  say "Docker is not installed. It can be installed with the official script:" \
      "Docker не установлен. Его можно поставить официальным скриптом:"
  echo "    curl -fsSL https://get.docker.com | sudo sh"
  if [ "${DOZORCAM_INSTALL_DOCKER:-}" = 1 ] || ask "Install Docker now?" "Поставить Docker сейчас?"; then
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    as_root sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    if [ "$(id -u)" != 0 ]; then
      as_root usermod -aG docker "$(id -un)"
      say "You were added to the docker group; until you log in again, docker runs via sudo." \
          "Вы добавлены в группу docker; до нового входа в систему docker пойдёт через sudo."
    fi
  else
    die "install Docker and run this script again" "поставьте Docker и запустите скрипт снова"
  fi
fi
if docker info >/dev/null 2>&1; then
  DOCKER=docker
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"
else
  die "no access to docker (is the daemon running?)" "нет доступа к docker (запущен ли демон?)"
fi
# shellcheck disable=SC2086  # DOCKER — «docker» или «sudo docker»
$DOCKER compose version >/dev/null 2>&1 ||
  die "the docker compose plugin is missing (package docker-compose-plugin)" \
      "нет плагина docker compose (пакет docker-compose-plugin)"

# --- 2. файлы релиза ----------------------------------------------------------------
VERSION=${DOZORCAM_VERSION:-}
if [ -z "$VERSION" ]; then
  VERSION=$(curl -fsSL --retry 3 -H 'Accept: application/vnd.github+json' \
              "$GITHUB_API/repos/$REPO/releases/latest" |
            sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1) || true
  [ -n "$VERSION" ] || die "cannot read the latest release of $REPO" "не удалось узнать последний релиз $REPO"
fi
VERSION=${VERSION#v}
BASE=${DOZORCAM_DOWNLOAD_URL:-https://github.com/$REPO/releases/download/v$VERSION}
step "Downloading Dozorcam $VERSION into $DIR" "Скачиваю Dozorcam $VERSION в $DIR"
mkdir -p "$DIR"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL --retry 3 -o "$TMP/SHA256SUMS" "$BASE/SHA256SUMS" ||
  die "cannot download $BASE/SHA256SUMS" "не скачать $BASE/SHA256SUMS"
for name in $FILES; do
  curl -fsSL --retry 3 -o "$TMP/$name" "$BASE/$name" || die "cannot download $BASE/$name" "не скачать $BASE/$name"
  want=$(NAME=$name awk '$2 == ENVIRON["NAME"] || $2 == "*" ENVIRON["NAME"] { print $1 }' "$TMP/SHA256SUMS")
  { [ -n "$want" ] && [ "$want" = "$(sha256_of "$TMP/$name")" ]; } ||
    die "$name: sha256 does not match SHA256SUMS of the release" "$name: sha256 не совпадает с SHA256SUMS релиза"
done
cp "$TMP/compose.yml" "$DIR/compose.yml"
cp "$TMP/env.example" "$DIR/.env.example"
cp "$TMP/dozorcam" "$DIR/dozorcam"
chmod 0755 "$DIR/dozorcam"
# Каталог конфига монтируется в контейнер только на чтение и читается uid 10001.
mkdir -p "$DIR/config"
chmod 0755 "$DIR/config"
if [ ! -f "$DIR/.env" ]; then
  (umask 077; cp "$DIR/.env.example" "$DIR/.env")
fi
chmod 0600 "$DIR/.env"

# --- 3. токен бота ------------------------------------------------------------------
step "Telegram bot" "Бот Telegram"
token=${DOZORCAM_TOKEN:-$(env_get CCTV_BOT_TOKEN)}
if [ -z "$token" ]; then
  say "Create a bot with @BotFather in Telegram (/newbot) and paste its token." \
      "Создайте бота у @BotFather в Telegram (/newbot) и вставьте его токен."
fi
bot=
tries=3
while :; do
  [ -n "$token" ] || token=$(prompt "Bot token" "Токен бота" "")
  if printf '%s' "$token" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'; then
    reply=$(curl -fsS --max-time 15 "$TELEGRAM_API/bot$token/getMe" 2>/dev/null) || reply=
    bot=$(printf '%s' "$reply" | sed -n 's/.*"username": *"\([^"]*\)".*/\1/p')
    [ -n "$bot" ] && break
    warn "Telegram rejected this token (getMe failed)." "Telegram не принял токен (getMe не прошёл)."
  else
    warn "This does not look like a bot token (123456789:AA...)." "Это не похоже на токен бота (123456789:AA...)."
  fi
  tries=$((tries - 1))
  if [ "$tries" -le 0 ] || ! has_tty; then die "no valid bot token" "нет рабочего токена бота"; fi
  token=
done
say "Bot: @$bot" "Бот: @$bot"
env_set CCTV_BOT_TOKEN "$token"

# --- 4. язык и часовой пояс ---------------------------------------------------------
lang=${DOZORCAM_LANG:-$(env_get CCTV_LANG)}
lang=$(prompt "Language (en/ru)" "Язык (en/ru)" "${lang:-$UI}")
case $lang in ru*) lang=ru ;; *) lang=en ;; esac
UI=$lang
env_set CCTV_LANG "$lang"

host_tz() {
  zone=$(timedatectl show -p Timezone --value 2>/dev/null) || zone=
  [ -n "$zone" ] || zone=$(cat /etc/timezone 2>/dev/null) || zone=
  [ -n "$zone" ] || zone=$(readlink /etc/localtime 2>/dev/null | sed -n 's|.*/zoneinfo/||p') || zone=
  printf '%s\n' "${zone:-UTC}"
}
tz=${DOZORCAM_TZ:-$(env_get CCTV_TZ)}
tz=$(prompt "Time zone (IANA, e.g. Europe/Berlin)" "Часовой пояс (IANA, например Europe/Moscow)" "${tz:-$(host_tz)}")
if [ -d /usr/share/zoneinfo ] && [ ! -f "/usr/share/zoneinfo/$tz" ]; then
  warn "Unknown time zone $tz — using UTC; change CCTV_TZ in $DIR/.env later." \
       "Неизвестный пояс $tz — ставлю UTC; поменять можно в CCTV_TZ в $DIR/.env."
  tz=UTC
fi
env_set CCTV_TZ "$tz"

# --- 5. память под буфер видео ------------------------------------------------------
ram_mb=$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)
buffer_mb=$((ram_mb / 4))
[ "$buffer_mb" -lt 512 ] && buffer_mb=512
[ "$buffer_mb" -gt 4096 ] && buffer_mb=4096
spool_mb=512
[ "$buffer_mb" -lt 1024 ] && spool_mb=256
# Лимит буфера — на поток, общего лимита у движка нет: камеры обязаны поместиться в tmpfs
# сами. 200 МБ — это 7 минут основного потока 4 Мбит/с; с детекторным (~36 МБ за 10 минут)
# камера занимает не больше ~240 МБ, и буфер вмещает buffer_mb/240 камер. Прежние
# «половина буфера на поток» переполняли tmpfs уже двумя камерами 4 Мбит/с на машине с 2 ГБ
# (2 × (256 + 36) > 512 МБ) — запись вставала (замер калькулятора железа, 09.10.2026).
camera_mb=200
fits=$((buffer_mb / 240))
say "RAM ${ram_mb} MB: video buffer ${buffer_mb} MB (in RAM, not on disk), enough for about ${fits} cameras of 2 MP." \
    "RAM ${ram_mb} МБ: буфер видео ${buffer_mb} МБ (в памяти, не на диске) — примерно на ${fits} камер 2 Мп."
if [ "$ram_mb" -lt 1700 ]; then
  warn "Less than 2 GB RAM: Dozorcam needs at least 2 GB." "Меньше 2 ГБ RAM: Dozorcam нужно не меньше 2 ГБ."
fi
env_set CCTV_BUFFER_TMPFS "${buffer_mb}m"
env_set CCTV_SPOOL_TMPFS "${spool_mb}m"
env_set CCTV_BUFFER_MAX_BYTES $((camera_mb * 1048576))
env_set CCTV_STORAGE_BUDGET_BYTES $(((buffer_mb + spool_mb) * 1048576))

# --- 6. образ и запуск --------------------------------------------------------------
[ -n "${DOZORCAM_IMAGE:-}" ] && env_set CCTV_IMAGE "$DOZORCAM_IMAGE"
[ -n "${DOZORCAM_TELEGRAM_API:-}" ] && env_set CCTV_TELEGRAM_API "$DOZORCAM_TELEGRAM_API"
env_set CCTV_IMAGE_TAG "$VERSION"  # тег@digest допишет обёртка при pull

step "Starting Dozorcam $VERSION" "Запускаю Dozorcam $VERSION"
DOZORCAM_LANG=$lang sh "$DIR/dozorcam" start

echo
say "Installed in $DIR. Manage it with:" "Установлено в $DIR. Управление:"
echo "    $DIR/dozorcam status | logs | code | update | backup | restore | restart | uninstall"
