# vps-edge

[![CI](https://github.com/PhilippKlinger/vps-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/PhilippKlinger/vps-edge/actions/workflows/ci.yml)
[![Security Checks](https://github.com/PhilippKlinger/vps-edge/actions/workflows/security.yml/badge.svg)](https://github.com/PhilippKlinger/vps-edge/actions/workflows/security.yml)
[![CodeQL](https://github.com/PhilippKlinger/vps-edge/actions/workflows/codeql.yml/badge.svg)](https://github.com/PhilippKlinger/vps-edge/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/PhilippKlinger/vps-edge/badge)](https://scorecard.dev/viewer/?uri=github.com/PhilippKlinger/vps-edge)

Shared edge proxy for all projects on the VPS: [Caddy](https://caddyserver.com/) terminates TLS and routes requests to the project stacks.

## Contents

- `compose.yaml`: Caddy (pinned by digest), the only service with published ports (80, 443, 443/udp), dropped capabilities
- `Caddyfile`: automatic HTTPS, admin API off, shared `security_headers` snippet, imports `sites/*.caddy`
- `sites/videoflix.caddy`: `/api/`, `/admin/`, `/static/`, API docs → `videoflix-web:8000` (body limit 55 MB), everything else → `videoflix-frontend:8080`
- `sites/status.caddy`: public status page of Uptime Kuma ([vps-monitoring](https://github.com/PhilippKlinger/vps-monitoring)) at `status.philipp-klinger.com`. Only the status page, its read-only API, badges and static files are let through (GET/HEAD); dashboard, login, socket.io and metrics return 404
- Docker network `edge`: project stacks join it as an external network and are reached by their network alias
- Docker network `monitoring` (internal): only Caddy and Uptime Kuma; Kuma cannot reach any project container. It is defined here, so the proxy starts even without the monitoring stack
- `.github/workflows/`: CI (compose and Caddyfile validation, actionlint, smoke test, Trivy scan of the pinned image), Security Checks (gitleaks, zizmor), CodeQL for the workflows, OpenSSF Scorecard
- `host/journald-retention.conf`: deletes logs on the VPS after 14 days at the latest

Project stacks (e.g. [videoflix-deploy](https://github.com/PhilippKlinger/videoflix-deploy)) stay independent and replaceable; they never publish ports themselves. No access log is written, because activation and reset tokens are part of request paths.

## Usage on the server

```bash
cd /srv/edge
cp .env.example .env && chmod 600 .env   # set ACME_EMAIL
docker compose up -d
docker compose logs -f caddy             # certificate issuance
```

A new project gets its own file in `sites/`, then `docker compose restart caddy`. DNS for the domain must point to the VPS before the first start, otherwise the certificate request fails.

## Log retention

The privacy policy promises that server logs are deleted after 14 days at the latest. `json-file` can only rotate by size, so a quiet container could keep old entries for months. All containers therefore log to journald, which has a time limit. Caddy writes no access log and no log file, so there is nothing to rotate on its side; its error log can still contain client addresses, and that ends up in the journal too.

The setting applies to the whole journal of the host, so system logs are kept for 14 days as well. Set it up before the stacks switch to journald:

```bash
sudo install -d /etc/systemd/journald.conf.d
sudo cp /srv/edge/host/journald-retention.conf /etc/systemd/journald.conf.d/retention.conf
sudo systemctl restart systemd-journald
systemd-analyze cat-config systemd/journald.conf | grep -E 'MaxFileSec|MaxRetentionSec'
```

Then recreate the containers (`docker compose up -d` in each stack); this also removes their old `json-file` logs. Check:

```bash
docker inspect edge-caddy-1 --format '{{.HostConfig.LogConfig.Type}}'   # journald
journalctl --output=short-iso | head -n 2                               # oldest entry, 14 days at most
```

## Check

The CI starts the real proxy configuration with certificates from Caddy's local CA and stand-in upstreams ([whoami](https://github.com/traefik/whoami)) that answer with their hostname. It checks on every pull request:

- Videoflix: `/api/`, `/admin/`, `/static/` reach the backend, everything else the frontend
- Status page: `/status/*`, the status page API, badges and assets reach Kuma; `/` redirects to `/status/videoflix`; dashboard, settings, socket.io, metrics, push, setup and every non-GET request return 404
- HTTP redirects to HTTPS; HSTS, `nosniff` and the referrer policy are set, the `Server` header is removed
- The network `monitoring` is internal

Locally:

```bash
docker run --rm -e ACME_EMAIL=test@example.com -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$PWD/sites:/etc/caddy/sites:ro" caddy:2.11.9-alpine caddy validate --config /etc/caddy/Caddyfile
```

## Troubleshooting

Sorted by what the monitors report. Commands run in `/srv/edge`. Project-specific cases are in the runbook of each project, e.g. [videoflix-deploy](https://github.com/PhilippKlinger/videoflix-deploy#troubleshooting).

### Proxy down

**Symptom:** every site on the VPS refuses connections or times out, the status page as well.

**Check:** `docker compose ps` shows Caddy stopped or restarting; `docker compose logs --tail 50 caddy` names the file and line of a config error.

**Fix:** the usual cause is a change to `Caddyfile` or `sites/`. Validate it with the command under [Check](#check) and correct it through a pull request. Until then restore the last working files from Git and `docker compose restart caddy`.

**Way back:** none needed once the fixed config is merged and Caddy is restarted.

### Upstream 502

**Symptom:** one site or path returns 502, the other sites work.

**Check:** the Caddy log shows `no such host` or `connection refused` for the upstream. `docker network inspect edge --format '{{range .Containers}}{{.Name}} {{end}}'` lists the containers on `edge`; `docker compose ps` in the project stack shows whether the target runs.

**Fix:** target container down → runbook of the project. Container up but unreachable → the network alias in the project's `compose.yaml` and the upstream in `sites/*.caddy` must match (`videoflix-web`, `videoflix-frontend`) and the service must join `edge`.

**Way back:** revert the change that renamed the alias or the upstream.

### Certificate

**Symptom:** TLS error in the browser, or the certificate warning of the monitors 14 days before expiry.

**Check:** `docker compose logs caddy | grep -iE 'acme|certificate|challenge'`; the domain must resolve to the VPS (`dig +short <domain>`); ports 80 and 443 must be open in the provider firewall.

**Fix:** correct DNS or firewall, then `docker compose restart caddy`; Caddy retries on its own. After a rate limit of Let's Encrypt wait instead of restarting in a loop.

**Way back:** none needed; valid certificates stay in the volume `caddy_data`.
