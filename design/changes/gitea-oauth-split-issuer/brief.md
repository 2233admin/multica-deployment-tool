# Gitea OAuth: split browser-facing and server-facing issuer URLs

`GITEA_ISSUER_URL` is a single value used for two different network paths. On any deployment
where the Gitea hostname the browser uses is not reachable *from the deployment target*, login
fails at the token-exchange step even though every credential is correct.

## Observed failure

Authorization succeeds (the browser reaches Gitea and returns to the callback), then the backend
fails to exchange the code:

```
ERR Gitea OAuth token exchange failed
    error="Post \"https://<gitea-public-host>/login/oauth/access_token\":
           context deadline exceeded (Client.Timeout exceeded while awaiting headers)"
ERR http request method=POST path=/auth/gitea status=502 duration=15.0s
```

The 15-second duration is the HTTP client timeout, not a Gitea rejection. Gitea never sees the
request.

## Why it happens

Two different clients resolve the same URL:

| Step | Client | Needs |
| --- | --- | --- |
| authorize / redirect | the operator's browser | a hostname reachable from the operator's network |
| token exchange | the Multica backend, inside the deployment target | a hostname reachable from the target host |

These are frequently not the same address in self-hosted setups: the public hostname may resolve
to a WAN address the target cannot loop back to (hairpin NAT), may be served by a reverse proxy
that is only reachable from outside, or may simply be blocked by the target's egress rules — while
the same Gitea is directly reachable over the LAN or an overlay network.

`.env.template` currently exposes one `GITEA_ISSUER_URL`, and no code in this repository reads or
validates it. So the operator cannot express the split even when they know it exists, and the tool
reports a healthy deployment while login is broken.

This mirrors the browser-facing values that already use a placeholder for the target address
(`FRONTEND_ORIGIN`, `MULTICA_APP_URL`, `GITEA_REDIRECT_URI`) — the issuer is the one address that
needs *both* forms.

## Proposed change

1. Keep `GITEA_ISSUER_URL` as the browser-facing issuer, unchanged in meaning, so existing
   deployments keep working.
2. Add an optional server-side override — the address the backend uses for token exchange and
   any other back-channel call. When unset, it falls back to `GITEA_ISSUER_URL`, so single-address
   deployments need no configuration.
3. Verify both paths at deploy time rather than at first login:
   - reachability of the browser-facing issuer is the operator's to confirm;
   - reachability of the server-side issuer is checked **from the deployment target**, since that
     is the client that actually fails.
4. Report the result in the deployment output. A deployment whose OAuth back-channel is unreachable
   should not be presented as complete.

Upstream Multica may only accept one issuer value. If so, the equivalent target-side fix is a host
mapping that makes the browser-facing hostname resolve to a reachable address on the target
(`extra_hosts` in compose). That preserves TLS SNI and certificate matching, which a plain URL
substitution does not — worth stating explicitly, because substituting a bare IP for a hostname
will break certificate verification unless the certificate covers that IP.

## Out of scope

- Merging Multica with `agent-control`, `agent-plugins`, or Gitea.
- Managing or rotating the OAuth client secret. Note only that changing an OAuth application's
  registration in Gitea **regenerates the client secret**, that the API cannot read it back
  afterwards, and that the container must be recreated rather than restarted for a changed
  environment value to take effect.
- Any change to the browser-facing URL scheme.

## Resolution (2026-08-17)

Shipped as the host-mapping fallback (option 2 in "Proposed change"), not a second issuer URL —
this repository doesn't build or carry the `gitea-oauth-native` backend fork, so it can't add a
variable the Go binary would need to read. `GITEA_BACKEND_HOST_OVERRIDE` is applied purely as a
Docker `extra_hosts` entry on the `backend` service; `GITEA_ISSUER_URL` never changes.

On the NAS this was diagnosed against, the failure had *two* layers, not one:

1. The address split this brief describes: `git.xart.top` is not reachable from the deployment
   target over its normal route.
2. A second, narrower gap discovered while fixing it: even the LAN-facing address
   (`192.168.50.130:8418`, a DSM Reverse Proxy `nginx` process bound to `0.0.0.0:8418`) was
   reachable from the NAS host itself but **not from inside the backend container** — while the
   container had normal internet egress and could reach other host-bound ports fine. The working
   address turned out to be the Docker bridge gateway for the `multica_default` network
   (`10.253.0.1`, from `docker-compose.nas.yml`'s pinned `10.253.0.0/24` subnet), since the
   reverse proxy listens on every interface including that one. The exact kernel-level reason the
   bonded LAN interface specifically doesn't hairpin from a container was not root-caused (candidate:
   `rp_filter` on `ovs_bond0`, or Synology VMM's OVS bridging bypassing the host netfilter path
   entirely) — out of scope to chase further once a reliable working address was confirmed.

Net effect: `GITEA_BACKEND_HOST_OVERRIDE` is deployment-topology-specific by design (operators
fill in whatever address their target can actually reach — LAN IP, overlay IP, or a bridge
gateway); it is not assumed to be Gitea's "real" LAN address.
