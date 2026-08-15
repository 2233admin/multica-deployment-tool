#!/usr/bin/env python3
"""Cross-platform first-run entry point for the Multica deployment tool."""

from __future__ import annotations

import os
import shutil
import sys

import multica_deploy


def main() -> int:
    missing = [name for name in ("ssh", "scp") if shutil.which(name) is None]
    if missing:
        platform_hint = (
            "Windows OpenSSH Client"
            if os.name == "nt"
            else "openssh-client（Debian/Ubuntu）或 openssh-clients（Fedora/RHEL）"
        )
        print(
            f"缺少 {', '.join(missing)}。请先安装 {platform_hint}，再重新运行 install.py。",
            file=sys.stderr,
        )
        return 1
    print("Multica 部署工具：首次运行请在菜单中填写目标 NAS 参数。")
    return multica_deploy.main(["wizard", "--guided", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
