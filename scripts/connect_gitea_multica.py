"""Connect XART Gitea to Multica (VCS connection) and register per-repo webhooks.

Secrets (Multica bearer, Gitea token, webhook secret) stay inside this process; nothing
secret is printed. Idempotent-ish: reuses an existing gitea connection for the same
instance_url if present, and skips repos that already have an active hook pointing at
the Multica webhook URL.
"""
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

MULTICA_CFG = os.path.expanduser("~/.multica/config.json")
GITEA_API = "https://git.xart.top:8418/api/v1"
INSTANCE_URL = "https://git.xart.top:8418"
CONNECTION_NAME = "XART Gitea (git.xart.top)"
REPOS = [
    "chen-qianyu/quant--k-atana",
    "Curry/tdxcli-rs",
    "Curry/obsidian-llm-wiki",
    "Curry/code-intel-pipeline",
    "Curry/imvt",
    "chen-qianyu/claude-dotfiles",
]
# Multica docs: Gitea format, Pull Request (+ Commit Status) events.
HOOK_EVENTS = [
    "pull_request",
    "pull_request_sync",
    "pull_request_label",
    "pull_request_assign",
    "pull_request_comment",
    "pull_request_review",
    "status",
]

cfg = json.load(open(MULTICA_CFG, encoding="utf-8"))
MULTICA_TOKEN = next(v for k, v in cfg.items() if "token" in k.lower())
WS = cfg["workspace_id"]
MULTICA = cfg["server_url"].rstrip("/")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN")
if not GITEA_TOKEN:
    sys.exit("GITEA_TOKEN not in env")


def call(url, method="GET", body=None, headers=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


mh = {"Authorization": f"Bearer {MULTICA_TOKEN}"}
gh = {"Authorization": f"token {GITEA_TOKEN}"}

# 1. Multica VCS connection (reuse if the instance is already connected)
st, listing = call(f"{MULTICA}/api/workspaces/{WS}/vcs/connections", headers=mh)
print("connections list:", st, {k: v for k, v in (listing or {}).items() if k != "connections"})
existing = [c for c in (listing or {}).get("connections", []) if c.get("instance_url", "").rstrip("/") == INSTANCE_URL]
webhook_secret = None
if existing:
    conn = existing[0]
    conn_id = conn["id"]
    print("reusing existing connection", conn_id, conn.get("provider"), conn.get("name"))
    # Existing connection: secret not retrievable -> rotate so we can register hooks.
    st, rot = call(f"{MULTICA}/api/workspaces/{WS}/vcs/connections/{conn_id}/rotate-webhook", "POST", {}, mh)
    print("rotate-webhook:", st, sorted((rot or {}).keys()))
    webhook_secret = (rot or {}).get("webhook_secret")
    webhook_url = (rot or {}).get("webhook_url") or conn.get("webhook_url")
else:
    webhook_secret = secrets.token_urlsafe(32)
    payload = {
        "provider": "gitea",
        "instance_url": INSTANCE_URL,
        "access_token": GITEA_TOKEN,
        "name": CONNECTION_NAME,
        "webhook_secret": webhook_secret,
    }
    st, conn = call(f"{MULTICA}/api/workspaces/{WS}/vcs/connections", "POST", payload, mh)
    if st >= 300:
        sys.exit(f"connect failed: {st} {json.dumps(conn)[:400]}")
    conn_id = conn.get("id") or conn.get("connection", {}).get("id")
    webhook_url = conn.get("webhook_url") or conn.get("connection", {}).get("webhook_url")
    if conn.get("webhook_secret"):
        webhook_secret = conn["webhook_secret"]
    print("connected:", st, "id", conn_id, "keys", sorted(conn.keys()))

if not webhook_url:
    webhook_url = f"/api/webhooks/vcs/{conn_id}"
if webhook_url.startswith("/"):
    webhook_url = MULTICA + webhook_url
# Gitea (native Synology package) only allows webhook targets in
# [webhook] ALLOWED_HOST_LIST = 127.0.0.1,localhost,192.168.50.130 — so register the
# LAN address of the same Caddy (it binds both 100.80.110.105 and 192.168.50.130).
webhook_url = webhook_url.replace("100.80.110.105", "192.168.50.130")
print("webhook_url (as registered in Gitea):", webhook_url)
if not webhook_secret:
    sys.exit("no webhook secret available; cannot register Gitea hooks")

# 2. Gitea per-repo webhooks
for repo in REPOS:
    st, hooks = call(f"{GITEA_API}/repos/{repo}/hooks", headers=gh)
    if st != 200:
        print(f"  {repo}: list hooks failed {st} {json.dumps(hooks)[:120]}")
        continue
    dup = [h for h in hooks if h.get("config", {}).get("url") == webhook_url]
    if dup:
        # Replace so the secret matches the (possibly rotated) connection secret.
        for h in dup:
            call(f"{GITEA_API}/repos/{repo}/hooks/{h['id']}", "DELETE", headers=gh)
        print(f"  {repo}: replaced {len(dup)} stale Multica hook(s)")
    body = {
        "type": "gitea",
        "active": True,
        "branch_filter": "*",
        "config": {"url": webhook_url, "content_type": "json", "secret": webhook_secret},
        "events": HOOK_EVENTS,
    }
    st, created = call(f"{GITEA_API}/repos/{repo}/hooks", "POST", body, gh)
    if st >= 300 and "status" in HOOK_EVENTS:
        body["events"] = [e for e in HOOK_EVENTS if e != "status"]
        st, created = call(f"{GITEA_API}/repos/{repo}/hooks", "POST", body, gh)
        note = " (without status event)"
    else:
        note = ""
    if st >= 300:
        print(f"  {repo}: hook create failed {st} {json.dumps(created)[:200]}")
    else:
        print(f"  {repo}: hook id {created.get('id')} active={created.get('active')}{note}")

# 3. Confirm
st, listing = call(f"{MULTICA}/api/workspaces/{WS}/vcs/connections", headers=mh)
for c in (listing or {}).get("connections", []):
    print("connection:", c.get("id"), c.get("provider"), c.get("instance_url"), c.get("name"), "status", c.get("status") or c.get("health") or "-")
