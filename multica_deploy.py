#!/usr/bin/env python3
"""Zero-dependency Multica NAS deployment helper.

The script intentionally delegates container work to the NAS Docker Compose
installation. It only needs Python 3.9+, OpenSSH (ssh/scp), and an SSH account
that can run Docker. Synology uses ``sudo -n`` by default; generic Linux can
use ``--no-sudo`` when the SSH user already owns the target directory and can
run Docker directly.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
DOCKER_PATH = "docker"
DEFAULTS = {
    "nas_host": "",
    "nas_ip": "",
    "nas_target": "/opt/multica",
    "source_dir": "",
    "image_tag": "v0.4.26",
    "backend_image": "",
    "web_image": "",
    "app_port": 3010,
    "backend_port": 3011,
    "frontend_port": 3012,
    "network_subnet": "10.253.0.0/24",
    "owner": "multica",
    "group": "multica",
    "platform": "auto",
}
REQUIRED_FILES = (
    "docker-compose.selfhost.yml",
    "docker-compose.nas.yml",
    ".env.template",
)
CONFIG_KEYS = (
    "nas_host",
    "ssh_port",
    "nas_ip",
    "nas_target",
    "source_dir",
    "docker_path",
    "no_sudo",
    "owner",
    "group",
    "backend_image",
    "web_image",
)
CONFIG_OPTIONS = {
    "nas_host": ("--nas-host",),
    "ssh_port": ("--ssh-port",),
    "nas_ip": ("--nas-ip",),
    "nas_target": ("--nas-target",),
    "source_dir": ("--source-dir",),
    "docker_path": ("--docker-path",),
    "no_sudo": ("--no-sudo", "--sudo"),
    "owner": ("--owner",),
    "group": ("--group",),
    "backend_image": ("--backend-image",),
    "web_image": ("--web-image",),
}


class ConfigError(ValueError):
    """An operator-supplied setting is invalid."""


def default_config_path() -> Path:
    """Return a per-user path; deployment settings never live in the source tree."""

    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Multica" / "deploy.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "multica" / "deploy.json"
    return Path.home() / ".config" / "multica" / "deploy.json"


def load_config(path: Path) -> dict[str, object]:
    """Load non-secret deployment settings from the operator's config file."""

    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"读取部署配置失败: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"部署配置必须是 JSON 对象: {path}")
    result = {key: payload[key] for key in CONFIG_KEYS if key in payload}
    string_keys = {
        "nas_host",
        "nas_ip",
        "nas_target",
        "source_dir",
        "docker_path",
        "owner",
        "group",
        "backend_image",
        "web_image",
    }
    for key in string_keys:
        if key in result and not isinstance(result[key], str):
            raise ConfigError(f"部署配置字段 {key} 必须是字符串: {path}")
    if "ssh_port" in result and (
        isinstance(result["ssh_port"], bool) or not isinstance(result["ssh_port"], int)
    ):
        raise ConfigError(f"部署配置字段 ssh_port 必须是整数: {path}")
    if "no_sudo" in result and not isinstance(result["no_sudo"], bool):
        raise ConfigError(f"部署配置字段 no_sudo 必须是布尔值: {path}")
    return result


def save_config(args: argparse.Namespace) -> Path:
    """Persist only connection settings; secrets are deliberately excluded."""

    path = Path(args.config_file).expanduser()
    payload = {key: getattr(args, key) for key in CONFIG_KEYS if hasattr(args, key)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise ConfigError(f"保存部署配置失败: {path}") from exc
    return path


def option_supplied(raw_args: list[str], option: str) -> bool:
    return any(token == option or token.startswith(option + "=") for token in raw_args)


def apply_saved_config(args: argparse.Namespace) -> Path:
    """Merge saved values only when the operator did not pass that option now."""

    path = Path(args.config_file).expanduser()
    saved = load_config(path)
    explicit = getattr(args, "_explicit_options", set())
    for key, options in CONFIG_OPTIONS.items():
        if key in saved and not any(option in explicit for option in options):
            setattr(args, key, saved[key])
    return path


def q(value: str) -> str:
    """Quote a value for the POSIX shell used by the remote SSH command."""

    return shlex.quote(value)


def check_binary(name: str) -> None:
    if shutil.which(name) is None:
        package_hint = "Windows OpenSSH Client" if os.name == "nt" else "openssh-client"
        raise ConfigError(
            f"找不到 {name}。请安装 {package_hint}，并确认它在 PATH 中。"
        )


def validate_config(args: argparse.Namespace, *, require_ip: bool = True) -> None:
    if not args.nas_host:
        raise ConfigError("必须提供 --nas-host（SSH 主机、IP 或 SSH config 别名）。")
    if require_ip and not args.nas_ip:
        raise ConfigError("必须提供 --nas-ip（部署服务的内网 IPv4 地址）。")
    if args.nas_ip:
        try:
            address = ipaddress.ip_address(args.nas_ip)
        except ValueError as exc:
            raise ConfigError(f"--nas-ip 不是有效 IP: {args.nas_ip}") from exc
        if address.version != 4:
            raise ConfigError("目前 Caddy 模板只支持 IPv4 --nas-ip。")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", args.nas_target):
        raise ConfigError("--nas-target 必须是简单的绝对 NAS 路径。")
    docker_value = getattr(args, "docker_path", DOCKER_PATH)
    if not re.fullmatch(r"[A-Za-z0-9._/+:-]+", docker_value):
        raise ConfigError("--docker-path 只能是 Docker 可执行文件名或绝对路径。")
    image_tag = getattr(args, "image_tag", DEFAULTS["image_tag"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", image_tag):
        raise ConfigError("--image-tag 只能包含字母、数字、点、下划线或短横线。")
    platform = getattr(args, "platform", DEFAULTS["platform"])
    if platform not in {"auto", "linux/amd64", "linux/arm64", "linux/arm/v7"}:
        raise ConfigError("--platform 只能是 auto、linux/amd64、linux/arm64 或 linux/arm/v7。")
    for name in ("backend_image", "web_image"):
        image = getattr(args, name, DEFAULTS[name])
        if image and not re.fullmatch(r"[A-Za-z0-9._/@:-]+", image):
            raise ConfigError(f"--{name.replace('_', '-')} 不是有效的 Docker 镜像引用。")
    for name in ("owner", "group"):
        value = getattr(args, name, DEFAULTS[name])
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ConfigError(f"--{name} 含有不安全字符。")
    network_subnet = getattr(args, "network_subnet", DEFAULTS["network_subnet"])
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}", network_subnet):
        raise ConfigError("--network-subnet 必须类似 10.253.0.0/24。")
    if args.ssh_port < 0 or args.ssh_port > 65535:
        raise ConfigError("--ssh-port 必须在 0 到 65535 之间，0 表示使用 SSH 配置。")
    for name in ("app_port", "backend_port", "frontend_port"):
        port = getattr(args, name, DEFAULTS[name])
        if port < 1 or port > 65535:
            raise ConfigError(f"--{name.replace('_', '-')} 必须在 1 到 65535 之间。")


def ssh_base(args: argparse.Namespace) -> list[str]:
    command = ["ssh"]
    if args.ssh_port:
        command += ["-p", str(args.ssh_port)]
    return command + [args.nas_host]


def docker_path(args: argparse.Namespace) -> str:
    return getattr(args, "docker_path", DOCKER_PATH)


def auto_detect_remote_docker(args: argparse.Namespace) -> None:
    """Resolve Synology's non-PATH Docker binary for direct CLI usage."""

    explicit = getattr(args, "_explicit_options", set())
    docker_explicit = any(option in explicit for option in CONFIG_OPTIONS["docker_path"])
    target_explicit = any(option in explicit for option in CONFIG_OPTIONS["nas_target"])
    if (docker_explicit or docker_path(args) != DOCKER_PATH) and (
        target_explicit or args.nas_target != DEFAULTS["nas_target"]
    ):
        return
    detected = probe_remote(args)
    candidate = detected.get("synology_docker") or detected.get("docker")
    if not docker_explicit and docker_path(args) == DOCKER_PATH and candidate:
        args.docker_path = candidate
        print(f"已自动发现远端 Docker: {candidate}")
    target = detected.get("multica_target")
    if (
        not target_explicit
        and args.nas_target == DEFAULTS["nas_target"]
        and target
        and detected.get("synology_docker")
    ):
        args.nas_target = target
        print(f"已自动发现 Multica 部署目录: {target}")


def privileged(args: argparse.Namespace, command: str) -> str:
    """Prefix a remote command with non-interactive sudo unless disabled."""

    if getattr(args, "no_sudo", False):
        return command
    return f"sudo -n {command}"


def scp_base(args: argparse.Namespace) -> list[str]:
    command = ["scp", "-O"]
    if args.ssh_port:
        command += ["-P", str(args.ssh_port)]
    return command


def run(command: list[str], *, label: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True)
    except FileNotFoundError as exc:
        raise ConfigError(f"执行 {label} 失败：找不到可执行文件。") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{label} 失败（退出码 {exc.returncode}）。") from exc


def remote(args: argparse.Namespace, command: str) -> None:
    run(ssh_base(args) + [command], label="NAS 命令")


def remote_capture(args: argparse.Namespace, command: str) -> str:
    try:
        result = subprocess.run(
            ssh_base(args) + [command],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"NAS 命令失败（退出码 {exc.returncode}）。") from exc
    return result.stdout


def remote_stdin(args: argparse.Namespace, script: str, *, label: str) -> None:
    """Send a shell script over SSH stdin so secrets never enter argv/logs."""

    try:
        subprocess.run(
            ssh_base(args) + ["sh", "-s"],
            # Do not let Windows text mode translate LF to CRLF; BusyBox ash
            # treats the carriage return as part of every shell token.
            input=script.encode("utf-8"),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{label} 失败（退出码 {exc.returncode}）。") from exc


def copy_to_nas(
    args: argparse.Namespace, local_path: Path, *, remote_name: str | None = None
) -> None:
    destination = f"{args.nas_host}:{args.nas_target}/{remote_name or local_path.name}"
    run(scp_base(args) + [str(local_path), destination], label=f"复制 {local_path.name}")


def compose(args: argparse.Namespace) -> str:
    target = q(args.nas_target)
    docker = q(docker_path(args))
    return (
        f"cd {target} && {privileged(args, f'{docker} compose')} --env-file .env "
        f"-f docker-compose.selfhost.yml -f docker-compose.nas.yml"
    )


def release_state_path(args: argparse.Namespace, name: str) -> str:
    """Return the path for non-secret deployment release metadata on the NAS."""

    if name not in {"previous", "current"}:
        raise ConfigError("非法 release state 名称。")
    return f"{args.nas_target}/.multica-release.{name}"


def backup_release_state(args: argparse.Namespace) -> None:
    """Save the currently deployed image references before changing Compose."""

    previous = release_state_path(args, "previous")
    script = """
set -eu
target='__TARGET__'
env_file="$target/.env"
state='__STATE__'
if [ -f "$env_file" ]; then
  tag=$(sed -n 's/^MULTICA_IMAGE_TAG=//p' "$env_file" | tail -n 1)
  backend=$(sed -n 's/^MULTICA_BACKEND_IMAGE=//p' "$env_file" | tail -n 1)
  web=$(sed -n 's/^MULTICA_WEB_IMAGE=//p' "$env_file" | tail -n 1)
  if [ -n "$tag" ]; then
    umask 077
    printf 'MULTICA_IMAGE_TAG=%s\nMULTICA_BACKEND_IMAGE=%s\nMULTICA_WEB_IMAGE=%s\n' \
      "$tag" "$backend" "$web" > "$state"
    chmod 600 "$state"
  fi
fi
""".strip()
    remote(
        args,
        script.replace("__TARGET__", args.nas_target).replace("__STATE__", previous),
    )


def write_current_release_state(args: argparse.Namespace) -> None:
    """Record the deployed image references without storing any secret."""

    current = release_state_path(args, "current")
    backend = getattr(args, "backend_image", DEFAULTS["backend_image"])
    web = getattr(args, "web_image", DEFAULTS["web_image"])
    script = """
set -eu
state='__STATE__'
umask 077
printf 'MULTICA_IMAGE_TAG=%s\nMULTICA_BACKEND_IMAGE=%s\nMULTICA_WEB_IMAGE=%s\n' \
  '__TAG__' '__BACKEND__' '__WEB__' > "$state"
chmod 600 "$state"
""".strip()
    replacements = {
        "__STATE__": current,
        "__TAG__": args.image_tag,
        "__BACKEND__": backend,
        "__WEB__": web,
    }
    for key, value in replacements.items():
        script = script.replace(key, value)
    remote(args, script)


def read_release_state(args: argparse.Namespace) -> dict[str, str]:
    """Read the last deployment metadata; secrets are never part of this file."""

    previous = release_state_path(args, "previous")
    output = remote_capture(
        args,
        f"if test -f {q(previous)}; then cat {q(previous)}; fi",
    )
    state: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"MULTICA_IMAGE_TAG", "MULTICA_BACKEND_IMAGE", "MULTICA_WEB_IMAGE"}:
            state[key] = value
    if not state.get("MULTICA_IMAGE_TAG"):
        raise ConfigError("NAS 上没有可回滚的上一版本记录。先成功部署一次新版本后才能回滚。")
    return state


def check_package() -> None:
    for name in (*REQUIRED_FILES, "Caddyfile"):
        path = PACKAGE_ROOT / name
        if not path.is_file():
            raise ConfigError(f"部署包缺少文件: {path}")


def render_caddy(args: argparse.Namespace) -> Path:
    template = (PACKAGE_ROOT / "Caddyfile").read_text(encoding="utf-8")
    rendered, count = re.subn(
        r"(?m)^http://[^:\r\n]+:\d+ \{",
        f"http://{args.nas_ip}:{args.app_port} {{",
        template,
        count=1,
    )
    if count != 1:
        raise ConfigError("Caddyfile 没有找到可渲染的 HTTP 监听地址。")
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".Caddyfile", prefix="multica-", delete=False
    )
    try:
        handle.write(rendered)
    finally:
        handle.close()
    return Path(handle.name)


def initialize_remote_env(args: argparse.Namespace) -> None:
    script = """
set -eu
target='__TARGET__'
env_file="$target/.env"
if [ ! -f "$env_file" ]; then
  umask 077
  cp "$target/.env.template" "$env_file"
  printf '\\nJWT_SECRET=%s\\n' "$(openssl rand -hex 32)" >> "$env_file"
  printf 'POSTGRES_PASSWORD=%s\\n' "$(openssl rand -hex 24)" >> "$env_file"
  printf 'MULTICA_VCS_SECRET_KEY=%s\\n' "$(openssl rand -hex 32)" >> "$env_file"
fi
upsert() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\\n' "$key" "$value" >> "$env_file"
  fi
}
upsert MULTICA_IMAGE_TAG '__IMAGE_TAG__'
upsert MULTICA_BACKEND_IMAGE '__BACKEND_IMAGE__'
upsert MULTICA_WEB_IMAGE '__WEB_IMAGE__'
upsert BACKEND_PORT '__BACKEND_PORT__'
upsert FRONTEND_PORT '__FRONTEND_PORT__'
upsert FRONTEND_ORIGIN 'http://__NAS_IP__:__APP_PORT__'
upsert CORS_ALLOWED_ORIGINS 'http://__NAS_IP__:__APP_PORT__'
upsert MULTICA_APP_URL 'http://__NAS_IP__:__APP_PORT__'
chmod 600 "$env_file"
chmod 640 "$target/docker-compose.selfhost.yml" "$target/docker-compose.nas.yml" "$target/Caddyfile" "$target/.env.template"
__CHOWN__
""".strip()
    replacements = {
        "__TARGET__": args.nas_target,
        "__IMAGE_TAG__": args.image_tag,
        "__BACKEND_IMAGE__": getattr(args, "backend_image", DEFAULTS["backend_image"]),
        "__WEB_IMAGE__": getattr(args, "web_image", DEFAULTS["web_image"]),
        "__BACKEND_PORT__": str(args.backend_port),
        "__FRONTEND_PORT__": str(args.frontend_port),
        "__NAS_IP__": args.nas_ip,
        "__APP_PORT__": str(args.app_port),
        "__OWNER__": args.owner,
        "__GROUP__": args.group,
        "__CHOWN__": (
            ":"
            if getattr(args, "no_sudo", False)
            else f"sudo -n chown {q(args.owner)}:{q(args.group)} {q(args.nas_target + '/.env')} "
            f"{q(args.nas_target + '/docker-compose.selfhost.yml')} "
            f"{q(args.nas_target + '/docker-compose.nas.yml')} "
            f"{q(args.nas_target + '/Caddyfile')} {q(args.nas_target + '/.env.template')}"
        ),
    }
    for key, value in replacements.items():
        script = script.replace(key, value)
    remote(args, script)


def update_backend_env(
    args: argparse.Namespace,
    values: dict[str, str],
    *,
    private_key_remote: str | None = None,
) -> None:
    """Update selected backend settings, recreate only backend, and verify it."""

    check_binary("ssh")
    auto_detect_remote_docker(args)
    if any("\x00" in value or "\r" in value or "\n" in value for value in values.values()):
        raise ConfigError("环境变量值不能包含 NUL 或换行；GitHub 私钥请使用 --private-key-file。")
    script = """
set -eu
target='__TARGET__'
env_file="$target/.env"
test -f "$env_file"
upsert() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    printf '%s=%s\\n' "$key" "$value" >> "$env_file"
  fi
}
__UPSERTS__
if [ -n '__PRIVATE_KEY_FILE__' ]; then
  key_file='__PRIVATE_KEY_FILE__'
  test -s "$key_file"
  tmp_file=$(mktemp)
  awk '
    BEGIN { skip = 0 }
    $0 ~ /^GITHUB_APP_PRIVATE_KEY=/ {
      if ($0 ~ /-----BEGIN [A-Z ]+PRIVATE KEY-----/) { skip = 1 }
      next
    }
    skip && $0 ~ /^-----END [A-Z ]+PRIVATE KEY-----$/ { skip = 0; next }
    !skip { print }
  ' "$env_file" > "$tmp_file"
  printf 'GITHUB_APP_PRIVATE_KEY=%s\\n' "$(cat "$key_file")" >> "$tmp_file"
  mv "$tmp_file" "$env_file"
fi
chmod 600 "$env_file"
__CHOWN__
cd "$target"
__COMPOSE__ --env-file .env -f docker-compose.selfhost.yml -f docker-compose.nas.yml up -d --force-recreate backend
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 5 http://__NAS_IP__:__APP_PORT__/readyz; then exit 0; fi
  sleep 3
done
exit 1
""".strip()
    upserts = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            raise ConfigError(f"非法环境变量名: {key}")
        upserts.append(f"upsert {q(key)} {q(value)}")
    script = script.replace("__TARGET__", args.nas_target)
    script = script.replace("__UPSERTS__", "\n".join(upserts))
    script = script.replace("__PRIVATE_KEY_FILE__", private_key_remote or "")
    script = script.replace("__OWNER__", getattr(args, "owner", DEFAULTS["owner"]))
    script = script.replace("__GROUP__", getattr(args, "group", DEFAULTS["group"]))
    script = script.replace("__DOCKER__", q(docker_path(args)))
    owner = getattr(args, "owner", DEFAULTS["owner"])
    group = getattr(args, "group", DEFAULTS["group"])
    script = script.replace(
        "__CHOWN__",
        (
            ":"
            if getattr(args, "no_sudo", False)
            else f"sudo -n chown {q(owner)}:{q(group)} {q(args.nas_target + '/.env')}"
        ),
    )
    script = script.replace(
        "__COMPOSE__",
        privileged(args, q(docker_path(args)) + " compose"),
    )
    script = script.replace("__NAS_IP__", args.nas_ip)
    script = script.replace("__APP_PORT__", str(args.app_port))
    remote_stdin(args, script, label="更新 backend 配置")


def configure_email(args: argparse.Namespace, *, production_mode: bool = False) -> None:
    check_binary("ssh")
    check_package()
    if args.provider == "smtp":
        username = args.smtp_username or input("SMTP 用户名（通常是完整邮箱地址）: ").strip()
        if not username:
            raise ConfigError("SMTP 用户名不能为空。")
        sender = args.smtp_from or input(f"发件人地址（回车使用 {username}）: ").strip() or username
        password = getpass.getpass("SMTP 密码/授权码（不会显示，也不会写入命令行）: ")
        if not password:
            raise ConfigError("SMTP 密码/授权码不能为空。")
        values = {
            "RESEND_API_KEY": "",
            "RESEND_FROM_EMAIL": sender,
            "SMTP_HOST": args.smtp_host,
            "SMTP_PORT": str(args.smtp_port),
            "SMTP_USERNAME": username,
            "SMTP_PASSWORD": password,
            "SMTP_FROM_EMAIL": sender,
            "SMTP_TLS": args.smtp_tls,
            "SMTP_TLS_INSECURE": "false",
        }
    else:
        sender = args.resend_from or input("Resend 发件人地址（必须属于已验证域名）: ").strip()
        if not sender:
            raise ConfigError("Resend 发件人地址不能为空。")
        api_key = getpass.getpass("Resend API key（不会显示，也不会写入命令行）: ")
        if not api_key:
            raise ConfigError("Resend API key 不能为空。")
        values = {
            "RESEND_API_KEY": api_key,
            "RESEND_FROM_EMAIL": sender,
            "SMTP_HOST": "",
            "SMTP_PORT": "25",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM_EMAIL": "",
            "SMTP_TLS": "",
            "SMTP_TLS_INSECURE": "false",
        }
    if production_mode:
        # The upstream shortcut code is intentionally disabled by APP_ENV=production.
        # Clear it when switching back from the LAN test mode so a later deploy cannot
        # accidentally keep accepting a fixed code.
        values.update({"APP_ENV": "production", "MULTICA_DEV_VERIFICATION_CODE": ""})
    update_backend_env(args, values)
    print("邮箱配置已写入 NAS，并已重建 backend。现在可以重新发送验证码测试。")


def configure_gitea_auth(args: argparse.Namespace) -> None:
    """Configure the self-hosted Gitea OAuth2/OIDC login provider."""

    issuer = prompt_required("Gitea 地址（例如 http://gitea.internal:3000）").rstrip("/")
    parsed = urllib.parse.urlparse(issuer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigError("Gitea 地址必须是没有查询参数或片段的 http(s) URL。")
    client_id = prompt_required("Gitea OAuth Client ID")
    client_secret = getpass.getpass("Gitea OAuth Client Secret（不会显示，也不会写入命令行）: ")
    if not client_secret:
        raise ConfigError("Gitea OAuth Client Secret 不能为空。")
    default_redirect = f"http://{args.nas_ip}:{args.app_port}/auth/callback"
    redirect_uri = prompt_default("Multica 回调地址（必须与 Gitea 应用完全一致）", default_redirect)
    redirect = urllib.parse.urlparse(redirect_uri)
    if redirect.scheme not in {"http", "https"} or not redirect.netloc or redirect.query or redirect.fragment:
        raise ConfigError("Multica 回调地址必须是没有查询参数或片段的 http(s) URL。")
    update_backend_env(
        args,
        {
            "GITEA_ISSUER_URL": issuer,
            "GITEA_CLIENT_ID": client_id,
            "GITEA_CLIENT_SECRET": client_secret,
            "GITEA_REDIRECT_URI": redirect_uri,
        },
    )
    print("Gitea 登录已写入 NAS，并已重建 backend。刷新登录页即可看到“使用 Gitea 登录”。")


def configure_plugins(args: argparse.Namespace) -> None:
    """Enable the V1 plugin flags without rebuilding the backend image."""

    base = console_choice(
        prompt_default("启用 Plugins V1 管理（y/N）", "n").lower(),
        "yn",
    )
    base_enabled = base == "y"
    private_default = "y" if base_enabled else "n"
    private = console_choice(
        prompt_default("启用 Private Skill Plugins（y/N）", private_default).lower(),
        "yn",
    )
    private_enabled = base_enabled and private == "y"
    update_backend_env(
        args,
        {
            "FF_PLUGINS_V1": "true" if base_enabled else "false",
            "FF_PRIVATE_PLUGINS_V1": "true" if private_enabled else "false",
        },
    )
    if private_enabled:
        print("Private Skill Plugins 已启用。V1 只加载声明式 Skill，不会执行插件包中的任意代码。")
    elif base_enabled:
        print("Plugins V1 已启用；Private Skill Plugins 保持关闭。")
    else:
        print("Plugins V1 已关闭。")


def configure_login_mode(args: argparse.Namespace) -> None:
    """Configure a LAN-friendly login path without pretending it is password auth."""

    choice = console_choice(
        prompt_default(
            "登录验证（1=Gitea，2=内网 SMTP，3=测试固定验证码，4=Resend，5=稍后）",
            "1",
        ),
        "12345",
    )
    if choice == "5":
        print("已跳过登录验证配置。没有 SMTP/Resend 时，验证码会写入 backend 日志。")
        print("以后可重新运行部署工具并选择“配置登录验证”，或查看 code 命令。")
        return
    if choice not in {"1", "2", "3", "4"}:
        raise ConfigError("登录验证只能选择 1（Gitea）、2（内网 SMTP）、3（测试固定验证码）、4（Resend）或 5（稍后）。")
    if choice == "1":
        try:
            configure_gitea_auth(args)
        except (ConfigError, RuntimeError) as exc:
            print(f"服务已部署，但 Gitea 登录配置未完成：{exc}", file=sys.stderr)
            print("可稍后在管理工具中选择“配置登录验证”重试。", file=sys.stderr)
        return
    if choice == "3":
        print("\n内网测试模式：验证码固定在 backend 环境变量中，不发邮件。")
        print("只适合局域网开发/验收；不要把它作为公网或生产登录方案。")
        code = prompt_required("测试验证码（必须是 6 位数字）")
        if not re.fullmatch(r"\d{6}", code):
            raise ConfigError("测试验证码必须正好是 6 位数字。")
        values = {
            "APP_ENV": "development",
            "MULTICA_DEV_VERIFICATION_CODE": code,
            "RESEND_API_KEY": "",
            "RESEND_FROM_EMAIL": "",
            "SMTP_HOST": "",
            "SMTP_PORT": "25",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM_EMAIL": "",
            "SMTP_TLS": "",
            "SMTP_TLS_INSECURE": "false",
        }
        update_backend_env(args, values)
        print("内网测试登录已启用。页面输入任意邮箱后，验证码使用你刚设置的 6 位数字。")
        return
    if choice == "2":
        try:
            smtp_port = int(prompt_default("内网 SMTP 端口", "25"))
        except ValueError as exc:
            raise ConfigError("SMTP 端口必须是数字。") from exc
        email_args = argparse.Namespace(
            **vars(args),
            provider="smtp",
            owner=getattr(args, "owner", DEFAULTS["owner"]),
            group=getattr(args, "group", DEFAULTS["group"]),
            smtp_host=prompt_required("内网 SMTP 主机（IP 或主机名）"),
            smtp_port=smtp_port,
            smtp_tls=prompt_default("SMTP TLS（implicit/starttls/none）", "starttls"),
            smtp_username=None,
            smtp_from=None,
            resend_from=None,
        )
    else:
        email_args = argparse.Namespace(
            **vars(args),
            provider="resend",
            owner=getattr(args, "owner", DEFAULTS["owner"]),
            group=getattr(args, "group", DEFAULTS["group"]),
            smtp_host="smtp.qq.com",
            smtp_port=465,
            smtp_tls="implicit",
            smtp_username=None,
            smtp_from=None,
            resend_from=None,
        )
    try:
        configure_email(email_args, production_mode=True)
    except (ConfigError, RuntimeError) as exc:
        print(f"服务已部署，但邮箱配置未完成：{exc}", file=sys.stderr)
        print("可稍后在管理工具中选择“配置登录验证”重试。", file=sys.stderr)


# Backwards-compatible name used by older callers of the deployment package.
configure_guided_email = configure_login_mode


def configure_github(args: argparse.Namespace) -> None:
    check_binary("ssh")
    check_binary("scp")
    check_package()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        raise ConfigError("--slug 应是 GitHub App URL 最后的 slug，例如 multica-acme。")
    webhook_secret = getpass.getpass("GitHub Webhook secret（不会显示，也不会写入命令行）: ")
    if not webhook_secret:
        raise ConfigError("Webhook secret 不能为空。")
    values = {
        "GITHUB_APP_SLUG": args.slug,
        "GITHUB_WEBHOOK_SECRET": webhook_secret,
    }
    if args.app_id:
        if not args.app_id.isdigit():
            raise ConfigError("--app-id 必须是 GitHub App 的数字 ID。")
        values["GITHUB_APP_ID"] = args.app_id

    remote_key = None
    temporary_remote_name = None
    if args.private_key_file:
        key_path = Path(args.private_key_file).expanduser().resolve()
        if not key_path.is_file():
            raise ConfigError(f"找不到私钥文件: {key_path}")
        key_text = key_path.read_text(encoding="utf-8")
        if "-----BEGIN " not in key_text or "PRIVATE KEY-----" not in key_text:
            raise ConfigError("私钥文件不像 PEM 格式，请选择 GitHub 下载的 .pem 文件。")
        temporary_remote_name = f".multica-github-key-{uuid.uuid4().hex}.tmp"
        copy_to_nas(args, key_path, remote_name=temporary_remote_name)
        remote_key = f"{args.nas_target}/{temporary_remote_name}"
    try:
        update_backend_env(args, values, private_key_remote=remote_key)
    finally:
        if temporary_remote_name:
            remote(args, f"rm -f {q(args.nas_target + '/' + temporary_remote_name)}")
    print("GitHub 基础配置已写入 NAS，并已重建 backend。下一步在 Settings → GitHub 点击 Connect GitHub。")


def prompt_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def prompt_required(label: str, default: str = "") -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip() or default
        if value:
            return value
        print("这个值不能为空。")


def console_choice(value: str, allowed: str) -> str:
    """Tolerate a leading BOM/codepage marker when stdin comes from PowerShell."""

    cleaned = value.strip().lstrip("\ufeff")
    if cleaned in allowed:
        return cleaned
    return next((char for char in cleaned if char in allowed), cleaned)


def probe_remote(args: argparse.Namespace) -> dict[str, str]:
    """Best-effort, non-interactive probe used only to improve first-run defaults."""

    if shutil.which("ssh") is None or not args.nas_host:
        return {}
    probe_script = """
set +e
printf 'user=%s\n' "$(id -un 2>/dev/null)"
printf 'group=%s\n' "$(id -gn 2>/dev/null)"
if test -x /var/packages/ContainerManager/target/usr/bin/docker; then
  printf 'synology_docker=/var/packages/ContainerManager/target/usr/bin/docker\n'
fi
if command -v docker >/dev/null 2>&1; then
  printf 'docker=%s\n' "$(command -v docker)"
  if docker version >/dev/null 2>&1; then printf 'docker_direct=true\n'; fi
fi
for candidate in /volume1/docker/multica /opt/multica; do
  if test -f "$candidate/docker-compose.selfhost.yml" || test -f "$candidate/.env"; then
    printf 'multica_target=%s\n' "$candidate"
    break
  fi
done
""".strip()
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if args.ssh_port:
        command += ["-p", str(args.ssh_port)]
    command += [args.nas_host, "sh", "-s"]
    try:
        result = subprocess.run(
            command,
            input=probe_script.encode("utf-8"),
            capture_output=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    detected: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "user",
            "group",
            "docker",
            "synology_docker",
            "docker_direct",
            "multica_target",
        } and value:
            detected[key] = value.strip()
    return detected


def configure_connection(args: argparse.Namespace) -> None:
    """Collect target-specific connection settings without exposing secrets."""

    args.nas_host = prompt_required("SSH 主机、IP 或 SSH config 别名", args.nas_host)
    port_text = prompt_default("SSH 端口（0=使用 SSH config）", str(args.ssh_port))
    try:
        args.ssh_port = int(port_text)
    except ValueError as exc:
        raise ConfigError("SSH 端口必须是数字。") from exc
    detected = probe_remote(args)
    if detected:
        print(
            "已自动探测远端："
            + ", ".join(
                value
                for value in (
                    detected.get("user"),
                    detected.get("group"),
                    detected.get("synology_docker") or detected.get("docker"),
                )
                if value
            )
        )
    profile_default = "1" if detected.get("synology_docker") or "/volume1/" in args.nas_target else "2"
    profile = prompt_default(
        "部署目标（1=Synology NAS，2=普通 Linux）",
        profile_default,
    ).lower()
    profile = console_choice(profile, "12sSlL")
    if profile in {"1", "synology", "s"}:
        is_synology = True
        target_default = "/volume1/docker/multica"
        docker_default = "/var/packages/ContainerManager/target/usr/bin/docker"
        group_default = "users"
        owner_default = "" if getattr(args, "owner", DEFAULTS["owner"]) == DEFAULTS["owner"] else args.owner
    elif profile in {"2", "linux", "l"}:
        is_synology = False
        target_default = "/opt/multica"
        docker_default = "docker"
        group_default = DEFAULTS["group"]
        owner_default = getattr(args, "owner", DEFAULTS["owner"])
    else:
        raise ConfigError("部署目标只能选择 1（Synology）或 2（普通 Linux）。")
    host_ip = ""
    try:
        host_ip = args.nas_host if ipaddress.ip_address(args.nas_host).version == 4 else ""
    except ValueError:
        pass
    args.nas_ip = prompt_required("NAS 内网 IPv4 地址", args.nas_ip or host_ip)
    detected_docker = detected.get("synology_docker") if is_synology else detected.get("docker")
    target_default = args.nas_target if args.nas_target != DEFAULTS["nas_target"] else target_default
    docker_default = args.docker_path if args.docker_path != DOCKER_PATH else (detected_docker or docker_default)
    owner_default = detected.get("user") or owner_default
    group_default = detected.get("group") or group_default
    advanced = prompt_default("需要修改目录/Docker/用户组等高级参数？（y/N）", "n").lower() == "y"
    args.nas_target = prompt_default("NAS 部署目录", target_default) if advanced else target_default
    args.docker_path = prompt_default("Docker 命令/路径", docker_default) if advanced else docker_default
    args.owner = prompt_required("远端目录所有者/SSH 用户", owner_default)
    args.group = prompt_default("远端目录组", group_default) if advanced else group_default
    direct_default = detected.get("docker_direct") == "true"
    args.no_sudo = (
        prompt_default(
            "远端用户是否可以直接运行 Docker（y/N）",
            "y" if getattr(args, "no_sudo", direct_default) else "n",
        ).lower()
        == "y"
        if advanced
        else getattr(args, "no_sudo", direct_default)
    )
    validate_config(args)
    path = save_config(args)
    print(f"连接配置已保存到: {path}")


def guided_install(args: argparse.Namespace) -> None:
    """First-run flow used by install.py: configure, confirm, deploy, explain next step."""

    if not args.nas_host or not args.nas_ip:
        configure_connection(args)
    else:
        print(f"已读取配置: {args.nas_host} → {args.nas_ip}:{args.app_port}")
    deploy_now = prompt_default("现在部署/升级 Multica？", "Y").lower()
    if deploy_now in {"n", "no"}:
        print("配置已保存。以后重新运行 install.py 即可继续部署。")
        return
    deploy_args = argparse.Namespace(
        **vars(args),
        image_tag=prompt_default("镜像版本", DEFAULTS["image_tag"]),
        backend_port=DEFAULTS["backend_port"],
        frontend_port=DEFAULTS["frontend_port"],
        network_subnet=DEFAULTS["network_subnet"],
        owner=getattr(args, "owner", DEFAULTS["owner"]),
        group=getattr(args, "group", DEFAULTS["group"]),
        no_pull=prompt_default("跳过镜像拉取？（y/N）", "n").lower() == "y",
    )
    validate_config(deploy_args)
    save_config(deploy_args)
    deploy(deploy_args)
    configure_login_mode(args)
    print("\n下一步：")
    print(f"1. 浏览器打开 http://{args.nas_ip}:{args.app_port} 完成注册/登录。")
    print(f"2. Linux agent 运行: bash client-bootstrap.sh http://{args.nas_ip}:{args.app_port}")
    print(f"3. Windows agent 运行: .\\compat\\windows\\client-bootstrap.ps1 -ServerUrl http://{args.nas_ip}:{args.app_port}")


def wizard(args: argparse.Namespace) -> None:
    """Interactive operator menu; keeps routine work out of PowerShell syntax."""

    check_package()
    config_path = apply_saved_config(args)
    display_ip = args.nas_ip or "<未设置>"
    print("\nMultica NAS 管理工具")
    print(f"目标: {args.nas_host}  地址: {display_ip}:{args.app_port}")
    print(f"本地配置: {config_path}")
    if args.guided:
        guided_install(args)
        return
    while True:
        print(
            "\n1) 部署/升级\n"
            "2) 查看状态\n"
            "3) 配置登录验证\n"
            "4) 配置 GitHub App\n"
            "5) 配置 Plugins / Private Skill Plugins\n"
            "6) 查看脱敏日志\n"
            "7) 查看登录验证码\n"
            "8) 修改 NAS 连接参数\n"
            "9) 从本地 Multica 源码构建并部署\n"
            "d) 运行部署环境诊断\n"
            "r) 回滚上一版本\n"
            "0) 退出"
        )
        choice = input("选择 [0]: ").strip() or "0"
        # PowerShell pipelines can prepend a console-codepage marker to stdin;
        # keep the menu usable even when someone feeds a choice from a script.
        if choice not in {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "d", "r"}:
            choice = next((char for char in choice if char in "0123456789dr"), choice)
        try:
            if choice == "0":
                return
            if choice == "1":
                deploy_args = argparse.Namespace(
                    **vars(args),
                    image_tag=prompt_default("镜像版本", DEFAULTS["image_tag"]),
                    backend_port=DEFAULTS["backend_port"],
                    frontend_port=DEFAULTS["frontend_port"],
                    network_subnet=DEFAULTS["network_subnet"],
                    owner=getattr(args, "owner", DEFAULTS["owner"]),
                    group=getattr(args, "group", DEFAULTS["group"]),
                    no_pull=input("跳过镜像拉取？[y/N]: ").strip().lower() == "y",
                )
                validate_config(deploy_args)
                save_config(deploy_args)
                deploy(deploy_args)
            elif choice == "2":
                validate_config(args)
                status(args)
            elif choice == "3":
                validate_config(args)
                configure_login_mode(args)
            elif choice == "4":
                slug = input("GitHub App slug（URL 最后一段）: ").strip()
                app_id = input("GitHub App 数字 ID（可留空）: ").strip() or None
                key_file = input("PEM 私钥文件路径（可留空）: ").strip() or None
                github_args = argparse.Namespace(
                    **vars(args),
                    slug=slug,
                    app_id=app_id,
                    private_key_file=key_file,
                    owner=getattr(args, "owner", DEFAULTS["owner"]),
                    group=getattr(args, "group", DEFAULTS["group"]),
                )
                validate_config(github_args)
                configure_github(github_args)
            elif choice == "5":
                validate_config(args)
                configure_plugins(args)
            elif choice == "6":
                service = prompt_default("服务（backend/frontend/postgres/caddy）", "backend")
                since = prompt_default("时间范围（例如 15m）", "15m")
                logs_args = argparse.Namespace(**vars(args), service=service, since=since)
                validate_config(logs_args, require_ip=False)
                logs(logs_args)
            elif choice == "7":
                since = prompt_default("时间范围（例如 15m）", "15m")
                code_args = argparse.Namespace(**vars(args), since=since)
                validate_config(code_args, require_ip=False)
                verification_code(code_args)
            elif choice == "8":
                configure_connection(args)
                print(f"连接参数已更新: {args.nas_host} → {args.nas_ip}:{args.app_port}")
            elif choice == "9":
                source_default = getattr(args, "source_dir", "")
                source_dir = prompt_required("Multica 源码目录", source_default)
                build_args = argparse.Namespace(
                    **vars(args),
                    source_dir=source_dir,
                    image_tag=prompt_default("源码镜像标签", "dev"),
                )
                validate_config(build_args)
                build_source(build_args)
            elif choice == "d":
                validate_config(args)
                doctor(args)
            elif choice == "r":
                validate_config(args)
                rollback_args = argparse.Namespace(
                    **vars(args),
                    app_port=DEFAULTS["app_port"],
                    backend_port=DEFAULTS["backend_port"],
                    frontend_port=DEFAULTS["frontend_port"],
                    network_subnet=DEFAULTS["network_subnet"],
                    owner=getattr(args, "owner", DEFAULTS["owner"]),
                    group=getattr(args, "group", DEFAULTS["group"]),
                    yes=False,
                )
                rollback(rollback_args)
            else:
                print("请输入菜单中的数字。")
        except (ConfigError, RuntimeError, EOFError, KeyboardInterrupt) as exc:
            print(f"操作未完成: {exc}", file=sys.stderr)


def validate_source_checkout(source_dir: Path) -> None:
    """Check that a Multica checkout can be built by the self-host recipe."""

    if not source_dir.is_dir():
        raise ConfigError(f"找不到 Multica 源码目录: {source_dir}")
    required = (
        "Dockerfile",
        "Dockerfile.web",
        "docker-compose.selfhost.yml",
        "docker-compose.selfhost.build.yml",
    )
    missing = [name for name in required if not (source_dir / name).is_file()]
    if missing:
        raise ConfigError(
            f"Multica 源码目录缺少构建文件: {', '.join(missing)}。"
        )


def local_source_compose(source_dir: Path) -> list[str]:
    """Return the compose command that builds the current Multica checkout."""

    return [
        "docker",
        "compose",
        "-f",
        str(source_dir / "docker-compose.selfhost.yml"),
        "-f",
        str(source_dir / "docker-compose.selfhost.build.yml"),
    ]


def normalize_platform(value: str) -> str:
    """Normalize Docker architecture names to a buildx platform."""

    aliases = {
        "amd64": "linux/amd64",
        "x86_64": "linux/amd64",
        "arm64": "linux/arm64",
        "aarch64": "linux/arm64",
        "armv7": "linux/arm/v7",
        "armv7l": "linux/arm/v7",
    }
    cleaned = value.strip().lower()
    if cleaned in {"", "auto"}:
        return ""
    if cleaned in aliases:
        return aliases[cleaned]
    if re.fullmatch(r"linux/(?:amd64|arm64|arm/v7)", cleaned):
        return cleaned
    raise ConfigError(f"无法识别 Docker 架构/平台: {value}")


def local_docker_platform() -> str:
    """Return the Docker daemon platform used by the local builder."""

    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Arch}}"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(
            "本机 Docker 服务不可用。请启动 Docker Desktop 或 Linux Docker daemon 后重试。"
        ) from exc
    return normalize_platform(result.stdout)


def remote_docker_platform(args: argparse.Namespace) -> str:
    """Return the Docker daemon platform on the deployment target."""

    output = remote_capture(
        args,
        f"{privileged(args, q(docker_path(args)))} version --format '{{{{.Server.Arch}}}}'",
    )
    return normalize_platform(output)


def build_source_with_buildx(
    source_dir: Path, platform: str, backend_tag: str, web_tag: str
) -> None:
    """Build backend/web for a target architecture when the local daemon differs."""

    try:
        subprocess.run(
            ["docker", "buildx", "version"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError(
            f"本机 Docker 架构与 NAS 不同（目标 {platform}），需要 Docker buildx。"
        ) from exc
    run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            platform,
            "--load",
            "--tag",
            backend_tag,
            "--file",
            str(source_dir / "Dockerfile"),
            "--build-arg",
            "VERSION=dev",
            "--build-arg",
            "COMMIT=unknown",
            "--build-arg",
            "DATE=unknown",
            str(source_dir),
        ],
        label=f"构建 {backend_tag}（{platform}）",
    )
    run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            platform,
            "--load",
            "--tag",
            web_tag,
            "--file",
            str(source_dir / "Dockerfile.web"),
            "--build-arg",
            "NEXT_PUBLIC_APP_VERSION=dev",
            str(source_dir),
        ],
        label=f"构建 {web_tag}（{platform}）",
    )


def load_built_images_on_nas(args: argparse.Namespace, archive: Path) -> None:
    """Transfer a local image archive and load it without requiring a registry."""

    check_binary("ssh")
    check_binary("scp")
    auto_detect_remote_docker(args)
    remote_archive = f"/tmp/multica-images-{uuid.uuid4().hex}.tar"
    try:
        run(
            scp_base(args) + [str(archive), f"{args.nas_host}:{remote_archive}"],
            label="上传 Multica 镜像",
        )
        remote(
            args,
            f"{privileged(args, q(docker_path(args)))} load --input {q(remote_archive)}",
        )
    finally:
        try:
            remote(args, f"rm -f {q(remote_archive)}")
        except (ConfigError, RuntimeError):
            print(f"警告：无法删除 NAS 临时镜像文件 {remote_archive}。", file=sys.stderr)


def build_source(args: argparse.Namespace) -> None:
    """Build the current checkout, transfer the images, and deploy them to the NAS.

    This is the fast edit loop for maintainers: edit the Multica fork, run one
    command, and the tool handles Docker build, image transfer, Compose config,
    restart, and readiness checks. It deliberately uses local image tags so a
    registry is not required for an internal NAS.
    """

    check_binary("docker")
    check_binary("ssh")
    check_binary("scp")
    local_platform = local_docker_platform()
    source_dir = Path(args.source_dir).expanduser().resolve()
    validate_source_checkout(source_dir)
    auto_detect_remote_docker(args)
    requested_platform = normalize_platform(getattr(args, "platform", "auto"))
    remote_platform = remote_docker_platform(args)
    if requested_platform and requested_platform != remote_platform:
        raise ConfigError(
            f"--platform {requested_platform} 与 NAS 架构 {remote_platform} 不一致；"
            "请省略 --platform 使用自动探测，或确认目标主机架构。"
        )
    target_platform = remote_platform
    cross_platform = target_platform != local_platform
    print(f"构建架构: 本机 {local_platform} → NAS {target_platform}")
    save_config(args)

    deploy_tags = (f"multica-backend:{args.image_tag}", f"multica-web:{args.image_tag}")
    print(f"从源码构建 Multica: {source_dir}")
    if cross_platform:
        build_source_with_buildx(source_dir, target_platform, *deploy_tags)
    else:
        compose_command = local_source_compose(source_dir)
        run(compose_command + ["config", "--quiet"], label="校验 Multica 源码 Compose")
        run(
            compose_command + ["build", "backend", "frontend"],
            label="构建 Multica backend/web 镜像",
        )
        built_tags = ("multica-backend:dev", "multica-web:dev")
        for source_tag, deploy_tag in zip(built_tags, deploy_tags):
            run(["docker", "image", "inspect", source_tag], label=f"检查 {source_tag}")
            run(["docker", "tag", source_tag, deploy_tag], label=f"标记 {deploy_tag}")

    archive_handle = tempfile.NamedTemporaryFile(
        prefix="multica-images-", suffix=".tar", delete=False
    )
    archive_handle.close()
    archive = Path(archive_handle.name)
    try:
        print("打包本地镜像并上传到 NAS（不需要 Docker Registry）...")
        run(
            ["docker", "save", "-o", str(archive), *deploy_tags],
            label="打包 Multica 镜像",
        )
        load_built_images_on_nas(args, archive)
    finally:
        archive.unlink(missing_ok=True)

    deploy_args = argparse.Namespace(**vars(args))
    deploy_args.backend_image = "multica-backend"
    deploy_args.web_image = "multica-web"
    deploy_args.no_pull = True
    print("本地镜像已加载到 NAS，开始重启 Multica...")
    deploy(deploy_args)


def doctor(args: argparse.Namespace) -> None:
    """Run read-only local and NAS checks before a deploy or upgrade."""

    for binary in ("ssh", "scp"):
        check_binary(binary)
    check_package()
    auto_detect_remote_docker(args)
    print("本地依赖: ssh/scp 已找到")
    command = " && ".join(
        [
            (
                f"test -x {q(docker_path(args))}"
                if "/" in docker_path(args)
                else f"command -v {q(docker_path(args))} >/dev/null"
            ),
            f"printf 'docker=%s\\n' $({privileged(args, q(docker_path(args)))} version --format '{{{{.Server.Version}}}}')",
            f"printf 'platform=%s\\n' $({privileged(args, q(docker_path(args)))} version --format '{{{{.Server.Arch}}}}')",
            f"printf 'compose=%s\\n' $({privileged(args, q(docker_path(args)))} compose version --short)",
            "printf 'openssl=%s\\n' \"$(openssl version | head -n 1)\"",
            "printf 'curl=%s\\n' \"$(curl --version | head -n 1)\"",
            f"if test -f {q(args.nas_target + '/.env')}; then printf 'multica_env=present\\n'; else printf 'multica_env=missing\\n'; fi",
        ]
    )
    print(f"NAS 检查: {args.nas_host}")
    output = remote_capture(args, command)
    print(output, end="")
    try:
        with urllib.request.urlopen(
            f"http://{args.nas_ip}:{args.app_port}/readyz", timeout=5
        ) as response:
            print(f"readyz={response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"readyz=unreachable ({exc})", file=sys.stderr)
    print("诊断完成；没有修改 NAS。")


def upgrade(args: argparse.Namespace) -> None:
    """Named upgrade entry point; deploy already performs the safe update flow."""

    print(f"升级 Multica 到 {args.image_tag} ...")
    deploy(args)


def rollback(args: argparse.Namespace) -> None:
    """Restore the previous image references recorded by the last deployment."""

    check_binary("ssh")
    check_binary("scp")
    check_package()
    auto_detect_remote_docker(args)
    state = read_release_state(args)
    tag = state["MULTICA_IMAGE_TAG"]
    backend = state.get("MULTICA_BACKEND_IMAGE", "")
    web = state.get("MULTICA_WEB_IMAGE", "")
    if not getattr(args, "yes", False):
        answer = input(f"确认回滚到 {tag}？输入 yes 继续: ").strip().lower()
        if answer != "yes":
            print("已取消回滚。")
            return
    rollback_args = argparse.Namespace(**vars(args))
    rollback_args.image_tag = tag
    rollback_args.backend_image = backend
    rollback_args.web_image = web
    rollback_args.no_pull = (
        backend in {"multica-backend", "multica-backend:dev"}
        or web in {"multica-web", "multica-web:dev"}
    )
    validate_config(rollback_args)
    print(f"回滚到 {tag}（backend={backend or '官方镜像'}, web={web or '官方镜像'}）")
    deploy(rollback_args)


def deploy(args: argparse.Namespace) -> None:
    check_binary("ssh")
    check_binary("scp")
    check_package()
    auto_detect_remote_docker(args)
    # Direct CLI deployments should be just as reusable as the guided wizard:
    # persist only the resolved, non-secret connection settings for later
    # status/logs/upgrade commands.
    save_config(args)
    print("[1/6] 检查 NAS SSH、Docker 和基础工具...")
    remote(
        args,
        " && ".join(
            [
                (
                    f"test -x {q(docker_path(args))}"
                    if "/" in docker_path(args)
                    else f"command -v {q(docker_path(args))} >/dev/null"
                ),
                f"{privileged(args, q(docker_path(args)))} version --format '{{{{.Server.Version}}}}' >/dev/null",
                "command -v openssl >/dev/null",
                "command -v curl >/dev/null",
            ]
        ),
    )

    print("[2/6] 创建受限部署目录...")
    remote(
        args,
        (
            f"sudo -n install -d -m 0750 -o {q(args.owner)} -g {q(args.group)} {q(args.nas_target)}"
            if not getattr(args, "no_sudo", False)
            else f"install -d -m 0750 {q(args.nas_target)}"
        ),
    )

    backup_release_state(args)
    print("[3/6] 上传 Compose、Caddy 和环境模板...")
    for name in REQUIRED_FILES:
        copy_to_nas(args, PACKAGE_ROOT / name)
    rendered_caddy = render_caddy(args)
    try:
        copy_to_nas(args, rendered_caddy)
        remote(
            args,
            f"mv {q(args.nas_target + '/' + rendered_caddy.name)} {q(args.nas_target + '/Caddyfile')}",
        )
    finally:
        rendered_caddy.unlink(missing_ok=True)

    print("[4/6] 初始化或更新非敏感配置（保留 NAS 上已有密钥）...")
    initialize_remote_env(args)
    remote(
        args,
        f"sed -i -E 's|subnet: [0-9.]+/[0-9]+|subnet: {args.network_subnet}|' "
        f"{q(args.nas_target + '/docker-compose.nas.yml')}",
    )

    compose_command = compose(args)
    print("[5/6] 校验 Compose 和 Caddy 配置...")
    remote(args, f"{compose_command} config --quiet")
    remote(
        args,
        f"{privileged(args, q(docker_path(args)))} run --rm --network host "
        f"-v {q(args.nas_target + '/Caddyfile:/etc/caddy/Caddyfile:ro')} "
        "caddy:2.10-alpine caddy validate --config /etc/caddy/Caddyfile",
    )
    if not args.no_pull:
        print(f"拉取固定版本镜像 {args.image_tag} ...")
        remote(args, f"{compose_command} pull")

    print("[6/6] 启动服务并等待数据库迁移完成...")
    remote(args, f"{compose_command} up -d --remove-orphans")
    health = textwrap.dedent(
        f"""
        for i in 1 2 3 4 5 6 7 8 9 10; do
          if curl -fsS --max-time 5 http://{args.nas_ip}:{args.app_port}/readyz; then exit 0; fi
          sleep 3
        done
        exit 1
        """
    ).strip()
    remote(args, health)
    write_current_release_state(args)
    print(f"部署完成: http://{args.nas_ip}:{args.app_port}")


def status(args: argparse.Namespace) -> None:
    check_binary("ssh")
    check_package()
    auto_detect_remote_docker(args)
    print(f"地址: http://{args.nas_ip}:{args.app_port}")
    print("\n容器:")
    remote(args, f"{compose(args)} ps")
    print("\n就绪检查:")
    remote(args, f"curl -fsS --max-time 5 http://{args.nas_ip}:{args.app_port}/readyz")
    try:
        with urllib.request.urlopen(
            f"http://{args.nas_ip}:{args.app_port}/health", timeout=5
        ) as response:
            print(f"管理机 HTTP: {response.status} {response.reason}")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"管理机访问失败（NAS 自检已完成）: {exc}", file=sys.stderr)


def logs(args: argparse.Namespace) -> None:
    check_binary("ssh")
    auto_detect_remote_docker(args)
    if args.service not in {"backend", "frontend", "postgres", "caddy"}:
        raise ConfigError("--service 必须是 backend、frontend、postgres 或 caddy。")
    if not re.fullmatch(r"\d+[smhd]", args.since):
        raise ConfigError("--since 必须类似 15m、2h。")
    command = (
        f"{compose(args)} logs --since {q(args.since)} --tail 200 {q(args.service)} "
        "| sed -E 's/(Verification code[^0-9]*)([0-9]{6})/\\1******/g'"
    )
    print(f"显示 {args.service} 最近 {args.since} 的日志（验证码已脱敏）...")
    remote(args, command)


def verification_code(args: argparse.Namespace) -> None:
    check_binary("ssh")
    auto_detect_remote_docker(args)
    if not re.fullmatch(r"\d+[smhd]", args.since):
        raise ConfigError("--since 必须类似 15m、2h。")
    print("警告：下面可能包含登录验证码，只在自己的终端查看，不要复制到聊天或工单。")
    command = (
        f"{privileged(args, q(docker_path(args)))} logs --since {q(args.since)} "
        "multica-backend-1 2>&1 | grep 'Verification code' | tail -n 5"
    )
    try:
        remote(args, command)
    except RuntimeError:
        print("没有找到验证码，可能已过期或邮件服务已配置。", file=sys.stderr)
        raise SystemExit(1)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-file", default=str(default_config_path()), help="本地非敏感部署配置文件")
    parser.add_argument("--nas-host", default=DEFAULTS["nas_host"], help="SSH 主机、IP 或别名")
    parser.add_argument("--ssh-port", type=int, default=0, help="SSH 端口；0 表示使用 SSH 配置")
    parser.add_argument("--nas-ip", default=DEFAULTS["nas_ip"], help="内网访问 IP")
    parser.add_argument("--nas-target", default=DEFAULTS["nas_target"], help="NAS 部署目录")
    parser.add_argument(
        "--docker-path",
        default=DOCKER_PATH,
        help="远端 Docker 命令或绝对路径；普通 Linux 默认 docker，Synology 请显式填写路径",
    )
    sudo_group = parser.add_mutually_exclusive_group()
    sudo_group.add_argument(
        "--no-sudo",
        dest="no_sudo",
        action="store_true",
        help="远端用户已能直接运行 Docker；普通 Linux 无需 sudo 时使用",
    )
    sudo_group.add_argument(
        "--sudo",
        dest="no_sudo",
        action="store_false",
        help="强制使用免密 sudo（覆盖本地配置）",
    )
    parser.set_defaults(no_sudo=False)


def add_deploy_options(parser: argparse.ArgumentParser) -> None:
    add_common(parser)
    parser.add_argument("--image-tag", default=DEFAULTS["image_tag"])
    parser.add_argument(
        "--backend-image",
        default=DEFAULTS["backend_image"],
        help="可选的 backend 镜像仓库；用于部署自维护/Gitea 补丁镜像",
    )
    parser.add_argument(
        "--web-image",
        default=DEFAULTS["web_image"],
        help="可选的 web 镜像仓库；用于部署自维护/Gitea 补丁镜像",
    )
    parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    parser.add_argument("--backend-port", type=int, default=DEFAULTS["backend_port"])
    parser.add_argument("--frontend-port", type=int, default=DEFAULTS["frontend_port"])
    parser.add_argument("--network-subnet", default=DEFAULTS["network_subnet"])
    parser.add_argument("--owner", default=DEFAULTS["owner"])
    parser.add_argument("--group", default=DEFAULTS["group"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multica NAS 内网部署工具（Python 标准库，无 pip 依赖）"
    )
    subparsers = parser.add_subparsers(dest="command")

    deploy_parser = subparsers.add_parser("deploy", help="部署或升级 Multica")
    add_deploy_options(deploy_parser)
    deploy_parser.add_argument("--no-pull", action="store_true", help="跳过镜像拉取")

    upgrade_parser = subparsers.add_parser("upgrade", help="升级到指定 Multica 镜像版本")
    add_deploy_options(upgrade_parser)
    upgrade_parser.add_argument("--no-pull", action="store_true", help="跳过镜像拉取")

    build_parser = subparsers.add_parser(
        "build",
        help="从本地 Multica 源码构建、上传并部署（修改源码后的快捷流程）",
    )
    add_deploy_options(build_parser)
    build_parser.add_argument(
        "--source-dir",
        default=DEFAULTS["source_dir"],
        help="Multica 源码目录；首次使用必须提供，之后会保存到本地配置",
    )
    build_parser.add_argument(
        "--platform",
        choices=("auto", "linux/amd64", "linux/arm64", "linux/arm/v7"),
        default=DEFAULTS["platform"],
        help="构建目标架构；auto 自动读取 NAS，架构不同时使用 buildx",
    )

    doctor_parser = subparsers.add_parser("doctor", help="只读检查本机和 NAS 部署环境")
    add_common(doctor_parser)
    doctor_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])

    rollback_parser = subparsers.add_parser("rollback", help="回滚到上一次成功部署的镜像")
    add_common(rollback_parser)
    rollback_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    rollback_parser.add_argument("--backend-port", type=int, default=DEFAULTS["backend_port"])
    rollback_parser.add_argument("--frontend-port", type=int, default=DEFAULTS["frontend_port"])
    rollback_parser.add_argument("--network-subnet", default=DEFAULTS["network_subnet"])
    rollback_parser.add_argument("--owner", default=DEFAULTS["owner"])
    rollback_parser.add_argument("--group", default=DEFAULTS["group"])
    rollback_parser.add_argument("--yes", action="store_true", help="跳过交互确认")

    status_parser = subparsers.add_parser("status", help="查看状态和健康检查")
    add_common(status_parser)
    status_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])

    logs_parser = subparsers.add_parser("logs", help="查看脱敏日志")
    add_common(logs_parser)
    logs_parser.add_argument("--service", default="backend")
    logs_parser.add_argument("--since", default="15m")

    code_parser = subparsers.add_parser("code", help="查看最近登录验证码")
    add_common(code_parser)
    code_parser.add_argument("--since", default="15m")

    email_parser = subparsers.add_parser("email", help="安全配置 SMTP 或 Resend")
    add_common(email_parser)
    email_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    email_parser.add_argument("--owner", default=DEFAULTS["owner"])
    email_parser.add_argument("--group", default=DEFAULTS["group"])
    email_parser.add_argument("--provider", choices=("smtp", "resend"), default="smtp")
    email_parser.add_argument("--smtp-host", default="smtp.qq.com")
    email_parser.add_argument("--smtp-port", type=int, default=465)
    email_parser.add_argument("--smtp-tls", default="implicit")
    email_parser.add_argument("--smtp-username")
    email_parser.add_argument("--smtp-from")
    email_parser.add_argument("--resend-from")

    login_parser = subparsers.add_parser("login", help="配置内网/外部登录验证方式")
    add_common(login_parser)
    login_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    login_parser.add_argument("--owner", default=DEFAULTS["owner"])
    login_parser.add_argument("--group", default=DEFAULTS["group"])

    gitea_parser = subparsers.add_parser("gitea", help="配置 Gitea OAuth2/OIDC 登录")
    add_common(gitea_parser)
    gitea_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    gitea_parser.add_argument("--owner", default=DEFAULTS["owner"])
    gitea_parser.add_argument("--group", default=DEFAULTS["group"])

    plugins_parser = subparsers.add_parser("plugins", help="启用或关闭 Plugins V1 / Private Skill Plugins")
    add_common(plugins_parser)
    plugins_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    plugins_parser.add_argument("--owner", default=DEFAULTS["owner"])
    plugins_parser.add_argument("--group", default=DEFAULTS["group"])

    github_parser = subparsers.add_parser("github", help="配置 GitHub App 基础参数")
    add_common(github_parser)
    github_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    github_parser.add_argument("--owner", default=DEFAULTS["owner"])
    github_parser.add_argument("--group", default=DEFAULTS["group"])
    github_parser.add_argument("--slug", required=True, help="GitHub App URL 最后的 slug")
    github_parser.add_argument("--app-id", help="GitHub App 数字 ID；启用 CI/mergeability 时需要")
    github_parser.add_argument("--private-key-file", help="GitHub 下载的 PEM 私钥文件路径")

    wizard_parser = subparsers.add_parser("wizard", help="打开交互式管理菜单")
    add_common(wizard_parser)
    wizard_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    wizard_parser.add_argument("--source-dir", default=DEFAULTS["source_dir"])
    wizard_parser.add_argument("--guided", action="store_true", help=argparse.SUPPRESS)

    return parser


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", line_buffering=True)
    raw_args = list(sys.argv[1:] if argv is None else argv)
    # The no-subcommand form is intentionally the easy path for first-time
    # operators: ``python multica_deploy.py`` means deploy.
    if not raw_args or (
        raw_args[0].startswith("-") and raw_args[0] not in {"-h", "--help"}
    ):
        raw_args.insert(0, "deploy")
    parser = build_parser()
    args = parser.parse_args(raw_args)
    args._explicit_options = {
        option
        for options in CONFIG_OPTIONS.values()
        for option in options
        if option_supplied(raw_args, option)
    }
    try:
        if args.command != "wizard":
            apply_saved_config(args)
        if args.command == "deploy":
            validate_config(args)
            deploy(args)
        elif args.command == "upgrade":
            validate_config(args)
            upgrade(args)
        elif args.command == "build":
            validate_config(args)
            if not args.source_dir:
                raise ConfigError(
                    "源码构建需要 --source-dir，例如 --source-dir ..\\multica。"
                )
            build_source(args)
        elif args.command == "doctor":
            validate_config(args)
            doctor(args)
        elif args.command == "rollback":
            rollback(args)
        elif args.command == "status":
            validate_config(args)
            status(args)
        elif args.command == "logs":
            validate_config(args, require_ip=False)
            logs(args)
        elif args.command == "code":
            validate_config(args, require_ip=False)
            verification_code(args)
        elif args.command == "email":
            validate_config(args)
            configure_email(args)
        elif args.command == "login":
            validate_config(args)
            configure_login_mode(args)
        elif args.command == "gitea":
            validate_config(args)
            configure_gitea_auth(args)
        elif args.command == "plugins":
            validate_config(args)
            configure_plugins(args)
        elif args.command == "github":
            validate_config(args)
            configure_github(args)
        elif args.command == "wizard":
            wizard(args)
        else:
            parser.print_help()
        return 0
    except (ConfigError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
