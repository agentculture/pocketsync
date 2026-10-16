# pocketchat.culture.dev runbook

This runbook puts the pocketchat site (`pocketsync web`) on its own hostname,
behind Cloudflare Access, on the tunnel that already carries
`pocketsync.culture.dev`. It assumes
[`pocketsync-culture-dev.md`](pocketsync-culture-dev.md) is done: the tunnel
`pocketsync-culture-dev` is running, `pocketsync-serve` is up and
`pocketsync-mongo` is populated. Cloudflare steps use the operator's own
credentials; nothing here is committed with real values.

The site is a private reader and chat over the operator's own recordings. The
API contract is [`docs/web-api.md`](../web-api.md); do not repeat it here.

Shape:

```text
browser ──▶ pocketchat.culture.dev    (Cloudflare edge, TLS)
              │  Access ON for the whole host, operator email only, NO Bypass
              ▼
         cloudflared (existing unit, same remote-managed tunnel)
              ▼
         http://127.0.0.1:8788        (pocketsync web, loopback only;
                                       verifies the Access JWT on every request)
              ▼
         pocketsync-mongo (127.0.0.1:27019) + lobes gateway (localhost)
```

## Privacy model (read before exposing it)

- Every recording is searchable and readable by default. That is the
  operator's decision for an internal network. `exclude_sensitive` is opt-in
  per call and drops recordings the decider scored sensitive.
- The chat model and the embeddings run on the operator's own lobes gateway at
  `localhost`. Transcript text does not go to a third-party model API from the
  site.
- Access is the front door (operator email only, **no Bypass**, unlike the
  webhook route), and the server independently verifies the
  `Cf-Access-Jwt-Assertion` token on every request. A request that reaches
  `127.0.0.1:8788` some other way gets `403`.
- The MCP tools (`pocketsync mcp`) are a separate surface. They put the
  operator's own content into the calling model's context.

## Prerequisites

- The existing tunnel `pocketsync-culture-dev` and its `cloudflared-pocketsync`
  unit are running.
- `pocketsync` installed for the unit user with the extras the units need:
  `uv tool install --force --reinstall '.[mongo,events,mcp]'`.
- The `POCKETSYNC_GATEWAY_API_KEY` and `POCKETSYNC_MONGO_URI` grant secrets
  already exist (step 1 of the pocketsync runbook). The web unit needs only
  those two. It never touches the Pocket key or the webhook secret.
- Port `8788` is free on loopback (`ss -ltn | grep 8788` prints nothing).

## Step 1: create the Access application (no Bypass)

Do this first, so the hostname is never reachable without Access in front of
it.

In the Cloudflare Zero Trust dashboard, or through the API as for the
pocketsync host, create a self-hosted **Access application** for
`pocketchat.culture.dev` with one **Allow** policy: **Include → Emails →**
`<operator-email>`. Add no Bypass application and no Everyone policy.

Then record two values from the application:

| Value | Where it goes |
|---|---|
| Application **AUD tag** (Overview tab) | `POCKETSYNC_ACCESS_AUD` |
| Team domain, `<team>.cloudflareaccess.com` | `POCKETSYNC_ACCESS_TEAM_DOMAIN` |

Neither is a secret, but keep real values out of the repo; put them in the hand-turn
issue, not here.

## Step 2: add the public hostname to the existing tunnel

The tunnel is remote-managed, so ingress lives in Cloudflare, not in a local
file. Add a second public hostname to `pocketsync-culture-dev` (dashboard:
Networks → Tunnels → `pocketsync-culture-dev` → Public Hostnames; or
`cultureflare`):

| Hostname | Service |
|---|---|
| `pocketsync.culture.dev` | unchanged (`http://127.0.0.1:<webhook-port>`) |
| `pocketchat.culture.dev` | `http://127.0.0.1:8788` |

Make sure the proxied CNAME for `pocketchat.culture.dev` points at the tunnel.
The ingress ends with its catch-all `http_status:404` rule as before. No
change to the `cloudflared-pocketsync` unit or its token is needed.

## Step 3: install the unit

```bash
cp deploy/systemd/pocketsync-web.service $HOME/.config/systemd/user/
# fill the @NAME@ placeholders in the copy:
#   @WEB_PORT@ = 8788, @ACCESS_TEAM_DOMAIN@ and @ACCESS_AUD@ from step 1,
#   @GATEWAY_API_KEY_SECRET@ / @MONGO_URI_SECRET@ = your grant secret names
systemctl --user daemon-reload
systemctl --user enable --now pocketsync-web
```

`POCKETSYNC_ACCESS_TEAM_DOMAIN` and `POCKETSYNC_ACCESS_AUD` are set together or
not at all; a partial pair is refused. Leave `POCKETSYNC_SITE_ORIGIN` at its
default `https://pocketchat.culture.dev`: the server rejects a `POST` from any
other `Origin`. The chat model is `POCKETSYNC_CHAT_MODEL`, a lane on the lobes
gateway; add an `Environment=` line to the unit to change it.

## Step 4: verify

```bash
ss -ltn | grep 8788                          # 127.0.0.1:8788 only, never 0.0.0.0
curl -sI https://pocketchat.culture.dev/     # 302 to <team>.cloudflareaccess.com
curl -sI https://pocketsync.culture.dev/     # still 302, unchanged
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://pocketsync.culture.dev/hooks/pocket \
  -H 'Content-Type: application/json' -d '{}'   # still 401: webhook route unchanged
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/api/health  # 403: no Access JWT
journalctl --user -u pocketsync-web -n 20 --no-pager
```

Then open `https://pocketchat.culture.dev/` in a browser signed in as the
operator: the page loads, a search returns recordings and a chat answer cites
them. The unauthenticated `GET` must be `302`, never `200`; if it is `200`, the
Access application does not cover the host, so stop the unit and fix step 1.

## Upgrades

After upgrading, reinstall and restart. `--reinstall` is mandatory: uv caches a
local build by version and `pyproject.toml`, so `--force` alone silently
reinstalls the old code when the version has not changed.

```bash
uv tool install --force --reinstall '.[mongo,events,mcp]'
systemctl --user restart pocketsync-serve pocketsync-web
```

Restarting `pocketsync-serve` updates the vector indexes' filter fields in
place (the index manager reconciles them at startup), which the search
filters rely on.

## Rollback

- `systemctl --user disable --now pocketsync-web`
- Remove the `pocketchat.culture.dev` public hostname from the tunnel and
  delete its DNS record (dry-run first if you use `cultureflare`).
- Delete the Access application for `pocketchat.culture.dev`.

`pocketsync.culture.dev`, the webhook route, `pocketsync-serve` and the data in
`pocketsync-mongo` are untouched by rollback.
