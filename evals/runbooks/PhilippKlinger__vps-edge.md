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
