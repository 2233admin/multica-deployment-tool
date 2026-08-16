# Multica Deployment Tool

An independent installer and maintenance CLI for running [Multica](https://github.com/multica-ai/multica) inside your network.

It is designed for the awkward parts of self-hosting: first-run setup, target-host path discovery, secrets stored only on the deployment target, source builds, upgrades, health checks, and safe rollback. It does not merge Multica with `agent-control`, `agent-plugins`, or Gitea; those remain optional integrations.

中文文档：[README.zh-CN.md](README.zh-CN.md)

## What it supports

- Windows, Linux, or macOS as the management machine
- Synology Container Manager or ordinary Linux Docker hosts
- Stable image deployment and local Multica source builds
- Gitea OAuth/OIDC, SMTP/Resend, GitHub App, and optional plugin configuration
- Read-only diagnostics, redacted logs, upgrade, and release-state rollback
- No pip package or registry requirement for the source-build transfer loop

## Requirements

For a normal image deployment:

- Python 3.9+
- OpenSSH client (`ssh` and `scp`)
- An SSH account on the target with Docker access, either directly or through non-interactive `sudo`

For `build` from a Multica checkout, also install Docker Desktop or a Linux Docker daemon. Cross-architecture builds require Docker buildx.

## Platform matrix

| Role | Supported environments |
| --- | --- |
| Management machine | Windows, Linux, macOS |
| Local image builder | Docker Desktop on Windows/macOS, or Linux Docker |
| Remote deployment target | Synology Container Manager or a Linux Docker host reached through SSH |

The `--nas-*` option names are kept for compatibility, but the target does not have to be a NAS. A Windows Docker target is not part of this version yet: the remote deployment flow relies on a POSIX shell, `sudo`, `sed`, `curl`, and Linux-style Compose paths. Docker Desktop on Windows is fully supported as the local builder.

### NetBird private overlay

Pass `--netbird` with the NAS NetBird IPv4 address in `--nas-ip` to deploy a cross-site private endpoint. Before changing the stack, the tool verifies that NetBird is running on the target, connected to management and signal services, and using the requested address. Caddy is bound only to that address, and NetBird health checks bypass the management machine's HTTP proxy.

```bash
python3 multica_deploy.py deploy \
  --nas-host nas --nas-ip 100.80.110.105 --netbird
```

Every desktop or remote device must join the same NetBird network and be allowed to reach TCP 3010 on the target. Use the NetBird address as the Multica server URL and exclude the NetBird range from any local HTTP proxy. Use `--no-netbird` to override a saved NetBird setting when intentionally switching back to a normal private-LAN deployment.

## Quick start

Clone the repository and run the guided installer from its root:

```bash
git clone https://github.com/2233admin/multica-deployment-tool.git
cd multica-deployment-tool
python3 install.py
```

On Windows:

```powershell
git clone https://github.com/2233admin/multica-deployment-tool.git
cd multica-deployment-tool
python .\install.py
```

The wizard checks SSH, discovers common Synology Docker paths, asks for the target address, creates application secrets on the target host, deploys the stack, and verifies `/readyz`. It does not require manual `.env` editing.

For routine work, use `python multica_deploy.py wizard` on any platform or `bash multica-tool.sh` on Linux/macOS. Windows `.cmd` and PowerShell wrappers live under `compat/windows/` as optional compatibility entry points.

## Daily commands

All commands require the SSH host and the internal address unless you have saved them through the wizard. Replace the placeholders; the repository contains no fixed NAS address.

```bash
python3 multica_deploy.py doctor --nas-host NAS_SSH_HOST --nas-ip NAS_IP
python3 multica_deploy.py status --nas-host NAS_SSH_HOST --nas-ip NAS_IP
python3 multica_deploy.py upgrade --nas-host NAS_SSH_HOST --nas-ip NAS_IP --image-tag v0.4.27
python3 multica_deploy.py rollback --nas-host NAS_SSH_HOST --nas-ip NAS_IP
python3 multica_deploy.py logs --nas-host NAS_SSH_HOST --nas-ip NAS_IP --service backend --since 15m
```

Run `doctor` before a change. `rollback` only uses the previous successful image record and never pretends that a database migration is reversible; restore a database snapshot first when required.

## Build and deploy a modified Multica checkout

When you maintain a Multica fork, one command builds backend/web locally, transfers a temporary image archive over SSH, loads it on the NAS, restarts the stack, and checks `/readyz`:

```powershell
python .\multica_deploy.py build `
  --source-dir ..\multica `
  --nas-host NAS_SSH_HOST --nas-ip NAS_IP `
  --image-tag dev-20260816
```

The tool detects the NAS architecture. Same-architecture builds use the checkout's Compose override; cross-architecture builds use buildx. A registry is optional, not required.

## Windows compatibility layer

The deployment engine is Python-first. The files under `compat/windows/` are convenience wrappers for operators or Windows Agent devices that already use PowerShell; they are not required by the installer or the core CLI.

## Secrets and network boundary

- JWT, database, SMTP, Gitea, GitHub, and VCS secrets stay in the NAS `.env` or application storage.
- The management-machine config stores only connection and image settings.
- The Compose file refuses to start without `JWT_SECRET` and `POSTGRES_PASSWORD`.
- The default deployment is internal HTTP. Put HTTPS in front of it before exposing anything beyond your trusted network.

## Optional integrations

The adapter under `adapters/agent-plugins-multica/` converts static skills into Multica Private Plugin V1 archives. `agent-control` tools remain on the Agent device and can be exposed through a local CLI or MCP server. Neither repository is a runtime dependency of this tool.

## Maintainer release

```bash
python3 package.py
```

This creates `dist/multica-deployment-kit.zip` and its SHA256 file without including `.env`, local configuration, logs, or Python cache files.

## License

MIT. See [LICENSE](LICENSE).
