#!/usr/bin/env bash
set -euo pipefail

SERVER_URL="${1:?Usage: bash client-bootstrap.sh http://YOUR_NAS_IP:3010}"
SERVER_URL="${SERVER_URL%/}"

if ! command -v multica >/dev/null 2>&1; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install the Multica CLI." >&2
    exit 1
  fi
  echo "Installing the Multica CLI with the official installer..."
  unset MULTICA_MODE
  curl -fsSL https://raw.githubusercontent.com/multica-ai/multica/main/scripts/install.sh | bash
fi

if ! command -v multica >/dev/null 2>&1; then
  echo "Multica CLI is not on PATH. Open a new shell and run this script again." >&2
  exit 1
fi

echo "Configuring self-hosted Multica at $SERVER_URL ..."
multica setup self-host --server-url "$SERVER_URL" --app-url "$SERVER_URL"
multica daemon status
