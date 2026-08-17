#!/bin/sh

WATCHDOG=/usr/local/bin/multica-watchdog.sh
PID_FILE=/var/run/multica-watchdog.pid

is_running() {
  [ -s "$PID_FILE" ] || return 1
  pid=$(cat "$PID_FILE" 2>/dev/null || true)
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

case "${1:-status}" in
  start)
    if is_running; then exit 0; fi
    nohup "$WATCHDOG" loop >/dev/null 2>&1 &
    echo $! > "$PID_FILE"
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if is_running; then
      echo "multica-watchdog is running ($(cat "$PID_FILE"))"
    else
      echo "multica-watchdog is stopped"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status}" >&2
    exit 2
    ;;
esac
