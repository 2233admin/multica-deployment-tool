#!/usr/bin/env python3
"""Zero-dependency Multica local deployment kit.

Normal deployments use Docker Compose on the NAS. The ``build`` command is a
maintainer edit loop: it builds on the management computer, transfers only the
resulting images, and asks the NAS to run them. The tool only needs Python
3.9+, Docker, OpenSSH (ssh/scp), and an SSH account that can run Docker.
Synology uses ``sudo -n`` by default; generic Linux can use ``--no-sudo`` when
the SSH user already owns the target directory and can run Docker directly.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
DOCKER_PATH = "docker"
PRODUCT_NAME = "Multica 本地版一键部署包"
LEGACY_REPOSITORY_NAME = "multica-deployment-tool"
DEFAULTS = {
    "nas_host": "",
    "nas_ip": "",
    "netbird": False,
    "nas_target": "/opt/multica",
    "source_dir": "",
    # Keep the default aligned with the current official Multica release.  A
    # caller can still pin another tag explicitly for rollback or local builds.
    "image_tag": "v0.4.29",
    "backend_image": "",
    "web_image": "",
    "github_device_flow_enabled": False,
    "github_device_flow_client_id": "",
    "public_url": "",
    "browser_url": "",
    "service_url": "",
    "oauth_origin": "",
    "plane_url": "",
    "app_port": 0,
    "backend_port": 3011,
    "frontend_port": 3012,
    "network_subnet": "10.253.0.0/24",
    "owner": "multica",
    "group": "multica",
    "platform": "auto",
    "desktop_sync": True,
    "desktop_version": "",
    "desktop_profile": "desktop-api.multica.ai",
}
REQUIRED_FILES = (
    "docker-compose.selfhost.yml",
    "docker-compose.nas.yml",
    ".env.template",
)
WATCHDOG_FILES = (
    "ops/multica-watchdog.sh",
    "ops/S99multica-watchdog.sh",
)
CONFIG_KEYS = (
    "nas_host",
    "ssh_port",
    "nas_ip",
    "netbird",
    "nas_target",
    "source_dir",
    "docker_path",
    "no_sudo",
    "owner",
    "group",
    "backend_image",
    "web_image",
    "github_device_flow_enabled",
    "github_device_flow_client_id",
    "public_url",
    "browser_url",
    "service_url",
    "oauth_origin",
    "plane_url",
    "app_port",
    "backend_port",
    "frontend_port",
    "network_subnet",
    "desktop_sync",
    "desktop_profile",
)
CONFIG_OPTIONS = {
    "nas_host": ("--nas-host",),
    "ssh_port": ("--ssh-port",),
    "nas_ip": ("--nas-ip",),
    "netbird": ("--netbird", "--no-netbird"),
    "nas_target": ("--nas-target",),
    "source_dir": ("--source-dir",),
    "docker_path": ("--docker-path",),
    "no_sudo": ("--no-sudo", "--sudo"),
    "owner": ("--owner",),
    "group": ("--group",),
    "backend_image": ("--backend-image",),
    "web_image": ("--web-image",),
    "github_device_flow_enabled": ("--github-device-flow", "--no-github-device-flow"),
    "github_device_flow_client_id": ("--github-device-flow-client-id",),
    "public_url": ("--public-url",),
    "browser_url": ("--browser-url", "--browser-origin"),
    "service_url": ("--service-url",),
    "oauth_origin": ("--oauth-origin", "--oauth-callback-origin"),
    "plane_url": ("--plane-url",),
    "app_port": ("--app-port",),
    "backend_port": ("--backend-port",),
    "frontend_port": ("--frontend-port",),
    "network_subnet": ("--network-subnet",),
    "desktop_sync": ("--desktop-sync", "--no-desktop-sync"),
    "desktop_profile": ("--desktop-profile",),
}

DESKTOP_RELEASE_API = "https://api.github.com/repos/multica-ai/multica/releases"
DESKTOP_HEALTH_PORT = 19681


class ConfigError(ValueError):
    """An operator-supplied setting is invalid."""


@dataclass(frozen=True)
class TargetAddresses:
    """Resolved address roles for one Multica installation.

    ``bind_address`` is the address Caddy binds on the target host. The other
    values are URL origins and intentionally remain independent: a browser may
    use a NetBird IP or domain while the target uses a LAN address for health
    checks and server-to-server integrations.
    """

    bind_address: str
    browser_origin: str
    service_origin: str
    oauth_origin: str
    plane_url: str = ""

    @property
    def oauth_callback_url(self) -> str:
        return self.oauth_origin.rstrip("/") + "/auth/callback"


def _origin(value: str, label: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ConfigError(f"{label} 不是有效的 http(s) URL。") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{label} 必须是完整的 http(s) URL。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(f"{label} 只能填写 origin，不能包含用户、密码、查询参数或片段。")
    if parsed.path not in {"", "/"}:
        raise ConfigError(f"{label} 只能填写 origin，不能包含路径。")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _host(value: str, label: str) -> str:
    value = value.strip()
    if not value or any(char in value for char in "\\/ \t\r\n@"):
        raise ConfigError(f"{label} 必须是主机名或 IP 地址。")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ConfigError(f"{label} 必须是主机名或 IP 地址。")
    return value


def _origin_for_host(host: str, port: int, scheme: str = "http") -> str:
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{rendered_host}:{port}"


def resolve_target_addresses(args: argparse.Namespace) -> TargetAddresses:
    """Resolve address roles without inventing a product-specific endpoint."""

    browser_value = _origin(getattr(args, "browser_url", ""), "--browser-url")
    service_value = _origin(getattr(args, "service_url", ""), "--service-url")
    oauth_value = _origin(
        getattr(args, "oauth_origin", ""), "--oauth-origin"
    )
    plane_value = _origin(getattr(args, "plane_url", ""), "--plane-url")
    bind_value = getattr(args, "nas_ip", "").strip()
    if bind_value:
        bind_value = _host(bind_value, "--nas-ip/--bind-address")
    candidates = []
    for value in (service_value, browser_value):
        if value:
            candidates.append(urllib.parse.urlsplit(value).hostname or "")
    if not bind_value:
        bind_value = next((candidate for candidate in candidates if candidate), "")
    if not bind_value:
        raise ConfigError(
            "必须提供 --nas-ip（目标绑定/服务地址），或提供 --browser-url/--service-url 让工具从 URL 推导。"
        )
    app_port = getattr(args, "app_port", DEFAULTS["app_port"])
    browser_origin = browser_value or _origin_for_host(bind_value, app_port)
    service_origin = service_value or _origin_for_host(bind_value, app_port)
    oauth_origin = oauth_value or browser_origin
    if getattr(args, "netbird", False):
        try:
            address = ipaddress.ip_address(bind_value)
        except ValueError as exc:
            raise ConfigError("--netbird 需要 --nas-ip 填写 NAS 的 NetBird IPv4 地址。") from exc
        if address.version != 4:
            raise ConfigError("--netbird 只支持 NAS 的 NetBird IPv4 地址。")
    return TargetAddresses(
        bind_address=bind_value,
        browser_origin=browser_origin,
        service_origin=service_origin,
        oauth_origin=oauth_origin,
        plane_url=plane_value,
    )


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
        "github_device_flow_client_id",
        "public_url",
        "browser_url",
        "service_url",
        "oauth_origin",
        "plane_url",
        "desktop_profile",
    }
    for key in string_keys:
        if key in result and not isinstance(result[key], str):
            raise ConfigError(f"部署配置字段 {key} 必须是字符串: {path}")
    for key in ("ssh_port", "app_port", "backend_port", "frontend_port"):
        if key in result and (
            isinstance(result[key], bool) or not isinstance(result[key], int)
        ):
            raise ConfigError(f"部署配置字段 {key} 必须是整数: {path}")
    if "github_device_flow_enabled" in result and not isinstance(
        result["github_device_flow_enabled"], bool
    ):
        raise ConfigError(
            f"部署配置字段 github_device_flow_enabled 必须是布尔值: {path}"
        )
    if "no_sudo" in result and not isinstance(result["no_sudo"], bool):
        raise ConfigError(f"部署配置字段 no_sudo 必须是布尔值: {path}")
    if "netbird" in result and not isinstance(result["netbird"], bool):
        raise ConfigError(f"部署配置字段 netbird 必须是布尔值: {path}")
    if "desktop_sync" in result and not isinstance(result["desktop_sync"], bool):
        raise ConfigError(f"部署配置字段 desktop_sync 必须是布尔值: {path}")
    return result


def save_config(args: argparse.Namespace) -> Path:
    """Persist non-secret deployment settings; credentials are deliberately excluded."""

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


def desktop_paths(profile: str) -> tuple[Path, Path, Path]:
    """Return the installed desktop executable, bundled CLI, and profile config."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ConfigError("Windows 桌面端同步需要 LOCALAPPDATA")
    install_root = Path(local_app_data) / "Programs" / "@multicadesktop"
    desktop_exe = install_root / "Multica.exe"
    cli_exe = install_root / "resources" / "app.asar.unpacked" / "resources" / "bin" / "multica.exe"
    profile_config = Path.home() / ".multica" / "profiles" / profile / "config.json"
    return desktop_exe, cli_exe, profile_config


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"读取桌面端配置失败: {path}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"桌面端配置必须是 JSON 对象: {path}")
    return payload


def _write_json_without_bom(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ConfigError(f"写入桌面端配置失败: {path}") from exc


def preserve_desktop_profile(server_url: str, profile: str) -> Path:
    """Keep the local token/workspace while restoring the self-hosted endpoint."""

    _, _, profile_config = desktop_paths(profile)
    current = _read_json(profile_config)
    global_config = _read_json(Path.home() / ".multica" / "config.json")
    normalized_server = server_url.rstrip("/")
    current_server = str(current.get("server_url", "")).rstrip("/")
    global_server = str(global_config.get("server_url", "")).rstrip("/")
    credentials = (
        current
        if current_server == normalized_server
        else global_config
        if global_server == normalized_server
        else current
    )
    token = credentials.get("token")
    if not isinstance(token, str) or not token:
        raise ConfigError(
            f"找不到桌面 profile 的本地 token: {profile_config}。请先在桌面端完成一次本地登录。"
        )

    if profile_config.is_file():
        backup = profile_config.with_name("config.json.pre-desktop-sync.bak")
        if not backup.exists():
            try:
                backup.write_bytes(profile_config.read_bytes())
            except OSError as exc:
                raise ConfigError(f"备份桌面端配置失败: {backup}") from exc
    merged = dict(current)
    merged.update(
        {"server_url": normalized_server, "app_url": normalized_server, "token": token}
    )
    if not merged.get("workspace_id") and global_config.get("workspace_id"):
        merged["workspace_id"] = global_config["workspace_id"]
    _write_json_without_bom(profile_config, merged)
    return profile_config


def _desktop_version(path: Path) -> str:
    if not path.is_file():
        return ""
    powershell = shutil.which("powershell") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if not Path(powershell).is_file() and shutil.which(powershell) is None:
        return ""
    command = f"(Get-Item -LiteralPath '{path}').VersionInfo.ProductVersion"
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _version_number(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?:\.\d+)?", value or "")
    return match.group(1) if match else ""


def _desktop_release(version: str) -> tuple[str, str, str]:
    requested = version.strip() if version else "latest"
    if requested.lower() in {"latest", "stable"}:
        url = f"{DESKTOP_RELEASE_API}/latest"
    else:
        tag = requested if requested.startswith("v") else f"v{requested}"
        url = f"{DESKTOP_RELEASE_API}/tags/{urllib.parse.quote(tag, safe='')}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "multica-deployment-tool",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"读取 Multica 桌面端 release 失败: {requested}") from exc
    tag = str(release.get("tag_name", ""))
    release_version = _version_number(tag)
    if not release_version:
        raise ConfigError(f"GitHub release 没有有效版本号: {tag}")
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    asset_name = f"multica-desktop-{release_version}-windows-{architecture}.exe"
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            digest = str(asset.get("digest", "")).removeprefix("sha256:").lower()
            return release_version, str(asset["browser_download_url"]), digest
    raise ConfigError(f"release {tag} 没有 Windows {architecture} 桌面安装包")


def _download_desktop_installer(url: str, expected_digest: str, version: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f"multica-desktop-{version}-", suffix=".exe")
    os.close(fd)
    destination = Path(raw_path)
    request = urllib.request.Request(url, headers={"User-Agent": "multica-deployment-tool"})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
        actual = digest.hexdigest().lower()
        if expected_digest and actual != expected_digest:
            raise ConfigError(f"桌面端安装包 SHA-256 不匹配: {actual}")
    except (OSError, urllib.error.URLError) as exc:
        destination.unlink(missing_ok=True)
        raise ConfigError("下载 Multica 桌面端安装包失败") from exc
    return destination


def _desktop_health(profile: str, expected_server: str) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{DESKTOP_HEALTH_PORT}/health", timeout=5
        ) as response:
            health = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(health, dict):
        return None
    if (
        health.get("profile") != profile
        or str(health.get("server_url", "")).rstrip("/")
        != expected_server.rstrip("/")
    ):
        return None
    return health


def _start_desktop_daemon(
    cli_exe: Path, profile: str, expected_server: str
) -> dict[str, object]:
    if not cli_exe.is_file():
        raise ConfigError(f"找不到桌面端 CLI: {cli_exe}")
    subprocess.run(
        [str(cli_exe), "--profile", profile, "daemon", "stop"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [str(cli_exe), "--profile", profile, "daemon", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise ConfigError("启动桌面端 CLI daemon 失败") from exc
    for _ in range(15):
        time.sleep(1)
        health = _desktop_health(profile, expected_server)
        if health is not None:
            return health
    raise ConfigError("桌面端 daemon 未能在本地端点上就绪")


def resolve_desktop_version(args: argparse.Namespace) -> str:
    explicit = getattr(args, "desktop_version", "")
    if explicit:
        return explicit
    image_tag = getattr(args, "image_tag", "")
    return image_tag if re.fullmatch(r"v?\d+\.\d+\.\d+", image_tag or "") else "latest"


def sync_desktop(args: argparse.Namespace) -> None:
    """Install/update the Windows desktop and bind its CLI to the deployed service."""

    if not getattr(args, "desktop_sync", False):
        print("桌面端同步：已跳过（--no-desktop-sync）")
        return
    if os.name != "nt":
        print("桌面端同步：当前管理机不是 Windows，跳过")
        return
    profile = getattr(args, "desktop_profile", DEFAULTS["desktop_profile"])
    server_url = resolve_target_addresses(args).service_origin
    desktop_exe, cli_exe, _ = desktop_paths(profile)
    desired, download_url, digest = _desktop_release(resolve_desktop_version(args))
    current = _version_number(_desktop_version(desktop_exe))
    if current != desired:
        print(f"同步 Windows 桌面端：{current or '未安装'} -> v{desired}")
        installer = _download_desktop_installer(download_url, digest, desired)
        try:
            subprocess.run([str(installer), "/S"], check=True, timeout=300)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ConfigError("安装 Multica 桌面端失败") from exc
        finally:
            installer.unlink(missing_ok=True)
    else:
        print(f"Windows 桌面端已是 v{desired}，跳过下载安装")
    preserve_desktop_profile(server_url, profile)
    health = _desktop_health(profile, server_url) or _start_desktop_daemon(
        cli_exe, profile, server_url
    )
    print(f"桌面端同步完成：{health.get('cli_version', desired)} -> {health.get('server_url')}")


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
    if require_ip and not getattr(args, "nas_ip", "") and not (
        getattr(args, "browser_url", "") or getattr(args, "service_url", "")
    ):
        raise ConfigError("必须提供 --nas-ip，或提供 --browser-url/--service-url。")
    addresses = resolve_target_addresses(args)
    if require_ip and not addresses.bind_address:
        raise ConfigError("缺少目标绑定/服务地址。")
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
    device_client_id = getattr(
        args, "github_device_flow_client_id", DEFAULTS["github_device_flow_client_id"]
    ).strip()
    if device_client_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", device_client_id):
        raise ConfigError(
            "--github-device-flow-client-id 只能包含字母、数字、点、下划线或短横线。"
        )
    if getattr(args, "github_device_flow_enabled", False) and not device_client_id:
        raise ConfigError(
            "已启用 GitHub Device Flow，但缺少 --github-device-flow-client-id；"
            "没有 client ID 时请关闭该功能。"
        )
    public_url = getattr(args, "public_url", DEFAULTS["public_url"]).strip()
    if public_url:
        parsed = urllib.parse.urlparse(public_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError("--public-url 必须是完整的公网 HTTPS origin。")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ConfigError("--public-url 只能填写 origin，例如 https://multica.example.com。")
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


def verify_netbird_endpoint(args: argparse.Namespace) -> None:
    """Fail before changing Multica when the selected overlay endpoint is unavailable."""

    if not getattr(args, "netbird", False):
        return
    addresses = resolve_target_addresses(args)
    expected = f"NetBird IP: {addresses.bind_address}/"
    docker = privileged(args, q(docker_path(args)))
    command = textwrap.dedent(
        f"""
        netbird_status() {{
          if command -v netbird >/dev/null 2>&1; then
            netbird status
            return
          fi
          if {docker} inspect netbird >/dev/null 2>&1; then
            {docker} exec netbird netbird status
            return
          fi
          return 127
        }}
        status="$(netbird_status)" || {{
          echo 'NetBird 未运行：请先在 NAS 启动并加入 NetBird，再部署。' >&2
          exit 1
        }}
        printf '%s\\n' "$status" | grep -Fq 'Management: Connected' || {{
          echo 'NetBird 管理连接未建立。' >&2
          exit 1
        }}
        printf '%s\\n' "$status" | grep -Fq 'Signal: Connected' || {{
          echo 'NetBird 信令连接未建立。' >&2
          exit 1
        }}
        printf '%s\\n' "$status" | grep -Fq {q(expected)} || {{
          echo 'NAS 当前 NetBird IP 与 --nas-ip 不一致。' >&2
          exit 1
        }}
        """
    ).strip()
    remote(args, command)
    print(f"NetBird 已验证: {addresses.browser_origin}")


def open_service_url(args: argparse.Namespace, path: str, *, timeout: int = 5):
    """Open the selected service endpoint without leaking NetBird traffic to a proxy."""

    url = resolve_target_addresses(args).service_origin.rstrip("/") + path
    if getattr(args, "netbird", False):
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout)
    return urllib.request.urlopen(url, timeout=timeout)


def check_plane_url(args: argparse.Namespace, *, timeout: int = 5) -> str:
    """Report optional Plane reachability without treating it as required."""

    plane_url = resolve_target_addresses(args).plane_url
    if not plane_url:
        return "not-configured"
    try:
        with urllib.request.urlopen(plane_url, timeout=timeout) as response:
            return f"reachable ({response.status})"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"unreachable ({exc})"


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


def render_watchdog_config(args: argparse.Namespace) -> Path:
    """Render only non-secret watchdog connection settings."""

    addresses = resolve_target_addresses(args)
    template = (PACKAGE_ROOT / "ops" / "multica-watchdog.conf.template").read_text(
        encoding="utf-8"
    )
    replacements = {
        "__NAS_TARGET__": args.nas_target,
        "__NAS_IP__": addresses.bind_address,
        "__APP_PORT__": str(args.app_port),
        "__DOCKER_PATH__": docker_path(args),
    }
    for source, value in replacements.items():
        template = template.replace(source, value.replace("'", "'\\''"))
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".conf", delete=False
    )
    try:
        handle.write(template)
        handle.close()
        return Path(handle.name)
    except Exception:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise


def install_watchdog(args: argparse.Namespace) -> None:
    """Install and start a host-level watchdog on privileged NAS deployments."""

    if getattr(args, "no_sudo", False):
        print("跳过 NAS watchdog 安装：--no-sudo 部署需要由管理员自行接入开机任务。")
        return
    for name in WATCHDOG_FILES:
        copy_to_nas(args, PACKAGE_ROOT / name)
    rendered = render_watchdog_config(args)
    try:
        copy_to_nas(args, rendered, remote_name=".multica-watchdog.conf")
    finally:
        rendered.unlink(missing_ok=True)
    watchdog_script = q(args.nas_target + "/multica-watchdog.sh")
    init_script = q(args.nas_target + "/S99multica-watchdog.sh")
    config_file = q(args.nas_target + "/.multica-watchdog.conf")
    commands = [
        privileged(args, "install -d -m 0755 /usr/local/bin /usr/local/etc /usr/local/etc/rc.d"),
        privileged(
            args,
            f"install -m 0755 {watchdog_script} /usr/local/bin/multica-watchdog.sh",
        ),
        privileged(
            args,
            f"install -m 0755 {init_script} /usr/local/etc/rc.d/S99multica-watchdog.sh",
        ),
        privileged(
            args,
            f"install -m 0644 {config_file} /usr/local/etc/multica-watchdog.conf",
        ),
        privileged(args, "/usr/local/etc/rc.d/S99multica-watchdog.sh restart"),
    ]
    remote(
        args,
        " && ".join(commands),
    )


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
  backend_ref=$(sed -n 's/^MULTICA_BACKEND_REF=//p' "$env_file" | tail -n 1)
  web_ref=$(sed -n 's/^MULTICA_WEB_REF=//p' "$env_file" | tail -n 1)
  if [ -n "$tag" ]; then
    umask 077
    printf 'MULTICA_IMAGE_TAG=%s\nMULTICA_BACKEND_IMAGE=%s\nMULTICA_WEB_IMAGE=%s\nMULTICA_BACKEND_REF=%s\nMULTICA_WEB_REF=%s\n' \
      "$tag" "$backend" "$web" "$backend_ref" "$web_ref" > "$state"
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
    backend_ref = getattr(args, "backend_ref", "")
    web_ref = getattr(args, "web_ref", "")
    script = """
set -eu
state='__STATE__'
umask 077
printf 'MULTICA_IMAGE_TAG=%s\nMULTICA_BACKEND_IMAGE=%s\nMULTICA_WEB_IMAGE=%s\nMULTICA_BACKEND_REF=%s\nMULTICA_WEB_REF=%s\n' \
  '__TAG__' '__BACKEND__' '__WEB__' '__BACKEND_REF__' '__WEB_REF__' > "$state"
chmod 600 "$state"
""".strip()
    replacements = {
        "__STATE__": current,
        "__TAG__": args.image_tag,
        "__BACKEND__": backend,
        "__WEB__": web,
        "__BACKEND_REF__": backend_ref,
        "__WEB_REF__": web_ref,
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
        if key in {
            "MULTICA_IMAGE_TAG",
            "MULTICA_BACKEND_IMAGE",
            "MULTICA_WEB_IMAGE",
            "MULTICA_BACKEND_REF",
            "MULTICA_WEB_REF",
        }:
            state[key] = value
    if not state.get("MULTICA_IMAGE_TAG"):
        raise ConfigError("部署目标上没有可回滚的上一版本记录。先成功部署一次新版本后才能回滚。")
    return state


def check_package() -> None:
    for name in (*REQUIRED_FILES, "Caddyfile"):
        path = PACKAGE_ROOT / name
        if not path.is_file():
            raise ConfigError(f"部署包缺少文件: {path}")


def render_caddy(args: argparse.Namespace) -> Path:
    template = (PACKAGE_ROOT / "Caddyfile").read_text(encoding="utf-8")
    addresses = resolve_target_addresses(args)
    browser_host = urllib.parse.urlsplit(addresses.browser_origin).hostname
    if not browser_host:
        raise ConfigError("浏览器访问地址缺少主机名。")
    rendered = template
    for source, value in {
        "__BROWSER_HOST__": browser_host,
        "__APP_PORT__": str(args.app_port),
        "__BIND_ADDRESS__": addresses.bind_address,
    }.items():
        rendered = rendered.replace(source, value)
    if "__BROWSER_HOST__" in rendered or "__BIND_ADDRESS__" in rendered:
        raise ConfigError("Caddyfile 缺少可渲染的地址占位符。")
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".Caddyfile", prefix="multica-", delete=False
    )
    try:
        handle.write(rendered)
    finally:
        handle.close()
    return Path(handle.name)


def initialize_remote_env(args: argparse.Namespace) -> None:
    addresses = resolve_target_addresses(args)
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
if ! grep -q '^GITHUB_TOKEN_ENCRYPTION_KEY=.' "$env_file"; then
  umask 077
  printf 'GITHUB_TOKEN_ENCRYPTION_KEY=%s\\n' "$(openssl rand -hex 32)" >> "$env_file"
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
upsert MULTICA_BACKEND_REF '__BACKEND_REF__'
upsert MULTICA_WEB_REF '__WEB_REF__'
upsert BACKEND_PORT '__BACKEND_PORT__'
upsert FRONTEND_PORT '__FRONTEND_PORT__'
upsert FRONTEND_ORIGIN __BROWSER_ORIGIN__
upsert CORS_ALLOWED_ORIGINS __BROWSER_ORIGIN__
upsert MULTICA_APP_URL __BROWSER_ORIGIN__
upsert MULTICA_SERVICE_URL __SERVICE_ORIGIN__
upsert GITHUB_DEVICE_FLOW_ENABLED __GITHUB_DEVICE_FLOW_ENABLED__
upsert GITHUB_APP_CLIENT_ID __GITHUB_DEVICE_FLOW_CLIENT_ID__
current_gitea_redirect="$(sed -n 's/^GITEA_REDIRECT_URI=//p' "$env_file" | head -n 1)"
if [ -n __OAUTH_ORIGIN_EXPLICIT__ ]; then
  upsert GITEA_REDIRECT_URI __OAUTH_CALLBACK__
elif [ -z "$current_gitea_redirect" ] || printf '%s\n' "$current_gitea_redirect" | grep -Eq '^http://NAS_IP:[0-9]+/auth/callback$' || [ "$current_gitea_redirect" = 'http://localhost:3000/auth/callback' ]; then
  upsert GITEA_REDIRECT_URI __OAUTH_CALLBACK__
fi
if [ -n __PUBLIC_URL_VALUE__ ]; then
  upsert MULTICA_PUBLIC_URL __PUBLIC_URL_VALUE__
fi
if [ -n __PLANE_URL__ ]; then
  upsert MULTICA_PLANE_URL __PLANE_URL__
fi
chmod 600 "$env_file"
chmod 640 "$target/docker-compose.selfhost.yml" "$target/docker-compose.nas.yml" "$target/Caddyfile" "$target/.env.template"
__CHOWN__
""".strip()
    replacements = {
        "__TARGET__": args.nas_target,
        "__IMAGE_TAG__": args.image_tag,
        "__BACKEND_IMAGE__": getattr(args, "backend_image", DEFAULTS["backend_image"]),
        "__WEB_IMAGE__": getattr(args, "web_image", DEFAULTS["web_image"]),
        "__BACKEND_REF__": getattr(args, "backend_ref", ""),
        "__WEB_REF__": getattr(args, "web_ref", ""),
        "__BACKEND_PORT__": str(args.backend_port),
        "__FRONTEND_PORT__": str(args.frontend_port),
        "__BROWSER_ORIGIN__": q(addresses.browser_origin),
        "__SERVICE_ORIGIN__": q(addresses.service_origin),
        "__GITHUB_DEVICE_FLOW_ENABLED__": q(
            "true"
            if getattr(args, "github_device_flow_enabled", DEFAULTS["github_device_flow_enabled"])
            else "false"
        ),
        "__GITHUB_DEVICE_FLOW_CLIENT_ID__": q(
            getattr(
                args,
                "github_device_flow_client_id",
                DEFAULTS["github_device_flow_client_id"],
            ).strip()
        ),
        "__OAUTH_CALLBACK__": q(addresses.oauth_callback_url),
        "__OAUTH_ORIGIN_EXPLICIT__": q(getattr(args, "oauth_origin", "").strip()),
        "__APP_PORT__": str(args.app_port),
        "__PUBLIC_URL_VALUE__": q(getattr(args, "public_url", "").strip()),
        "__PLANE_URL__": q(addresses.plane_url),
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
    addresses = resolve_target_addresses(args)
    script = """
set -eu
target='__TARGET__'
env_file="$target/.env"
test -f "$env_file"
if ! grep -q '^GITHUB_TOKEN_ENCRYPTION_KEY=.' "$env_file"; then
  umask 077
  printf 'GITHUB_TOKEN_ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" >> "$env_file"
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
  # Docker Compose supports a single-quoted multi-line env value. Keeping the
  # PEM quoted is required; a plain multi-line KEY=value entry is invalid.
  printf "GITHUB_APP_PRIVATE_KEY='" >> "$tmp_file"
  tr -d '\r' < "$key_file" >> "$tmp_file"
  printf "'\\n" >> "$tmp_file"
  mv "$tmp_file" "$env_file"
fi
chmod 600 "$env_file"
__CHOWN__
cd "$target"
__COMPOSE__ --env-file .env -f docker-compose.selfhost.yml -f docker-compose.nas.yml up -d --force-recreate backend
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 5 __SERVICE_ORIGIN__/readyz; then exit 0; fi
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
    script = script.replace("__SERVICE_ORIGIN__", q(addresses.service_origin))
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

    verify_netbird_endpoint(args)
    issuer = prompt_required("Gitea 地址（例如 http://gitea.internal:3000）").rstrip("/")
    parsed = urllib.parse.urlparse(issuer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ConfigError("Gitea 地址必须是没有查询参数或片段的 http(s) URL。")
    client_id = prompt_required("Gitea OAuth Client ID")
    client_secret = getpass.getpass("Gitea OAuth Client Secret（不会显示，也不会写入命令行）: ")
    if not client_secret:
        raise ConfigError("Gitea OAuth Client Secret 不能为空。")
    default_redirect = resolve_target_addresses(args).oauth_callback_url
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


def configure_github_device_flow(args: argparse.Namespace) -> None:
    """Configure the public GitHub OAuth client metadata used by Device Flow."""

    enabled = (
        console_choice(
            prompt_default("启用 GitHub Device Flow（y/N；LAN/NetBird 不需要公网 webhook）", "n"),
            "yn",
        )
        == "y"
    )
    current_client_id = getattr(
        args,
        "github_device_flow_client_id",
        DEFAULTS["github_device_flow_client_id"],
    ).strip()
    client_id = (
        prompt_required(
            "GitHub OAuth App Device Flow Client ID（非敏感，不是 secret）",
            current_client_id,
        )
        if enabled
        else current_client_id
    )
    if client_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", client_id):
        raise ConfigError("GitHub Device Flow Client ID 含有不安全字符。")
    if enabled and not client_id:
        raise ConfigError("启用 GitHub Device Flow 时必须提供 Client ID。")
    args.github_device_flow_enabled = enabled
    args.github_device_flow_client_id = client_id
    if not enabled and not client_id:
        # A normal first-run install already rendered the disabled defaults;
        # do not restart backend just because the optional feature was skipped.
        save_config(args)
        print("GitHub Device Flow 已关闭；GitHub App/webhook 配置保持独立且可选。")
        return
    update_backend_env(
        args,
        {
            "GITHUB_DEVICE_FLOW_ENABLED": "true" if enabled else "false",
            "GITHUB_APP_CLIENT_ID": client_id,
        },
    )
    save_config(args)
    if enabled:
        print("GitHub Device Flow 已启用；Client ID 已写入 backend 环境（不会回显）。")
        print("桌面端可使用 GitHub Device Flow 授权；LAN/NetBird 部署不需要公网 webhook。")
    else:
        print("GitHub Device Flow 已关闭；GitHub App/webhook 配置保持独立且可选。")


def github_device_flow_state(args: argparse.Namespace) -> str:
    """Return a non-secret local summary suitable for wizard/status output."""

    enabled = bool(
        getattr(args, "github_device_flow_enabled", DEFAULTS["github_device_flow_enabled"])
    )
    client_id = getattr(
        args,
        "github_device_flow_client_id",
        DEFAULTS["github_device_flow_client_id"],
    ).strip()
    if enabled and client_id:
        return "enabled (client ID configured)"
    if enabled:
        return "enabled but client ID missing"
    if client_id:
        return "disabled (client ID saved)"
    return "disabled"


def configure_github(args: argparse.Namespace) -> None:
    check_binary("ssh")
    check_binary("scp")
    check_package()
    # A config-only run must also upgrade the backend Compose contract. Older
    # NAS deployments do not pass App ID/private key into the container yet.
    copy_to_nas(args, PACKAGE_ROOT / "docker-compose.selfhost.yml")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.slug):
        raise ConfigError("--slug 应是 GitHub App URL 最后的 slug，例如 multica-acme。")
    webhook_secret = getpass.getpass("GitHub Webhook secret（不会显示，也不会写入命令行）: ")
    if not webhook_secret:
        raise ConfigError("Webhook secret 不能为空。")
    values = {
        "GITHUB_APP_SLUG": args.slug,
        "GITHUB_WEBHOOK_SECRET": webhook_secret,
    }
    public_url = getattr(args, "public_url", "").strip().rstrip("/")
    if public_url:
        values["MULTICA_PUBLIC_URL"] = public_url
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
        if "'" in key_text:
            raise ConfigError("私钥文件包含不支持的单引号字符，请重新下载 GitHub PEM 私钥。")
        temporary_remote_name = f".multica-github-key-{uuid.uuid4().hex}.tmp"
        copy_to_nas(args, key_path, remote_name=temporary_remote_name)
        remote_key = f"{args.nas_target}/{temporary_remote_name}"
    try:
        update_backend_env(args, values, private_key_remote=remote_key)
    finally:
        if temporary_remote_name:
            remote(args, f"rm -f {q(args.nas_target + '/' + temporary_remote_name)}")
    print("GitHub App 配置已写入 NAS，并已重建 backend。")
    if public_url:
        print(f"Setup URL: {public_url}/api/github/setup")
        print(f"Webhook URL: {public_url}/api/webhooks/github")
    else:
        print("未设置 --public-url；GitHub 必须能从公网 HTTPS 访问 /api/github/setup 和 /api/webhooks/github。")
    print("下一步：在 Settings → GitHub 点击 Connect GitHub。")


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
    app_port_text = prompt_default("Multica 浏览器入口端口", str(getattr(args, "app_port", DEFAULTS["app_port"])))
    try:
        args.app_port = int(app_port_text)
    except ValueError as exc:
        raise ConfigError("Multica 入口端口必须是数字。") from exc
    args.nas_ip = prompt_required(
        "目标绑定/服务间地址（LAN、NetBird IP 或主机名）",
        args.nas_ip or host_ip,
    )
    args.browser_url = prompt_default(
        "浏览器访问 origin（可填 NetBird/LAN/域名）",
        getattr(args, "browser_url", "") or _origin_for_host(args.nas_ip, args.app_port),
    )
    args.service_url = prompt_default(
        "服务间访问 origin（部署目标自检使用）",
        getattr(args, "service_url", "") or _origin_for_host(args.nas_ip, args.app_port),
    )
    args.oauth_origin = prompt_default(
        "OAuth 回调 origin（必须能被浏览器访问）",
        getattr(args, "oauth_origin", "") or args.browser_url,
    )
    args.plane_url = prompt_default(
        "可选 Plane origin（直接回车表示不配置）",
        getattr(args, "plane_url", ""),
    )
    detected_docker = detected.get("synology_docker") if is_synology else detected.get("docker")
    target_default = args.nas_target if args.nas_target != DEFAULTS["nas_target"] else target_default
    docker_default = args.docker_path if args.docker_path != DOCKER_PATH else (detected_docker or docker_default)
    owner_default = detected.get("user") or owner_default
    group_default = detected.get("group") or group_default
    advanced = prompt_default("需要修改目录/Docker/用户组等高级参数？（y/N）", "n").lower() == "y"
    args.nas_target = prompt_default("部署目标目录", target_default) if advanced else target_default
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

    if (
        not args.nas_host
        or not args.nas_ip
        or not getattr(args, "browser_url", "")
        or not getattr(args, "app_port", 0)
    ):
        configure_connection(args)
    else:
        addresses = resolve_target_addresses(args)
        print(f"已读取配置: {args.nas_host} → 浏览器 {addresses.browser_origin}，服务间 {addresses.service_origin}")
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
    try:
        configure_github_device_flow(args)
    except (ConfigError, RuntimeError) as exc:
        print(f"服务已部署，但 GitHub Device Flow 未配置：{exc}", file=sys.stderr)
        print("这是可选能力；LAN/NetBird 部署不受影响，可稍后在 wizard 中配置。", file=sys.stderr)
    addresses = resolve_target_addresses(args)
    print("\n下一步：")
    print(f"1. 浏览器打开 {addresses.browser_origin} 完成注册/登录。")
    print(f"2. Linux agent 运行: bash client-bootstrap.sh --server-url {addresses.browser_origin} --device-name <设备名>")
    print(f"3. Windows agent 运行: .\\compat\\windows\\client-bootstrap.ps1 -ServerUrl {addresses.browser_origin} -DeviceName <设备名>")


def wizard(args: argparse.Namespace) -> None:
    """Interactive operator menu; keeps routine work out of PowerShell syntax."""

    check_package()
    config_path = apply_saved_config(args)
    display_ip = args.nas_ip or "<未设置>"
    print(f"\n{PRODUCT_NAME}")
    print("用于在 NAS/自有 Linux 服务器上部署和调整 Multica，并配置登录与可选 Plane 任务控制面。")
    print(f"目标: {args.nas_host or '<未设置>'}  地址: {display_ip}:{getattr(args, 'app_port', DEFAULTS['app_port'])}")
    print(f"本地配置: {config_path}")
    print(f"GitHub Device Flow: {github_device_flow_state(args)}")
    print("GitHub App/webhook: advanced optional; public HTTPS URL is not required for LAN/NetBird deployment")
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
            "g) 配置 GitHub Device Flow（仅 client ID 和开关）\n"
            "p) 查看地址、OAuth 回调和 Plane 配置\n"
            "d) 运行部署环境诊断\n"
            "r) 回滚上一版本\n"
            "0) 退出"
        )
        choice = input("选择 [0]: ").strip() or "0"
        # PowerShell pipelines can prepend a console-codepage marker to stdin;
        # keep the menu usable even when someone feeds a choice from a script.
        if choice not in {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "d", "r", "p", "g"}:
            choice = next((char for char in choice if char in "0123456789drpg"), choice)
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
            elif choice == "g":
                validate_config(args)
                configure_github_device_flow(args)
            elif choice == "4":
                slug = input("GitHub App slug（URL 最后一段）: ").strip()
                app_id = input("GitHub App 数字 ID（可留空）: ").strip() or None
                key_file = input("PEM 私钥文件路径（可留空）: ").strip() or None
                public_url = prompt_default(
                    "GitHub 对外 HTTPS 地址（没有公网入口可留空）",
                    getattr(args, "public_url", ""),
                ).strip()
                github_args = argparse.Namespace(
                    **vars(args),
                    slug=slug,
                    app_id=app_id,
                    private_key_file=key_file,
                    public_url=public_url,
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
                addresses = resolve_target_addresses(args)
                print(f"连接参数已更新: {args.nas_host} → 浏览器 {addresses.browser_origin}，服务间 {addresses.service_origin}")
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
            elif choice == "p":
                validate_config(args)
                addresses = resolve_target_addresses(args)
                print(f"浏览器入口: {addresses.browser_origin}")
                print(f"服务间入口: {addresses.service_origin}")
                print(f"OAuth 回调: {addresses.oauth_callback_url}")
                print(f"Plane: {addresses.plane_url or '未配置'}")
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
    print(f"构建位置: 本机 Docker（NAS 只接收镜像，不从源码编译）")
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
    if getattr(args, "hot_update", False):
        print("本地镜像已加载到 NAS，开始快速更新 backend/frontend（数据库和 Caddy 不动）...")
    else:
        print("本地镜像已加载到 NAS，开始更新 Multica...")
    deploy(deploy_args)


def node_ssh_base(args: argparse.Namespace) -> list[str]:
    """Build the independent node SSH command; AGX never runs in Multica."""

    host = getattr(args, "node_host", "")
    if not host:
        raise ConfigError("fleet apply 需要 --node-host 指定 AGX 节点。")
    command = ["ssh"]
    port = getattr(args, "node_ssh_port", 0)
    if port:
        command += ["-p", str(port)]
    return command + [host]


def node_remote(args: argparse.Namespace, command: str) -> None:
    run(node_ssh_base(args) + [command], label="AGX 节点命令")


def node_remote_capture(args: argparse.Namespace, command: str) -> str:
    try:
        result = subprocess.run(
            node_ssh_base(args) + [command],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"AGX 节点命令失败（退出码 {exc.returncode}）。") from exc
    return result.stdout


def node_remote_stdin(
    args: argparse.Namespace, script: str, command: list[str], *, label: str
) -> None:
    try:
        subprocess.run(
            node_ssh_base(args) + command,
            input=script.encode("utf-8"),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{label} 失败（退出码 {exc.returncode}）。") from exc


def node_remote_stdin_capture(
    args: argparse.Namespace, script: str, command: list[str], *, label: str
) -> str:
    """Run a checked-in node script and return stdout for structured parsing."""

    try:
        result = subprocess.run(
            node_ssh_base(args) + command,
            input=script.encode("utf-8"),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{label} 失败（退出码 {exc.returncode}）。") from exc
    return result.stdout.decode("utf-8", errors="replace")


def _fleet_image_tag(identity: str) -> str:
    return "fleet-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def _validate_fleet_agx_init_args(args: argparse.Namespace) -> None:
    """Reject an incomplete AGX init command before any remote mutation."""

    owner = str(getattr(args, "agx_github_owner", "") or "").strip()
    provider = str(getattr(args, "agx_provider", "") or "").strip()
    if not owner or not provider:
        raise ConfigError(
            "fleet apply 必须同时提供 --agx-github-owner 和 --agx-provider；"
            "AGX 初始化参数不能在远端突变后才补齐。"
        )
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", owner):
        raise ConfigError("--agx-github-owner 含有不安全字符。")
    if provider not in {"codex", "claude", "both"}:
        raise ConfigError("--agx-provider 只能是 codex、claude 或 both。")


def _source_revisions_match(expected: str, actual: str) -> bool:
    """Compare full and abbreviated Git object IDs consistently."""

    expected = expected.strip().lower()
    actual = actual.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", actual):
        return False
    return expected == actual or expected.startswith(actual) or actual.startswith(expected)


def apply_fleet_multica(contract: dict[str, object], args: argparse.Namespace) -> dict[str, str]:
    """Use the existing NAS Compose or source-build operation for Multica."""

    # This guard must precede the first Multica/NAS mutation as well as the
    # AGX phase guard.  Direct adapter use must not bypass the fleet CLI seam.
    _validate_fleet_agx_init_args(args)
    multica = contract["multica"]
    if "source_revision" in multica:
        identity = str(multica["source_revision"])
    else:
        backend_image = str(multica["backend_image"])
        web_image = str(multica["web_image"])
        identity = f"backend={backend_image}\nweb={web_image}"
    args.image_tag = _fleet_image_tag(identity)
    args.backend_ref = ""
    args.web_ref = ""
    if "source_revision" not in multica:
        # v1 carries one immutable digest per Compose service.  Never copy a
        # backend reference into the web slot (or vice versa).
        args.backend_image = ""
        args.web_image = ""
        args.backend_ref = backend_image
        args.web_ref = web_image
        args.no_pull = False
        deploy(args)
        return {
            "operation": "deploy",
            "backend_image": backend_image,
            "web_image": web_image,
        }

    source_dir = getattr(args, "source_dir", "")
    if not source_dir:
        raise ConfigError("source_revision 合同需要 fleet apply --source-dir 指向 Multica checkout。")
    checkout = Path(source_dir).expanduser().resolve()
    expected = str(contract["multica"]["source_revision"]).lower()
    try:
        actual = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip().lower()
    except FileNotFoundError as exc:
        raise ConfigError("检查 Multica 源码 revision 需要 git。") from exc
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"无法读取 Multica 源码 revision: {checkout}") from exc
    try:
        dirty = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ConfigError(f"无法检查 Multica 源码工作区: {checkout}") from exc
    if dirty:
        raise ConfigError("Multica 源码 checkout 有未提交变更；固定 source_revision 不能构建 dirty tree。")
    if not _source_revisions_match(expected, actual):
        raise ConfigError(
            f"Multica 源码 revision 不匹配：合同要求 {expected}，当前是 {actual}。"
        )
    args.no_pull = True
    build_source(args)
    return {"operation": "build", "source_revision": expected}


def apply_fleet_agx(contract: dict[str, object], node: dict[str, object], args: argparse.Namespace) -> dict[str, str]:
    """Invoke the AGX public CLI on the selected node only."""

    _validate_fleet_agx_init_args(args)
    agx_bin = getattr(args, "agx_bin", "agx")
    expected_version = str(contract["agx"]["version"]).lstrip("v")
    version_output = node_remote_capture(args, f"{q(agx_bin)} version")
    version_tokens = version_output.strip().split()
    actual_version = version_tokens[-1].lstrip("v") if version_tokens else ""
    if actual_version != expected_version:
        raise ConfigError(
            f"AGX CLI 版本不匹配：合同要求 {contract['agx']['version']}，节点报告 {actual_version or 'unknown'}。"
        )
    command = [agx_bin, "apply", "--root", str(contract["agx"]["installation_root"])]
    bundle = getattr(args, "agx_bundle", "")
    if bundle:
        command += ["--bundle", bundle]
    node_remote(args, " ".join(q(value) for value in command))

    owner = str(args.agx_github_owner)
    provider = str(args.agx_provider)
    init = [
        agx_bin,
        "init",
        "--root",
        str(contract["agx"]["installation_root"]),
        "--github-owner",
        owner,
        "--provider",
        provider,
        "--apply",
        "--output",
        "json",
    ]
    node_remote(args, " ".join(q(value) for value in init))
    return {"operation": "apply", "node": str(node["name"]), "bundle": "agx-owned"}


def _client_bootstrap_command(
    contract: dict[str, object], node: dict[str, object], args: argparse.Namespace, *, verify_only: bool
) -> list[str]:
    multica = contract["multica"]
    device = getattr(args, "device_name", "") or str(node["name"])
    runtime = getattr(args, "runtime_name", "") or str(node["name"])
    command = [
        "bash",
        "-s",
        "--",
        "--server-url",
        str(multica["server_url"]),
        "--profile",
        str(multica["profile"]),
        "--workspace-id",
        str(multica["workspace_id"]),
        "--device-name",
        device,
        "--runtime-name",
        runtime,
    ]
    if verify_only:
        command += ["--skip-install", "--verify-only"]
    return command


def _json_between_markers(output: str, begin: str, end: str) -> object:
    """Decode one machine-readable section from the bootstrap script."""

    lines = output.splitlines()
    try:
        start = lines.index(begin) + 1
        finish = lines.index(end, start)
    except ValueError as exc:
        raise RuntimeError(f"Multica client verification omitted {begin}.") from exc
    body = "\n".join(lines[start:finish]).strip()
    if not body:
        raise RuntimeError(f"Multica client verification returned no data for {begin}.")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Multica client verification returned invalid JSON for {begin}.") from exc


def _live_multica_evidence(contract: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    """Read Multica health and official CLI state without using private APIs."""

    health_status = 0
    readiness_status = 0
    try:
        with open_service_url_direct(args, "/health") as response:
            health_status = response.status
        with open_service_url_direct(args, "/readyz") as response:
            readiness_status = response.status
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Multica health/readiness probe failed: {exc}") from exc

    script = (PACKAGE_ROOT / "client-bootstrap.sh").read_text(encoding="utf-8")
    output = node_remote_stdin_capture(
        args,
        script,
        _client_bootstrap_command(contract, {"name": args.node_host}, args, verify_only=True)
        + ["--output-json"],
        label="Multica live verification",
    )
    auth = _json_between_markers(output, "MULTICA_VERIFY_AUTH_BEGIN", "MULTICA_VERIFY_AUTH_END")
    workspace = _json_between_markers(
        output, "MULTICA_VERIFY_WORKSPACE_BEGIN", "MULTICA_VERIFY_WORKSPACE_END"
    )
    runtime = _json_between_markers(output, "MULTICA_VERIFY_RUNTIME_BEGIN", "MULTICA_VERIFY_RUNTIME_END")
    if not all(isinstance(value, dict) for value in (auth, workspace, runtime)):
        raise RuntimeError("Multica live verification sections must be JSON objects.")
    workspace_id = workspace.get("id") or workspace.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise RuntimeError("Multica workspace readback has no id.")
    runtime_status = str(runtime.get("status") or runtime.get("state") or "").lower()
    return {
        "health": {"healthy": health_status == 200, "status": str(health_status)},
        "readiness": {"ready": readiness_status == 200, "status": str(readiness_status)},
        "auth": auth,
        "workspace": {"available": True, "workspace_id": workspace_id.strip()},
        "runtime": {
            "online": runtime_status == "running",
            "status": runtime_status or "unknown",
            "runtime_id": runtime.get("runtime_id") or runtime.get("daemon_id") or "daemon",
        },
    }


def _live_agx_evidence(contract: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    """Read AGX's public version/status commands; never infer task success."""

    agx_bin = getattr(args, "agx_bin", "agx")
    version_output = node_remote_capture(args, f"{q(agx_bin)} version")
    actual_version = _validate_agx_version(version_output, contract)
    status_command = " ".join(
        [q(agx_bin), "status", "--root", q(str(contract["agx"]["installation_root"])), "--output", "json"]
    )
    status_output = node_remote_capture(args, status_command)
    try:
        status_payload = json.loads(status_output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AGX status did not return JSON.") from exc
    status = _validate_agx_reconcile_status(status_payload, contract, {"name": args.node_host})
    phase_ready = status["phase"] == "configured"
    initialization_ready = status["initialization"] in _AGX_INITIALIZATION_STATUSES
    # AGX's current public status schema has no node identity or task receipt.
    # Leave that evidence absent so FleetVerifier reports the real contract gap.
    return {
        "installation": {
            "installed": phase_ready,
            "installation_id": status["installation_id"],
            "version": actual_version,
        },
        "version": {"status": "ok", "version": actual_version},
        "bundle": {
            "installed": phase_ready,
            "bundle_id": status["bundle_id"],
            "version": actual_version,
        },
        "node": {"registered": False, "status": "identity-not-exposed"},
        "lifecycle": {"ready": phase_ready and initialization_ready, "status": status["initialization"]},
    }


def _live_task_runner(_context: dict[str, object]) -> dict[str, object]:
    """Fail closed until AGX publishes the task connector contract."""

    raise RuntimeError(
        "AGX currently exposes no public Multica task connector; "
        "a live disposable-task verification cannot be run"
    )


def _assert_live_origin_matches_contract(contract: dict[str, object], args: argparse.Namespace) -> None:
    """Prevent health checks and CLI verification from targeting different origins."""

    contract_origin = _origin(str(contract["multica"]["server_url"]), "合同中的 multica.server_url")
    supplied_origin = resolve_target_addresses(args).service_origin
    if supplied_origin != contract_origin:
        raise ConfigError(
            "--service-url/--nas-ip 与合同中的 multica.server_url 不一致："
            f"{supplied_origin} != {contract_origin}。"
        )


def open_service_url_direct(args: argparse.Namespace, path: str, *, timeout: int = 5):
    """Open a live check without inheriting HTTP(S)_PROXY for LAN/NAS traffic."""

    url = resolve_target_addresses(args).service_origin.rstrip("/") + path
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url, timeout=timeout)


def apply_fleet_connector(contract: dict[str, object], node: dict[str, object], args: argparse.Namespace) -> dict[str, str]:
    """Run the checked-in official Multica CLI bootstrap on the node."""

    if str(node["platform"]) != "linux":
        raise ConfigError("fleet apply 的节点连接器当前只支持 Linux；Windows 使用 client-bootstrap.ps1。")
    script = (PACKAGE_ROOT / "client-bootstrap.sh").read_text(encoding="utf-8")
    node_remote_stdin(
        args,
        script,
        _client_bootstrap_command(contract, node, args, verify_only=False),
        label="配置 Multica CLI/daemon",
    )
    return {"operation": "official-cli-bootstrap", "node": str(node["name"])}


def apply_fleet_preflight(contract: dict[str, object], node: dict[str, object], args: argparse.Namespace) -> dict[str, str]:
    """Read back auth, workspace, and daemon evidence through the official CLI."""

    if str(node["platform"]) != "linux":
        raise ConfigError("fleet apply 的 connector preflight 当前只支持 Linux。")
    script = (PACKAGE_ROOT / "client-bootstrap.sh").read_text(encoding="utf-8")
    node_remote_stdin(
        args,
        script,
        _client_bootstrap_command(contract, node, args, verify_only=True),
        label="Multica CLI/daemon preflight",
    )
    return {"operation": "official-cli-preflight", "node": str(node["name"])}


_AGX_INITIALIZATION_STATUSES = {
    "initialized",
    "ready",
    "configured",
    "completed",
    "success",
    "ok",
    "active",
}


def _validate_agx_version(version_output: object, contract: dict[str, object]) -> str:
    """Validate version independently because status has no version field."""

    if not isinstance(version_output, str):
        raise RuntimeError("AGX version returned no text evidence")
    tokens = version_output.strip().split()
    actual_version = tokens[-1].lstrip("v").lower() if tokens else ""
    agx = contract.get("agx")
    expected_version = (
        str(agx.get("version", "")).strip().lstrip("v").lower()
        if isinstance(agx, dict)
        else ""
    )
    if not actual_version or actual_version != expected_version:
        raise RuntimeError("AGX CLI version does not match the contract")
    return actual_version


def _validate_agx_reconcile_status(
    payload: object, contract: dict[str, object], node: dict[str, object]
) -> dict[str, object]:
    """Validate only fields emitted by ``agx status --output json``."""

    del contract, node
    if not isinstance(payload, dict):
        raise RuntimeError("AGX status JSON must be an object")
    if payload.get("phase") != "configured":
        raise RuntimeError("AGX status phase is not configured")

    identifiers: dict[str, str] = {}
    for field in ("installation_id", "bundle_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"AGX status has no {field}")
        identifiers[field] = value.strip()

    for field in ("missing", "modified"):
        value = payload.get(field)
        if not isinstance(value, list) or value:
            raise RuntimeError(f"AGX status {field} is not empty")

    initialization = payload.get("initialization")
    if not isinstance(initialization, dict):
        raise RuntimeError("AGX status has no initialization evidence")
    init_status = initialization.get("status")
    if (
        not isinstance(init_status, str)
        or init_status.strip().lower() not in _AGX_INITIALIZATION_STATUSES
    ):
        raise RuntimeError("AGX initialization is not initialized")
    problems = initialization.get("problems", [])
    if not isinstance(problems, list) or problems:
        raise RuntimeError("AGX initialization has problems")

    return {
        "phase": "configured",
        **identifiers,
        "initialization": init_status.strip().lower(),
    }


def reconcile_fleet_apply(
    contract: dict[str, object], node: dict[str, object], args: argparse.Namespace
) -> dict[str, str]:
    """Reconcile completed local state using read-only AGX and CLI checks."""

    agx_bin = getattr(args, "agx_bin", "agx")
    status_command = " ".join(
        [
            q(agx_bin),
            "status",
            "--root",
            q(str(contract["agx"]["installation_root"])),
            "--output",
            "json",
        ]
    )
    version_output = node_remote_capture(args, f"{q(agx_bin)} version")
    actual_version = _validate_agx_version(version_output, contract)
    status_output = node_remote_capture(args, status_command)
    if not isinstance(status_output, str) or not status_output.strip():
        raise RuntimeError("AGX status returned no structured readiness evidence")
    try:
        status_payload = json.loads(status_output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("AGX status did not return JSON") from exc
    status = _validate_agx_reconcile_status(status_payload, contract, node)
    cli_result = apply_fleet_preflight(contract, node, args)
    return {
        "ready": True,
        "agx": "status",
        "agx_status": status["phase"],
        "agx_version": actual_version,
        "multica": cli_result["operation"],
    }


def make_fleet_apply_adapters(args: argparse.Namespace):
    from fleet_apply import FleetApplyAdapters

    return FleetApplyAdapters(
        multica=lambda contract: apply_fleet_multica(contract, args),
        agx=lambda contract, node: apply_fleet_agx(contract, node, args),
        connector=lambda contract, node: apply_fleet_connector(contract, node, args),
        preflight=lambda contract, node: apply_fleet_preflight(contract, node, args),
        reconcile=lambda contract, node: reconcile_fleet_apply(contract, node, args),
    )


def run_fleet_apply(
    args: argparse.Namespace,
    *,
    adapters=None,
    which=shutil.which,
) -> int:
    from fleet_apply import (
        FleetApplyError,
        apply_contract,
        default_state_path,
        error_result,
        render,
    )
    from fleet_plan import FleetPlanError, build_plan, load_contract

    try:
        contract = load_contract(args.contract)
        _validate_fleet_agx_init_args(args)
        from urllib.parse import urlsplit

        endpoint = urlsplit(contract["multica"]["server_url"])
        if not args.nas_ip:
            args.nas_ip = endpoint.hostname or ""
        elif args.nas_ip != endpoint.hostname:
            raise ConfigError("--nas-ip 必须与合同中的 multica.server_url 主机一致。")
        if endpoint.port:
            args.app_port = endpoint.port
        validate_config(args)
        build_plan(contract, which=which)
        if adapters is None:
            adapters = make_fleet_apply_adapters(args)
        state_file = args.state_file or default_state_path(contract)
        result = apply_contract(
            contract,
            adapters,
            contract_path=args.contract,
            state_path=state_file,
            retry_command=(
                f'python multica_deploy.py fleet apply --contract "{args.contract}" '
                f'--state-file "{state_file}" --resume'
            ),
        )
        print(render(result, args.output_format), end="")
        return 0 if result["status"] == "configured" else 1
    except (ConfigError, FleetPlanError, FleetApplyError, RuntimeError, OSError) as exc:
        result = error_result(exc)
        print(render(result, args.output_format), end="")
        return 2


def run_fleet_verify(args: argparse.Namespace) -> int:
    """Run live preflight or validate previously captured structured evidence."""

    from fleet_plan import FleetPlanError, load_contract
    from fleet_verify import FleetVerifier, render

    try:
        contract = load_contract(args.contract)
        if args.live:
            if not args.node_host:
                raise ConfigError("--live 需要 --node-host。")
            if not args.nas_host:
                raise ConfigError("--live 需要 --nas-host（用于读取 NAS 服务）。")
            if not args.nas_ip and not args.service_url:
                raise ConfigError("--live 需要 --nas-ip 或 --service-url。")
            _assert_live_origin_matches_contract(contract, args)
            verifier = FleetVerifier(
                multica_reader=lambda _context: _live_multica_evidence(contract, args),
                agx_reader=lambda _context: _live_agx_evidence(contract, args),
                task_runner=_live_task_runner,
            )
        else:
            if args.evidence_file is None:
                raise ConfigError("离线校验需要 --evidence-file；现场校验请使用 --live。")
            payload = json.loads(args.evidence_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ConfigError("--evidence-file 必须是 JSON object。")
            required = {"multica", "agx", "task"}
            missing = sorted(required - payload.keys())
            if missing:
                raise ConfigError("--evidence-file 缺少: " + ", ".join(missing))
            verifier = FleetVerifier(
                multica_reader=lambda _context: payload["multica"],
                agx_reader=lambda _context: payload["agx"],
                task_runner=lambda _context: payload["task"],
            )
        result = verifier.verify(contract)
        print(render(result), end="")
        return 0 if result.get("status") == "verified" else 2
    except (ConfigError, FleetPlanError, OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False) + "\n", end="")
        return 2


def run_fleet_multi(args: argparse.Namespace) -> int:
    """Build a read-only multi-node plan; execution remains AGX-owned."""

    from fleet_multi import FleetMultiError, build_multi_plan, redact

    try:
        payload = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ConfigError("--config 必须是 JSON object。")
        result = build_multi_plan(payload)
        print(json.dumps(redact(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n", end="")
        return 0
    except (ConfigError, FleetMultiError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False) + "\n", end="")
        return 2


def doctor(args: argparse.Namespace) -> None:
    """Run read-only local and NAS checks before a deploy or upgrade."""

    for binary in ("ssh", "scp"):
        check_binary(binary)
    check_package()
    auto_detect_remote_docker(args)
    verify_netbird_endpoint(args)
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
            (
                "if test -f "
                + q(args.nas_target + "/.env")
                + "; then "
                + "device_enabled=$(sed -n 's/^GITHUB_DEVICE_FLOW_ENABLED=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "device_client_id=$(sed -n 's/^GITHUB_APP_CLIENT_ID=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "case \"$device_enabled\" in "
                + "true) printf 'github_device_flow=enabled\\n';; "
                + "false|'') printf 'github_device_flow=disabled\\n';; "
                + "*) printf 'github_device_flow=invalid\\n';; esac; "
                + "if test -n \"$device_client_id\"; then "
                + "printf 'github_device_flow_app_client_id=configured\\n'; "
                + "else printf 'github_device_flow_app_client_id=missing\\n'; fi; "
                + "token_key=$(sed -n 's/^GITHUB_TOKEN_ENCRYPTION_KEY=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "if test -n \"$token_key\"; then "
                + "printf 'github_device_flow_token_encryption_key=configured\\n'; "
                + "else printf 'github_device_flow_token_encryption_key=missing\\n'; fi; "
                + "slug=$(sed -n 's/^GITHUB_APP_SLUG=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "secret=$(sed -n 's/^GITHUB_WEBHOOK_SECRET=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "app_id=$(sed -n 's/^GITHUB_APP_ID=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "key=$(sed -n 's/^GITHUB_APP_PRIVATE_KEY=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "public_url=$(sed -n 's/^MULTICA_PUBLIC_URL=//p' "
                + q(args.nas_target + "/.env")
                + " | head -n 1); "
                + "if test -n \"$slug\" && test -n \"$secret\"; then "
                + "printf 'github_app=basic\\n'; "
                + "if test -n \"$app_id\" && test -n \"$key\"; then "
                + "printf 'github_app_credentials=complete\\n'; "
                + "else printf 'github_app_credentials=missing_optional\\n'; fi; "
                + "else printf 'github_app=disabled\\n'; fi; "
                + "if test -n \"$public_url\" && test -n \"$slug\" && test -n \"$secret\"; then "
                + "printf 'github_webhook=public-url-configured\\n'; "
                + "elif test -n \"$public_url\"; then "
                + "printf 'github_webhook=public-url-without-app\\n'; "
                + "else printf 'github_webhook=not-configured (LAN/NetBird is okay)\\n'; fi; "
                + "else printf 'github_app=unknown\\n'; "
                + "printf 'github_webhook=unknown (env missing)\\n'; fi"
            ),
        ]
    )
    print(f"NAS 检查: {args.nas_host}")
    output = remote_capture(args, command)
    print(output, end="")
    try:
        with open_service_url(args, "/readyz") as response:
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
    rollback_args.backend_ref = state.get("MULTICA_BACKEND_REF", "")
    rollback_args.web_ref = state.get("MULTICA_WEB_REF", "")
    rollback_args.no_pull = (
        backend in {"multica-backend", "multica-backend:dev"}
        or web in {"multica-web", "multica-web:dev"}
    )
    validate_config(rollback_args)
    print(f"回滚到 {tag}（backend={backend or '官方镜像'}, web={web or '官方镜像'}）")
    deploy(rollback_args)


def wait_for_backend_ready(args: argparse.Namespace, addresses: TargetAddresses) -> None:
    """Wait until the newly replaced backend has completed startup/migrations."""

    health = textwrap.dedent(
        f"""
        for i in 1 2 3 4 5 6 7 8 9 10; do
          if curl -fsS --max-time 5 {q(addresses.service_origin)}/readyz; then exit 0; fi
          sleep 3
        done
        exit 1
        """
    ).strip()
    remote(args, health)


def wait_for_frontend_running(args: argparse.Namespace, compose_command: str) -> None:
    """Wait until Compose reports the replacement frontend container running."""

    check = textwrap.dedent(
        f"""
        for i in 1 2 3 4 5 6 7 8 9 10; do
          if {compose_command} ps --status running --services frontend | grep -qx frontend; then exit 0; fi
          sleep 2
        done
        exit 1
        """
    ).strip()
    remote(args, check)


def hot_update_services(
    args: argparse.Namespace, compose_command: str, addresses: TargetAddresses
) -> None:
    """Replace only application containers, keeping data and proxy containers up."""

    remote(
        args,
        f"{compose_command} up -d --no-deps --force-recreate backend",
    )
    wait_for_backend_ready(args, addresses)
    remote(
        args,
        f"{compose_command} up -d --no-deps --force-recreate frontend",
    )
    wait_for_frontend_running(args, compose_command)


def deploy(args: argparse.Namespace) -> None:
    check_binary("ssh")
    check_binary("scp")
    check_package()
    auto_detect_remote_docker(args)
    addresses = resolve_target_addresses(args)
    if not getattr(args, "nas_ip", ""):
        args.nas_ip = addresses.bind_address
    verify_netbird_endpoint(args)
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
        # A hot update must not overwrite a live proxy config. The NAS may
        # intentionally listen on both LAN and NetBird addresses, while the
        # generated template represents only the selected deployment origin.
        if getattr(args, "hot_update", False) and name == "Caddyfile":
            continue
        copy_to_nas(args, PACKAGE_ROOT / name)
    if getattr(args, "hot_update", False):
        print("保留 NAS 当前 Caddyfile（hot-update 不改代理监听地址）。")
    else:
        rendered_caddy = render_caddy(args)
        try:
            copy_to_nas(args, rendered_caddy)
            remote(
                args,
                f"mv {q(args.nas_target + '/' + rendered_caddy.name)} {q(args.nas_target + '/Caddyfile')}",
            )
        finally:
            rendered_caddy.unlink(missing_ok=True)

    print("[4/6] 初始化或更新非敏感配置（保留部署目标上已有密钥）...")
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

    if getattr(args, "hot_update", False):
        print("[6/6] 快速更新 backend/frontend，并等待逐个就绪（postgres/Caddy 保持运行）...")
        hot_update_services(args, compose_command, addresses)
    else:
        print("[6/6] 启动服务并等待数据库迁移完成...")
        remote(args, f"{compose_command} up -d --remove-orphans")
        wait_for_backend_ready(args, addresses)
    print("安装 NAS 级 Multica watchdog...")
    install_watchdog(args)
    write_current_release_state(args)
    sync_desktop(args)
    print(f"部署完成: 浏览器入口 {addresses.browser_origin}")
    print(f"健康检查: {addresses.service_origin}/readyz")
    print(f"OAuth 回调: {addresses.oauth_callback_url}")
    print(f"Plane: {addresses.plane_url or '未配置'}")
    print(f"Plane 检查: {check_plane_url(args)}")
    print("代码源下一步：登录 Multica 后，在集成设置选择 GitHub、Gitea 或其他自托管 Git，并完成仓库连接验证。")
    print("桌面端配对：当前版本尚未实现一次性配对协议，请先在桌面端手动输入浏览器入口并完成登录。")


def status(args: argparse.Namespace) -> None:
    check_binary("ssh")
    check_package()
    auto_detect_remote_docker(args)
    verify_netbird_endpoint(args)
    addresses = resolve_target_addresses(args)
    print(f"浏览器入口: {addresses.browser_origin}")
    print(f"服务间入口: {addresses.service_origin}")
    print(f"OAuth 回调: {addresses.oauth_callback_url}")
    print(f"Plane: {addresses.plane_url or '未配置'}")
    print(f"Plane 检查: {check_plane_url(args)}")
    print(f"GitHub Device Flow: {github_device_flow_state(args)}")
    print("GitHub App/webhook: optional advanced capability; public HTTPS is not required for LAN/NetBird")
    print("\n容器:")
    remote(args, f"{compose(args)} ps")
    print("\n就绪检查:")
    remote(args, f"curl -fsS --max-time 5 {q(addresses.service_origin)}/readyz")
    try:
        with urllib.request.urlopen(
            addresses.browser_origin.rstrip("/") + "/health", timeout=5
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
    parser.add_argument("--nas-host", default=DEFAULTS["nas_host"], help="SSH 管理主机、IP 或 SSH config 别名")
    parser.add_argument("--ssh-port", type=int, default=0, help="SSH 端口；0 表示使用 SSH 配置")
    parser.add_argument("--nas-ip", default=DEFAULTS["nas_ip"], help="目标绑定/服务间地址；历史参数名保留，可填 IP 或主机名")
    parser.add_argument(
        "--browser-url", "--browser-origin", dest="browser_url", default=DEFAULTS["browser_url"],
        help="浏览器访问 origin，例如 http://nas.example:YOUR_APP_PORT 或 https://multica.example.com",
    )
    parser.add_argument(
        "--service-url", default=DEFAULTS["service_url"],
        help="目标主机/服务间访问 origin；省略时由 --nas-ip 和 --app-port 推导",
    )
    parser.add_argument(
        "--oauth-origin", "--oauth-callback-origin", dest="oauth_origin",
        default=DEFAULTS["oauth_origin"], help="OAuth 回调 origin；省略时使用浏览器访问 origin",
    )
    parser.add_argument(
        "--plane-url", default=DEFAULTS["plane_url"],
        help="可选 Plane 任务控制面 origin；只保存并展示，不假设固定主机或端口",
    )
    parser.add_argument(
        "--public-url",
        default=DEFAULTS["public_url"],
        help="可选的公网 HTTPS origin；用于 GitHub/webhook 等外部回调",
    )
    device_flow_group = parser.add_mutually_exclusive_group()
    device_flow_group.add_argument(
        "--github-device-flow",
        dest="github_device_flow_enabled",
        action="store_true",
        default=DEFAULTS["github_device_flow_enabled"],
        help="启用本地/桌面 GitHub Device Flow；只需要非敏感 client ID，不需要公网 webhook",
    )
    device_flow_group.add_argument(
        "--no-github-device-flow",
        dest="github_device_flow_enabled",
        action="store_false",
        help="关闭 GitHub Device Flow",
    )
    parser.add_argument(
        "--github-device-flow-client-id",
        default=DEFAULTS["github_device_flow_client_id"],
        help="GitHub OAuth App 的 Device Flow client ID（非敏感，不是 secret）",
    )
    overlay_group = parser.add_mutually_exclusive_group()
    overlay_group.add_argument(
        "--netbird",
        action="store_true",
        default=DEFAULTS["netbird"],
        help="把 --nas-ip 作为 NAS NetBird 地址；部署前校验 NetBird 已连接，且 Caddy 只绑定该地址",
    )
    overlay_group.add_argument(
        "--no-netbird",
        dest="netbird",
        action="store_false",
        help="不使用 NetBird 模式；覆盖已保存的 --netbird 设置",
    )
    parser.add_argument("--nas-target", default=DEFAULTS["nas_target"], help="远端部署目录（历史参数名保留 nas）")
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


def add_desktop_options(parser: argparse.ArgumentParser) -> None:
    desktop_group = parser.add_mutually_exclusive_group()
    desktop_group.add_argument(
        "--desktop-sync",
        dest="desktop_sync",
        action="store_true",
        default=DEFAULTS["desktop_sync"],
        help="Windows 部署机自动安装匹配版本的桌面端并恢复本地 profile",
    )
    desktop_group.add_argument(
        "--no-desktop-sync",
        dest="desktop_sync",
        action="store_false",
        help="不在 deploy/upgrade/build 后同步 Windows 桌面端",
    )
    parser.add_argument(
        "--desktop-version",
        default=DEFAULTS["desktop_version"],
        help="桌面端版本；默认跟随 --image-tag 的正式版本，否则使用 latest",
    )
    parser.add_argument(
        "--desktop-profile",
        default=DEFAULTS["desktop_profile"],
        help="桌面端 CLI profile 名称",
    )


def add_deploy_options(parser: argparse.ArgumentParser) -> None:
    add_common(parser)
    add_desktop_options(parser)
    parser.add_argument("--image-tag", default=DEFAULTS["image_tag"])
    parser.add_argument(
        "--hot-update",
        action="store_true",
        help="快速替换 backend/frontend；不重启 PostgreSQL、Caddy，不重新拉取镜像",
    )
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
        description=f"{PRODUCT_NAME}（兼容项目名：{LEGACY_REPOSITORY_NAME}；Python 标准库，无 pip 依赖）"
    )
    subparsers = parser.add_subparsers(dest="command")

    deploy_parser = subparsers.add_parser("deploy", help="部署或升级 Multica 本地版")
    add_deploy_options(deploy_parser)
    deploy_parser.add_argument("--no-pull", action="store_true", help="跳过镜像拉取")

    upgrade_parser = subparsers.add_parser("upgrade", help="升级 Multica 本地版镜像版本")
    add_deploy_options(upgrade_parser)
    upgrade_parser.add_argument("--no-pull", action="store_true", help="跳过镜像拉取")

    build_parser = subparsers.add_parser(
        "build",
        help="从本地 Multica 源码构建、上传并部署（维护者快捷流程）",
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

    doctor_parser = subparsers.add_parser("doctor", help="只读检查本机和远端部署环境")
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

    github_device_parser = subparsers.add_parser(
        "github-device", aliases=["github-device-flow"],
        help="配置本地/桌面 GitHub Device Flow（仅 client ID 和开关）",
    )
    add_common(github_device_parser)
    github_device_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    github_device_parser.add_argument("--owner", default=DEFAULTS["owner"])
    github_device_parser.add_argument("--group", default=DEFAULTS["group"])

    wizard_parser = subparsers.add_parser("wizard", help="打开 Multica 本地版引导式安装/管理菜单")
    add_common(wizard_parser)
    wizard_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    wizard_parser.add_argument("--source-dir", default=DEFAULTS["source_dir"])
    wizard_parser.add_argument("--guided", action="store_true", help=argparse.SUPPRESS)

    fleet_parser = subparsers.add_parser(
        "fleet", help="规划和执行 AGX/Multica fleet 工作流"
    )
    fleet_subparsers = fleet_parser.add_subparsers(
        dest="fleet_command", required=True
    )
    fleet_plan_parser = fleet_subparsers.add_parser(
        "plan", help="只读校验并输出 one-node fleet plan"
    )
    fleet_plan_parser.add_argument(
        "--contract",
        "--contract-file",
        "--file",
        dest="contract",
        required=True,
        type=Path,
        help="secret-free v1 deployment contract JSON",
    )
    fleet_plan_parser.add_argument(
        "--format",
        "--output-format",
        dest="output_format",
        choices=("human", "json"),
        default="human",
        help="输出格式（默认 human）",
    )
    fleet_plan_parser.add_argument(
        "--json",
        dest="output_format",
        action="store_const",
        const="json",
        help="输出稳定的 JSON 结果",
    )

    fleet_apply_parser = fleet_subparsers.add_parser(
        "apply", help="按顺序部署 Multica、AGX 和官方 CLI/daemon"
    )
    add_deploy_options(fleet_apply_parser)
    fleet_apply_parser.add_argument(
        "--contract",
        "--contract-file",
        "--file",
        dest="contract",
        required=True,
        type=Path,
        help="secret-free v1 deployment contract JSON",
    )
    fleet_apply_parser.add_argument(
        "--format",
        "--output-format",
        dest="output_format",
        choices=("human", "json"),
        default="human",
        help="输出格式（默认 human）",
    )
    fleet_apply_parser.add_argument(
        "--json",
        dest="output_format",
        action="store_const",
        const="json",
        help="输出稳定的 JSON 结果",
    )
    fleet_apply_parser.add_argument(
        "--state-file",
        type=Path,
        help="本地脱敏 apply 状态文件；默认按合同摘要分文件",
    )
    fleet_apply_parser.add_argument(
        "--resume",
        action="store_true",
        help="从上次失败的阶段继续（同一合同默认也会跳过已完成阶段）",
    )
    fleet_apply_parser.add_argument(
        "--source-dir",
        default=DEFAULTS["source_dir"],
        help="source_revision 合同使用的 Multica checkout",
    )
    fleet_apply_parser.add_argument(
        "--platform",
        choices=("auto", "linux/amd64", "linux/arm64", "linux/arm/v7"),
        default=DEFAULTS["platform"],
        help="源码构建目标架构",
    )
    fleet_apply_parser.add_argument("--node-host", default="", help="AGX 节点 SSH 主机或别名")
    fleet_apply_parser.add_argument("--node-ssh-port", type=int, default=0, help="AGX 节点 SSH 端口")
    fleet_apply_parser.add_argument("--agx-bin", default="agx", help="节点上的 AGX 官方 CLI")
    fleet_apply_parser.add_argument(
        "--agx-bundle",
        default="",
        help="可选的节点本地 Bundle JSON；省略时使用 AGX 内置 production Bundle",
    )
    fleet_apply_parser.add_argument("--agx-github-owner", default="")
    fleet_apply_parser.add_argument(
        "--agx-provider", choices=("codex", "claude", "both"), default=""
    )
    fleet_apply_parser.add_argument("--device-name", default="")
    fleet_apply_parser.add_argument("--runtime-name", default="")

    fleet_verify_parser = fleet_subparsers.add_parser(
        "verify", help="现场读取或校验一节点的 Multica/AGX 双侧证据"
    )
    add_common(fleet_verify_parser)
    fleet_verify_parser.add_argument("--app-port", type=int, default=DEFAULTS["app_port"])
    fleet_verify_parser.add_argument("--node-host", default="", help="AGX 节点 SSH 主机或别名")
    fleet_verify_parser.add_argument("--node-ssh-port", type=int, default=0, help="AGX 节点 SSH 端口")
    fleet_verify_parser.add_argument("--agx-bin", default="agx", help="节点上的 AGX 官方 CLI")
    fleet_verify_parser.add_argument("--device-name", default="")
    fleet_verify_parser.add_argument("--runtime-name", default="")
    fleet_verify_parser.add_argument(
        "--contract",
        "--contract-file",
        "--file",
        dest="contract",
        required=True,
        type=Path,
        help="secret-free v1 deployment contract JSON",
    )
    fleet_verify_parser.add_argument(
        "--evidence-file",
        required=False,
        type=Path,
        help="由官方 CLI/AGX 读回生成的结构化证据 JSON；不接受 mock",
    )
    fleet_verify_parser.add_argument(
        "--live",
        action="store_true",
        help="现场读取 NAS、官方 Multica CLI/daemon 与 AGX；缺少公开任务连接器时明确阻断",
    )
    fleet_verify_parser.add_argument(
        "--format",
        "--output-format",
        dest="output_format",
        choices=("json",),
        default="json",
        help="输出 JSON 证据结果",
    )
    fleet_verify_parser.add_argument(
        "--json",
        dest="output_format",
        action="store_const",
        const="json",
        help=argparse.SUPPRESS,
    )

    fleet_multi_parser = fleet_subparsers.add_parser(
        "multi", help="只读生成多节点选择与状态聚合计划"
    )
    fleet_multi_parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="secret-free multi-node fleet JSON",
    )

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
    if args.command == "fleet":
        if args.fleet_command == "plan":
            from fleet_plan import run_plan

            return run_plan(args.contract, output_format=args.output_format)
        if args.fleet_command == "apply":
            args._explicit_options = {
                option
                for options in CONFIG_OPTIONS.values()
                for option in options
                if option_supplied(raw_args, option)
            }
            apply_saved_config(args)
            return run_fleet_apply(args)
        if args.fleet_command == "verify":
            return run_fleet_verify(args)
        if args.fleet_command == "multi":
            return run_fleet_multi(args)
        parser.error(f"未知 fleet 子命令: {args.fleet_command}")
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
        elif args.command in {"github-device", "github-device-flow"}:
            validate_config(args)
            configure_github_device_flow(args)
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
