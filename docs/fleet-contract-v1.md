# Fleet deployment contract v1

This is the first secret-free contract for the one-node end-to-end path. The
checked-in `fleet plan` and `fleet apply` commands consume this contract.

```json
{
  "contract_version": 1,
  "multica": {
    "server_url": "http://multica.example.test:4310",
    "profile": "production",
    "workspace_id": "workspace-id",
    "backend_image": "ghcr.io/2233admin/multica-backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "web_image": "ghcr.io/2233admin/multica-web@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "agx": {
    "version": "0.1.0",
    "installation_root": "/opt/agx"
  },
  "nodes": [
    {
      "name": "deploy-01",
      "node_identity": "agx-node-01",
      "platform": "linux",
      "labels": ["docker", "staging"]
    }
  ],
  "projects": [
    {
      "name": "project-a",
      "repository": "2233admin/project-a",
      "ref": "main",
      "environment": "staging"
    }
  ]
}
```

## Phases

`fleet plan` is read-only and validates versions, URLs, node selectors, immutable
image/source references, and the presence of required local tools. v1 currently
accepts Linux nodes only because the checked-in connector/bootstrap path is
Linux-only. A source build also requires local `docker` and `git`.

Every node has two distinct identifiers: `name` is the operator-facing label;
`node_identity` is the stable AGX/task binding key. `node_identity` is required
and is carried unchanged through normalization and `fleet plan` output. Verify
rejects AGX and task evidence that belongs to another self-consistent node.

The `multica` object must contain either both `backend_image` and `web_image`
(each an independent `@sha256:<64-hex>` reference) or one 40/64-hex
`source_revision`. A single image reference is never copied into both Compose
service slots.

`fleet apply` performs, in order:

1. Deploy or upgrade the Multica server using the existing Docker Compose path.
2. Install/configure the pinned AGX Bundle on the selected node.
3. Connect the node's official Multica CLI to the declared profile and workspace.
4. Run the AGX–Multica connector preflight.

Before phase 1, `fleet apply` rejects a missing `--agx-github-owner` or
`--agx-provider`; AGX initialization parameters must be complete before any
remote mutation. If a local state file says all phases are complete, apply
first runs `agx version` and then the read-only `agx status --root <root>
--output json` check, followed by official Multica CLI readiness checks.
Only a passing check returns `no_op`; a failed check replays the phases or
returns an error without claiming completion.

The AGX status readback uses its actual schema: `phase` must be
`configured`; `installation_id` and `bundle_id` must be non-empty; `missing`
and `modified` must both be empty arrays; and `initialization.status` must be
an accepted initialized state with no `problems`. Status does not carry the
contract version, installation root, node, bundle, or lifecycle fields. The
contract version is checked separately from `agx version`.

`fleet verify` must be able to prove, without creating a fake success record:

- Multica `/health` and `/readyz` are healthy.
- AGX reports the expected installation and Bundle.
- Multica auth, workspace, and runtime are online.
- A disposable task can reach AGX and return a structured result.

The deployment tool now exposes the live preflight seam:

```bash
python multica_deploy.py fleet verify \
  --live --contract contract.json \
  --nas-host nas --nas-ip 192.0.2.10 \
  --node-host agx-node
```

`--live` reads `/health` and `/readyz`, the official Multica CLI workspace and
daemon state, and AGX `version`/`status --output json`. It never treats those
readbacks as a successful disposable task. With the current AGX release, the
public status surface still does not expose `node_identity` or a Multica task
connector, so the command must return `blocked` until those two cross-repo
contracts are published. `--evidence-file` remains available for offline,
already-captured evidence and is still fail-closed.

## Ownership rules

The contract contains no token, password, SSH private key, model key, or database secret. Node credentials remain on the node. AGX owns node receipts and deployment state. Multica receives task IDs and redacted summaries, not the node's private state.
