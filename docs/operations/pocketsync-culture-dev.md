# pocketsync.culture.dev runbook

This runbook takes a serving host (spark) from nothing to Pocket webhooks
landing in `pocketsync-mongo`. It mirrors culture-rules'
`docs/operations/rules-culture-dev.md`. Cloudflare and Pocket steps use the
operator's own credentials; nothing here is committed with real values.

Shape:

```text
Pocket ──POST──▶ pocketsync.culture.dev/hooks/pocket   (Cloudflare edge, TLS)
                   │  Access ON for the host; exact-path Bypass for /hooks/pocket
                   ▼
         cloudflared (systemd user unit, outbound tunnel)
                   ▼
         http://127.0.0.1:<port>/hooks/pocket   (pocketsync serve, loopback only)
                   ▼
         pocketsync-mongo (127.0.0.1:27019)  +  $HOME/.local/share/pocketsync/files
```

## Prerequisites

- `pocketsync` installed for the unit user with the extras:
  `uv tool install 'pocketsync[mongo,events]'` (or from a checkout).
- `pocketsync-mongo` running per `docs/operations/pocketsync-mongo.md`.
- `grant` and `cloudflared` installed (`$HOME/.local/bin/grant`,
  `$HOME/.local/bin/cloudflared`).
- `cultureflare` with a Cloudflare API token that has Tunnel Edit plus Access
  Apps and Policies Edit (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` in
  its `.env`, never in this repo).
- Pick a free loopback port for the receiver, for example `8787`
  (`ss -ltn | grep 8787` must print nothing).

## Step 1: seal the pocketsync secrets in grant

Store each value as a **hidden** grant secret (names are examples):

| grant secret | becomes env var | source |
|---|---|---|
| `POCKETSYNC_POCKET_API_KEY` | `POCKETSYNC_API_KEY` | Pocket app → Settings → Developer → API Keys (`pk_…`) |
| `POCKETSYNC_WEBHOOK_SECRET` | `POCKETSYNC_WEBHOOK_SECRET` | shown once when the Pocket webhook is created (step 5) |
| `POCKETSYNC_GATEWAY_API_KEY` | `POCKETSYNC_GATEWAY_API_KEY` | lobes gateway key (`GATEWAY_API_KEY` in `$HOME/.lobes/.env`) |
| `POCKETSYNC_MONGO_URI` | `POCKETSYNC_MONGO_URI` | `mongodb://<user>:<password>@127.0.0.1:27019/?directConnection=true&authSource=admin` |

Then verify the key without starting anything:

```bash
grant run --inject POCKETSYNC_API_KEY=POCKETSYNC_POCKET_API_KEY -- pocketsync auth --json
```

## Step 2: provision the tunnel and Access (dry-run first)

```bash
cultureflare remote-login setup \
  --hostname pocketsync.culture.dev \
  --service http://127.0.0.1:<port> \
  --allow <operator-email>
```

Read the plan, then re-run with `--apply`. One idempotent run creates the named
tunnel `pocketsync-culture-dev` with remote-managed ingress to the loopback
port, the proxied CNAME, the Access application and an allow-by-email policy.
**Do not** use `--no-access`: Access stays on for the whole host.

Record the resulting ids on the hand-turn issue (not here):

| Item | Value |
|---|---|
| tunnel name | `pocketsync-culture-dev` |
| tunnel id | `<tunnel-id>` |
| Access app id | `<access-app-id>` |
| Bypass app id (step 3) | `<bypass-app-id>` |
| applied on (date, host) | `<yyyy-mm-dd, host>` |

## Step 3: the exact-path Access Bypass for `/hooks/pocket`

Pocket cannot sign in through Access, so the webhook path needs its own
**Bypass** application (**Include: Everyone**). Access policies carry no path,
so the Bypass is a separate self-hosted Access application whose domain is the
exact path `pocketsync.culture.dev/hooks/pocket`; the more specific path wins
over the host application. Create it through the Cloudflare API (as
culture-rules did for `/hooks/github`). **Never widen it** to `/hooks/*` or `/`.

Bypass does not mean open: the receiver verifies every delivery's
`X-HeyPocket-Signature` (HMAC-SHA256 over `{timestamp}.{rawBody}`), rejects
stale or unsigned requests with `401`, and refuses bodies over 5 MB with `413`.

## Step 4: seal the tunnel token and start both units

```bash
# token from GET .../cfd_tunnel/<tunnel-id>/token, sealed hidden in grant
cp deploy/cloudflared/cloudflared-pocketsync.service $HOME/.config/systemd/user/
cp deploy/systemd/pocketsync-serve.service $HOME/.config/systemd/user/
# fill the @NAME@ placeholders in both copies (grant secret names, port)
systemctl --user daemon-reload
systemctl --user enable --now pocketsync-serve cloudflared-pocketsync
loginctl enable-linger "$USER"
```

## Step 5: create the Pocket personal webhook

In the Pocket app's integrations UI, add a personal webhook:

- URL: `https://pocketsync.culture.dev/hooks/pocket`
- events: at least `summary.completed`, `transcription.completed`,
  `summary.updated`, `summary.regenerated`, `action_items.updated`,
  `transcript.edited`, `speakers.labeled`, `recording.deleted`,
  `recording.merged`
- copy the signing secret **immediately** (shown once) into the
  `POCKETSYNC_WEBHOOK_SECRET` grant secret, then
  `systemctl --user restart pocketsync-serve`.

## Step 6: verify

```bash
ss -ltn | grep <port>                       # 127.0.0.1:<port> only, never 0.0.0.0
curl -sI https://pocketsync.culture.dev/    # 302 to <team>.cloudflareaccess.com
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://pocketsync.culture.dev/hooks/pocket \
  -H 'Content-Type: application/json' -d '{}'   # 401: reached the receiver, unsigned
pocketsync status --json                    # last_webhook_at updates after a Pocket test delivery
pocketsync doctor --json                    # gateway, mongo auth, index/model, webhook freshness
```

Then send a test delivery from the Pocket UI (or record a short private test
clip) and confirm `last_webhook_at` moves and the recording appears in
`pocketsync-mongo`. If the unsigned `POST` returns a challenge page instead of
`401`, a zone WAF or Bot Fight rule is intercepting (plan risk r5): add a skip
rule scoped to that exact path.

## Rollback

- `systemctl --user disable --now pocketsync-serve cloudflared-pocketsync`
- Remove the Pocket webhook in the app.
- `cultureflare remote-login teardown --hostname pocketsync.culture.dev`
  (dry-run first) and delete the Bypass application.

Data in `pocketsync-mongo` and the files root is untouched by rollback.
