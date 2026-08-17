#!/bin/sh

# NAS-local Multica recovery loop. This deliberately has no dependency on n8n,
# a database, or NetBird management APIs. It only starts the existing Compose
# project when the local HTTP readiness endpoint is unavailable.

set -u

CONFIG_FILE="${MULTICA_WATCHDOG_CONFIG:-/usr/local/etc/multica-watchdog.conf}"
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"

TARGET="${MULTICA_TARGET:-/opt/multica}"
NAS_IP="${MULTICA_NAS_IP:-}"
APP_PORT="${MULTICA_APP_PORT:-}"
INTERVAL="${MULTICA_WATCHDOG_INTERVAL:-60}"
COOLDOWN="${MULTICA_WATCHDOG_COOLDOWN:-180}"
DOCKER="${MULTICA_DOCKER_PATH:-docker}"
LOG_FILE="${MULTICA_WATCHDOG_LOG:-/var/log/multica-watchdog.log}"
LOCK_FILE="${MULTICA_WATCHDOG_LOCK:-/var/run/multica-watchdog.lock}"
LAST_RECOVERY_FILE="${MULTICA_WATCHDOG_LAST_RECOVERY:-/var/run/multica-watchdog.last-recovery}"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG_FILE"
}

ready() {
  [ -n "$NAS_IP" ] || return 1
  [ -n "$APP_PORT" ] || return 1
  curl -fsS --max-time 8 "http://${NAS_IP}:${APP_PORT}/readyz" >/dev/null 2>&1
}

recover() {
  now=$(date +%s)
  if [ -f "$LAST_RECOVERY_FILE" ]; then
    last=$(cat "$LAST_RECOVERY_FILE" 2>/dev/null || printf '0')
    case "$last" in
      ''|*[!0-9]*) last=0 ;;
    esac
    if [ $((now - last)) -lt "$COOLDOWN" ]; then
      log "health failed; recovery suppressed by cooldown"
      return 0
    fi
  fi

  printf '%s\n' "$now" > "$LAST_RECOVERY_FILE"
  log "health failed; starting existing Compose project"
  if cd "$TARGET" && "$DOCKER" compose --env-file .env \
      -f docker-compose.selfhost.yml -f docker-compose.nas.yml \
      up -d --remove-orphans >> "$LOG_FILE" 2>&1; then
    log "Compose recovery command completed"
  else
    log "Compose recovery command failed"
  fi
}

check_once() {
  if ready; then
    return 0
  fi
  recover
}

loop() {
  mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    exit 0
  fi
  log "watchdog started"
  while :; do
    check_once
    sleep "$INTERVAL"
  done
}

case "${1:-loop}" in
  check) check_once ;;
  loop) loop ;;
  *) printf 'usage: %s [check|loop]\n' "$0" >&2; exit 2 ;;
esac
