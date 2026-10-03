#!/usr/bin/env bash
# Контроль локального временного хранилища CCTV. Не содержит и не читает секреты.
set -euo pipefail

storage_root="${CCTV_STORAGE_ROOT:-/var/lib/cctv}"
max_total_bytes="${CCTV_MAX_TOTAL_BYTES:-8589934592}"       # 8 GiB
min_free_bytes="${CCTV_MIN_FREE_BYTES:-4294967296}"         # 4 GiB
clip_ttl_minutes="${CCTV_CLIP_TTL_MINUTES:-10080}"          # 7 суток

usage() {
  echo "usage: $0 check|prune" >&2
  exit 64
}

[[ $# -eq 1 ]] || usage
[[ "$storage_root" = /* && "$storage_root" != / ]] || {
  echo "storage root must be an absolute non-root path" >&2
  exit 64
}
[[ "$max_total_bytes" =~ ^[0-9]+$ && "$min_free_bytes" =~ ^[0-9]+$ && "$clip_ttl_minutes" =~ ^[1-9][0-9]*$ ]] || {
  echo "retention limits must be positive integer byte/minute values" >&2
  exit 64
}

delivered_dir="$storage_root/events/delivered"
pending_dir="$storage_root/events/pending"
buffer_dir="${CCTV_BUFFER_DIR:-$storage_root/buffer}"
install -d -m 0700 "$delivered_dir" "$pending_dir" "$buffer_dir"

bytes_used() {
  # Буфер может жить отдельно (tmpfs): считаем его вместе с транзитом.
  du -scxB1 "$storage_root" "$buffer_dir" | awk 'END {print $1}'
}

bytes_free() {
  df -PB1 "$storage_root" | awk 'NR == 2 {print $4}'
}

check() {
  local used free
  used="$(bytes_used)"
  free="$(bytes_free)"
  if (( used > max_total_bytes )); then
    echo "storage_capacity: used=${used} exceeds budget=${max_total_bytes}" >&2
    return 1
  fi
  if (( free < min_free_bytes )); then
    echo "storage_capacity: free=${free} below reserve=${min_free_bytes}" >&2
    return 1
  fi
  echo "storage_ok: used=${used} free=${free}"
}

prune() {
  # Pending files are intentionally excluded: delivery confirmation owns them.
  find "$delivered_dir" -xdev -type f -mmin +"$clip_ttl_minutes" -print -delete
  find "$delivered_dir" -xdev -depth -type d -empty -delete
  check
}

case "$1" in
  check) check ;;
  prune) prune ;;
  *) usage ;;
esac
