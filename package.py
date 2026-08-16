#!/usr/bin/env python3
"""Build a clean, reproducible ZIP distribution of this deployment tool."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
FILES = (
    (".env.template", ".env.template"),
    ("Caddyfile", "Caddyfile"),
    ("compat/windows/client-bootstrap.ps1", "compat/windows/client-bootstrap.ps1"),
    ("client-bootstrap.sh", "client-bootstrap.sh"),
    ("compat/windows/deploy.ps1", "compat/windows/deploy.ps1"),
    ("docker-compose.nas.yml", "docker-compose.nas.yml"),
    ("docker-compose.selfhost.yml", "docker-compose.selfhost.yml"),
    ("install.py", "install.py"),
    ("compat/windows/logs.ps1", "compat/windows/logs.ps1"),
    ("multica_deploy.py", "multica_deploy.py"),
    ("compat/windows/multica-admin.cmd", "compat/windows/multica-admin.cmd"),
    ("multica-admin.sh", "multica-admin.sh"),
    ("compat/windows/multica-deploy.cmd", "compat/windows/multica-deploy.cmd"),
    ("multica-deploy.sh", "multica-deploy.sh"),
    ("compat/windows/multica-tool.cmd", "compat/windows/multica-tool.cmd"),
    ("multica-tool.sh", "multica-tool.sh"),
    ("package.py", "package.py"),
    ("README.zh-CN.md", "README.zh-CN.md"),
    ("compat/windows/status.ps1", "compat/windows/status.ps1"),
    ("test_multica_deploy.py", "test_multica_deploy.py"),
    ("compat/windows/verification-code.ps1", "compat/windows/verification-code.ps1"),
    ("adapters/agent-plugins-multica/agent_plugins_to_multica.py", "adapters/agent-plugins-multica/agent_plugins_to_multica.py"),
    ("adapters/agent-plugins-multica/AGENT-PLUGINS-BUNDLE-README.md", "adapters/agent-plugins-multica/AGENT-PLUGINS-BUNDLE-README.md"),
    ("adapters/agent-plugins-multica/INTEGRATION.agent-control.md", "adapters/agent-plugins-multica/INTEGRATION.agent-control.md"),
    ("adapters/agent-plugins-multica/MULTICA-ADAPTER-README.md", "adapters/agent-plugins-multica/MULTICA-ADAPTER-README.md"),
    ("adapters/agent-plugins-multica/test_agent_plugins_to_multica.py", "adapters/agent-plugins-multica/test_agent_plugins_to_multica.py"),
)


def build_archive(output: Path) -> tuple[Path, str]:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    resolved: list[tuple[Path, str]] = []
    missing: list[str] = []
    for source, archive_name in FILES:
        source_path = ROOT / source
        if not source_path.is_file():
            missing.append(source)
        else:
            resolved.append((source_path, archive_name))
    if missing:
        raise SystemExit("部署包缺少文件: " + ", ".join(missing))
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for source_path, archive_name in resolved:
            archive.write(source_path, archive_name)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + ".sha256").write_text(digest + "\n", encoding="ascii")
    return output, digest


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Multica Deployment Tool ZIP")
    parser.add_argument(
        "--output",
        default=str(ROOT / "dist" / "multica-deployment-kit.zip"),
        help="ZIP 输出路径（默认写入本目录 dist/）",
    )
    args = parser.parse_args()
    output, digest = build_archive(Path(args.output))
    print(f"已生成: {output}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
