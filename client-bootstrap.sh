#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash client-bootstrap.sh --server-url http://YOUR_BROWSER_HOST:YOUR_APP_PORT [options]

Options:
  --app-url URL          Browser/app URL when it differs from the API URL
  --profile NAME         Isolated Multica profile for this fleet
  --workspace-id ID      Verify/switch the workspace used by this daemon
  --device-name NAME     Stable name shown for this fleet device
  --runtime-name NAME    Name shown for the runtime registered by this daemon
  --skip-install         Do not install the official Multica CLI
  --verify-only          Only verify auth and daemon state; do not open login
  --output-json          Emit machine-readable verification sections
  -h, --help             Show this help
EOF
}

SERVER_URL=""
APP_URL=""
PROFILE=""
WORKSPACE_ID=""
DEVICE_NAME=""
RUNTIME_NAME=""
SKIP_INSTALL=false
VERIFY_ONLY=false
OUTPUT_JSON=false

while (($#)); do
  case "$1" in
    --server-url) SERVER_URL="${2:?--server-url requires a URL}"; shift 2 ;;
    --app-url) APP_URL="${2:?--app-url requires a URL}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile requires a name}"; shift 2 ;;
    --workspace-id) WORKSPACE_ID="${2:?--workspace-id requires an id or slug}"; shift 2 ;;
    --device-name) DEVICE_NAME="${2:?--device-name requires a name}"; shift 2 ;;
    --runtime-name) RUNTIME_NAME="${2:?--runtime-name requires a name}"; shift 2 ;;
    --skip-install) SKIP_INSTALL=true; shift ;;
    --verify-only) VERIFY_ONLY=true; shift ;;
    --output-json) OUTPUT_JSON=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "$SERVER_URL" && "$1" != -* ]]; then
        SERVER_URL="$1"
        shift
      else
        echo "Unknown option: $1" >&2
        usage >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -z "$SERVER_URL" ]]; then
  usage >&2
  exit 2
fi
SERVER_URL="${SERVER_URL%/}"
APP_URL="${APP_URL:-$SERVER_URL}"

case "$SERVER_URL" in
  http://*|https://*) ;;
  *) echo "--server-url 必须是 http(s) URL。" >&2; exit 2 ;;
esac

if command -v curl >/dev/null 2>&1; then
  if $OUTPUT_JSON; then
    curl --fail --silent --show-error --max-time 10 "$SERVER_URL/health" >/dev/null
  else
    echo "Checking Multica server at $SERVER_URL ..."
    curl --fail --silent --show-error --max-time 10 "$SERVER_URL/health" >/dev/null
  fi
fi

find_multica() {
  if command -v multica >/dev/null 2>&1; then
    command -v multica
    return 0
  fi
  if [[ -x "$HOME/.multica/bin/multica" ]]; then
    printf '%s\n' "$HOME/.multica/bin/multica"
    return 0
  fi
  return 1
}

if ! CLI_PATH="$(find_multica)"; then
  if $SKIP_INSTALL; then
    echo "找不到 Multica CLI；去掉 --skip-install，让脚本调用官方安装脚本。" >&2
    exit 1
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install the Multica CLI." >&2
    exit 1
  fi
  echo "Installing the Multica CLI with the official installer..."
  unset MULTICA_MODE
  curl -fsSL https://raw.githubusercontent.com/multica-ai/multica/main/scripts/install.sh | bash
fi

if ! CLI_PATH="$(find_multica)"; then
  echo "安装脚本结束后仍找不到 Multica CLI。请重新打开 shell 后重试。" >&2
  exit 1
fi

PROFILE_ARGS=()
if [[ -n "$PROFILE" ]]; then
  PROFILE_ARGS=(--profile "$PROFILE")
fi

if $VERIFY_ONLY; then
  if ! $OUTPUT_JSON; then
    echo "Verifying existing Multica client ..."
  fi
else
  echo "Configuring self-hosted Multica at $SERVER_URL ..."
  [[ -n "$DEVICE_NAME" ]] && export MULTICA_DAEMON_DEVICE_NAME="$DEVICE_NAME"
  [[ -n "$RUNTIME_NAME" ]] && export MULTICA_AGENT_RUNTIME_NAME="$RUNTIME_NAME"
  "$CLI_PATH" setup self-host "${PROFILE_ARGS[@]}" --server-url "$SERVER_URL" --app-url "$APP_URL"
  if [[ -n "$WORKSPACE_ID" ]]; then
    "$CLI_PATH" workspace switch "$WORKSPACE_ID" "${PROFILE_ARGS[@]}"
  fi
fi

if $OUTPUT_JSON; then
  # Keep auth proof explicit even though the official command is
  # human/stderr-only. Do not scrape its output or expose token material; its
  # exit status is the machine-readable proof.
  AUTHENTICATED=false
  if "$CLI_PATH" auth status "${PROFILE_ARGS[@]}" >/dev/null 2>&1; then
    AUTHENTICATED=true
  fi
  printf '%s\n' 'MULTICA_VERIFY_AUTH_BEGIN'
  printf '{"authenticated":%s,"source":"auth-status"}\n' "$AUTHENTICATED"
  printf '%s\n' 'MULTICA_VERIFY_AUTH_END'
  if [[ "$AUTHENTICATED" != true ]]; then
    exit 1
  fi
  printf '%s\n' 'MULTICA_VERIFY_WORKSPACE_BEGIN'
  "$CLI_PATH" workspace get "$WORKSPACE_ID" "${PROFILE_ARGS[@]}" --output json
  printf '%s\n' 'MULTICA_VERIFY_WORKSPACE_END'
  printf '%s\n' 'MULTICA_VERIFY_RUNTIME_BEGIN'
  "$CLI_PATH" daemon status "${PROFILE_ARGS[@]}" --output json
  printf '%s\n' 'MULTICA_VERIFY_RUNTIME_END'
else
  echo "Authentication status:"
  "$CLI_PATH" auth status "${PROFILE_ARGS[@]}"
  if [[ -n "$WORKSPACE_ID" ]]; then
    echo "Workspace binding:"
    "$CLI_PATH" workspace get "$WORKSPACE_ID" "${PROFILE_ARGS[@]}" --output json
  fi
  echo "Daemon status (JSON):"
  "$CLI_PATH" daemon status "${PROFILE_ARGS[@]}" --output json
  echo "Client is connected. Use '$CLI_PATH daemon logs' when a task does not arrive."
fi
