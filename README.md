# Multica Deployment Tool

An independent, zero-dependency installer and maintenance CLI for self-hosted Multica on Synology NAS or ordinary Linux Docker hosts.

The tool keeps Multica deployment separate from `agent-control`, `agent-plugins`, and Gitea. Those systems are optional integrations, not runtime dependencies.

## Quick start

```bash
python3 install.py
```

Windows:

```powershell
python .\install.py
```

The guided installer checks OpenSSH, discovers common Synology paths, creates NAS-only secrets, deploys the stack, and verifies `/readyz`. For routine work use `multica-tool.cmd` on Windows or `bash multica-tool.sh` on Linux/macOS.

See [README.zh-CN.md](README.zh-CN.md) for the complete workflow, Gitea configuration, source builds, upgrades, rollback, and agent integration boundaries.

## Maintainer packaging

```bash
python3 package.py
```

This creates a clean ZIP and SHA256 file under `dist/` without including operator secrets or local runtime state.

## License

MIT. See [LICENSE](LICENSE).
