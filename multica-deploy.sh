#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "Python 3.9+ not found. Install Python and OpenSSH, then retry." >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/multica_deploy.py" "$@"
