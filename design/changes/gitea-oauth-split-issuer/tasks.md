# Tasks

- [ ] Add an optional server-side issuer override to `.env.template`, documented as falling back to `GITEA_ISSUER_URL` when unset.
- [ ] Thread the override through `docker-compose.selfhost.yml` / `docker-compose.nas.yml` so the backend receives it.
- [ ] Confirm whether upstream Multica accepts a separate back-channel issuer. If it does not, implement the host-mapping fallback instead and document why URL substitution is not equivalent (TLS SNI / certificate coverage).
- [ ] Add a deploy-time reachability check for the OAuth back-channel, executed **on the deployment target**, not on the management machine.
- [ ] Surface the check in deployment output and in the status command; do not report a deployment as complete when the back-channel is unreachable.
- [ ] Document the recreate-not-restart requirement for changed environment values.
- [ ] Regression: a single-address deployment with the override unset must behave exactly as before.
