# pocket to mongodb near-realtime

> pocketsync keeps a local MongoDB continuously up to date with every Pocket recording's transcript, summary, action items and metadata, landing new recordings as close to realtime as Pocket's API limits allow
> instruction: end-to-end check: new test recording -> phone sync -> doc present in pocketsync-mongo

## Audience

- The operator on spark who wants every Pocket recording searchable in a store they own, plus the mesh agents and culture-rules workflows that read pocketsync-mongo and react to pocketsync's events
  - instruction: check README names both readers: the operator and downstream consumers (culture-rules, mesh agents)

## Before → After

- Before: Pocket transcripts, summaries, action items and audio live only in Pocket's cloud; pocketsync is a scaffold with no Pocket, sink or sync code, and its docs assume a reverse-engineered Firebase-token API
  - instruction: confirm against git log / CLAUDE.md status section at the start of the work
- After: Within seconds of Pocket finishing a recording, pocketsync-mongo holds one document for it (metadata, speaker-attributed transcript segments, summary, action items, audio file path and hash, 768-d gemma2-embed chunk + summary vectors, decider type labels), queryable with $vectorSearch plus type filters, and a handoff event is emitted; data survives container restarts and re-creation
  - instruction: end-to-end demo: record a synthetic/private test clip, sync the phone, then query pocketsync-mongo for the doc, its vectors and labels

## Why it matters

- An owned, near-realtime, semantically searchable copy of every recording (filterable by type) makes Pocket data usable by local agents and rules within seconds, and survives Pocket API changes or account loss
  - instruction: n/a - motivation; verified indirectly by the success signals

## Requirements

- MongoDB is a third sink adapter (mongo) beside local and s3, behind the same sink interface planned in CLAUDE.md 'Target architecture' and issue #1 item 2 ('behind one interface so more can be added later') — not a parallel pipeline
  - instruction: tests/`test_sinks.py`: shared sink contract test parametrized over local and mongo (fake)
  - honesty: the mongo sink implements the same sink interface as local, with no Mongo-specific calls outside the sink module
- pymongo goes behind an optional extra (pocketsync\[mongo\]) and is imported lazily; core and local sink stay zero-dependency; tests use a fake/in-memory Mongo (mongomock or stub) in the dev group
  - instruction: test that imports pocketsync.cli with pymongo blocked via sys.modules; pyproject has \[project.optional-dependencies\] mongo
  - honesty: import pocketsync and the local sink work with pymongo uninstalled
- Mongo writes are idempotent upserts keyed by recording id with content hash; re-sync only on hash change, so a re-run with no remote changes is a no-op (issue #1 item 3)
  - instruction: test: upsert same fake recording twice; assert 1 doc, unchanged hash and `updated_at`
  - honesty: re-delivering an identical recording leaves the doc's content hash and `updated_at` unchanged
- Each new/updated recording also emits the handoff event (events.jsonl and/or an events-cli publish); culture-rules triggers on a durable events-cli subscription (`culture_rules`/events/ingest.py), so near-realtime downstream reaction needs the events-cli publish, not only the jsonl file
  - instruction: test: events emitted per new/changed recording; schema documented in README
  - honesty: every new or changed recording yields exactly one event with id, sink locations, content hash and created/updated
- The CLAUDE.md 'Verify the API premise first' section (and the three sibling harness files AGENTS.override.md/.pi/SYSTEM.md, AGENTS.colleague.md, QWEN.md) is updated: the Firebase/reverse-engineered premise is superseded by the official API + webhooks, which answers issue #1's first deliverable
  - instruction: uv run python scripts/harness-smoke.py --stage config; grep the five files for 'webhook'
  - honesty: CLAUDE.md, AGENTS.override.md, .pi/SYSTEM.md, AGENTS.colleague.md and QWEN.md all describe the official API + webhooks, and harness probes still pass
- Near-realtime path is push: a pocketsync webhook receiver accepts Pocket POSTs, verifies X-HeyPocket-Signature (HMAC-SHA256 hex of '{timestamp}.{rawBody}' with the webhook secret) against raw body bytes, rejects stale timestamps, dedupes at-least-once deliveries, and upserts the recording into Mongo
  - instruction: tests: valid signature (ms and s timestamps, raw hex and sha256= forms) accepted; bad/stale rejected
  - honesty: a request with a bad or missing signature, or a timestamp older than the tolerance, is rejected with 401 and writes nothing
- A polling reconciler (the planned 'watch' verb) backs up the webhooks: list recordings with `start_date` overlap (e.g. 2 days), compare `updated_at`/content hash, fetch details for changes, and back off on HTTP 429 — so missed webhooks (receiver down beyond Pocket's 3 attempts) still converge
  - instruction: test: fake adapter with a recording the receiver never saw; one reconcile pass lands it
  - honesty: a webhook dropped while the receiver is down is recovered by the next reconcile pass
- pocketsync.culture.dev is provisioned with `cultureflare remote-login setup` as a remote-managed Cloudflare tunnel to a loopback-only pocketsync receiver (`http://127.0.0.1:<port>`, never 0.0.0.0), with cloudflared run as a systemd user unit whose tunnel token is injected from grant, mirroring culture-rules cloudflared-rules.service
  - instruction: cultureflare remote-login show --hostname pocketsync.culture.dev; ss -ltn shows the receiver on 127.0.0.1 only
  - honesty: the receiver binds 127.0.0.1 only and the tunnel ingress points at that port
- Cloudflare Access stays on pocketsync.culture.dev; only the exact path pocketsync.culture.dev/hooks/pocket gets a Bypass (Include: Everyone) Access app, never widened to /hooks/\* or /, and the receiver's HMAC signature check is the auth for that path (decision c34)
  - instruction: curl -sI <https://pocketsync.culture.dev/> -> 302 to cloudflareaccess; Pocket test delivery -> 2xx
  - honesty: an unauthenticated GET to pocketsync.culture.dev/ hits the Access login, while a signed POST to /hooks/pocket reaches the receiver
- Embeddings are requested from spark's lobes gateway (POST <http://localhost:8001/v1/embeddings>, model 'gemma2-embed', OpenAI-compatible, Authorization: Bearer key from env), which mesh-proxies to the Orin; vectors are 768-d L2-normalised and the model id is stored on the doc so a future model switch is a detectable re-embed
  - instruction: test with fake gateway: request body has model gemma2-embed; doc stores 768 floats
  - honesty: the stored vector length is 768 and the doc records model 'gemma2-embed'
- Transcripts longer than gemma2-embed's 8192-token window are chunked (e.g. by transcript segments) and stored as per-chunk vectors with segment time ranges, alongside one summary-level vector, so long meetings stay searchable
  - instruction: test: long synthetic transcript chunked by segments under the token budget
  - honesty: a synthetic transcript longer than 8192 tokens yields multiple chunk vectors with segment time ranges
- Enrichment (embedding + classification) never blocks or fails the Pocket landing: the doc is upserted first with enrichment status 'pending', enrichment runs after and retries, and an Orin/gateway outage (503/404 from a stopped lane — no cross-lane fallback) leaves docs pending, not lost
  - instruction: test: fake gateway 503 -> doc landed, enrichment.status == pending; then 200 -> done
  - honesty: with the gateway returning 503, the recording still lands and enrichment status stays pending until it succeeds
- Audio lands via GET audio-download-url (pre-signed URL, `expires_in` 60-86400s) into the local file layout `<root>/<YYYY>/<MM>/<recording-id>/audio.<ext>` with an atomic temp-file + rename; the Mongo doc stores only the relative path, size, content hash and download time, never the bytes. The webhook carries no audio, so audio is fetched after the receiver or poller sees the recording
  - instruction: test: fake audio URL; atomic write; doc audio.path/sha256/size set
  - honesty: the audio file's hash on disk matches the hash in the doc, and a partial download never leaves a final-named file
- pocketsync-mongo (docker compose, mongodb/mongodb-atlas-local image) bind-mounts /data/db, /data/configdb and the mongot index directory (v9) to host dirs OUTSIDE the public repo tree (e.g. ~/.local/share/pocketsync/mongo/), restart: unless-stopped, and publishes only 127.0.0.1:27019 (27017/27018 are taken); the audio/file root likewise lives outside the repo (e.g. ~/.local/share/pocketsync/files)
  - instruction: docker compose config shows the bind mounts and 127.0.0.1:27019; re-create keeps data
  - honesty: deleting and re-creating the container keeps all data because it lives in the host bind-mount dirs
- A long-running `pocketsync serve` verb runs the webhook receiver (loopback HTTP, stdlib http.server or similar, zero core deps), the reconciler loop and the enrichment worker in one process, installed as a systemd user unit; `watch` is the reconciler-only loop and `sync --apply` a one-shot pass
  - instruction: check verbs exist with --json, appear in explain catalog, learn and overview `_VERBS`; teken cli doctor . --strict passes
  - honesty: one systemd user unit runs serve and restarts it on failure
- Sync state (reconcile watermark, webhook dedupe keys, last run, errors) persists in a pocketsync-mongo collection separate from the recordings collection, so a restart resumes without re-processing and 'pocketsync status --json' reports it
  - instruction: test: kill and restart serve with fake adapters; assert no duplicate writes and status shows last run
  - honesty: restarting serve mid-run causes no duplicate doc writes or events
- The decider classification questions (label taxonomy, descriptions, single/multi-label, extra yes/no filters) are read from config, not hard-coded, so the taxonomy (open question q8) can change without a code change; a taxonomy change marks docs for re-classification
  - instruction: test: two taxonomies in config yield different decider request bodies; changing it flips docs to classification pending
  - honesty: changing the taxonomy in config requires no code change
- Tests run without network: a fake Pocket adapter (synthetic fixtures only, no URL keys or localhost-only per scan-secrets), a fake Mongo/stub for unit tests, a fake gateway for embed/decider; one opt-in integration test (env-gated) runs against a real atlas-local container to exercise $vectorSearch
  - instruction: uv run pytest -n auto passes offline; `POCKETSYNC_IT_MONGO`=1 enables the integration test; python3 scripts/scan-secrets.py passes
  - honesty: the default test run makes no network calls
- Every new verb follows the repo CLI contract: write verbs dry-run by default and commit only with --apply, --json on every verb, CliError with remediation, results on stdout and diagnostics on stderr
  - instruction: uv run teken cli doctor . --strict; tests assert dry-run performs 0 writes
  - honesty: a dry-run of each write verb performs 0 writes
- Content writes (receiver/reconciler) and enrichment writes (worker) touch disjoint field groups via targeted $set, and an enrichment result is written only if the doc's content hash still equals the hash it was computed from (compare-and-set); a content change resets enrichment to pending, so a slow embed never overwrites newer content or leaves stale vectors
  - instruction: test: interleave fake content update between embed start and finish; assert stale enrichment is discarded and re-queued
  - honesty: an enrichment computed for an old content hash is never persisted
- The webhook receiver verifies the signature, durably records the event (dedupe key) and returns 2xx in <= 2 s; fetching details, audio and enrichment happen asynchronously after the ack, so Pocket's 30 s timeout and 3-attempt retry never fire because pocketsync is slow
  - instruction: test: fake slow gateway/pocket; assert handler returns 2xx before processing; measure ack latency
  - honesty: the HTTP response is sent before any outbound call to Pocket, the gateway or the disk audio write
- A webhook is treated as a trigger, not the record: after ack, pocketsync fetches GET /public/recordings/{id} (`include_transcript`, `include_summarizations`) and builds the doc from that canonical response, because the webhook transcript can be excluded by configuration and staged events carry partial state
  - instruction: test: webhook with transcript omitted still yields a doc with transcript from the fake details endpoint
  - honesty: the stored doc is identical whether the recording arrived via webhook or via the reconciler
- Secrets inventory is complete and all are env/keyring/git-ignored only: `POCKETSYNC_API_KEY` (Pocket `pk_`), `POCKETSYNC_WEBHOOK_SECRET`, the lobes gateway key (`GATEWAY_API_KEY`-equivalent, e.g. `POCKETSYNC_GATEWAY_API_KEY`), and the pocketsync-mongo root credentials (`MONGODB_INITDB_ROOT_USERNAME`/PASSWORD for atlas-local, auth enabled even on loopback)
  - instruction: README env table lists all four; docker compose reads Mongo creds from a git-ignored .env; scan-secrets passes
  - honesty: pocketsync-mongo rejects unauthenticated connections
- pocketsync status --json reports webhook-path health: last verified webhook time, count of recordings the reconciler found that no webhook delivered (a non-zero rate means the push path is broken: tunnel down, Access bypass gone, secret rotated), enrichment backlog and last errors; doctor flags each as a check
  - instruction: test: reconciler-found-missing counter increments with fake adapter; status --json exposes all fields
  - honesty: a broken webhook path becomes visible in status within one reconcile interval, not only as higher latency
- Handoff events are published with `events_cli` EventClient.`publish_event` at QoS 1 (QoS 0 bypasses durable capture) behind an optional extra (pocketsync\[events\]), once per content hash after enrichment completes (decision c59); an append-only events.jsonl in the file root doubles as an outbox so a broker-down publish is retried, never lost
  - instruction: test with fake EventClient: broker down -> event stays in outbox and is published on recovery; qos=1 asserted
  - honesty: every landed or updated recording eventually produces exactly one published event per content hash, even across broker outages
- First run backfills history: `pocketsync sync --since <date> --apply` pages list recordings (limit 100), honours 429 with backoff, and throttles enrichment to a configurable concurrency so a backlog of hundreds of recordings doesn't saturate the Orin lanes shared with the mesh
  - instruction: test: fake adapter with 250 recordings across 3 pages + one 429; all land; enrichment concurrency never exceeds config
  - honesty: the backfill completes without manual restarts and respects the concurrency cap
- pocketsync creates the vector search index idempotently (numDimensions from the configured model, similarity cosine or dotProduct, filter fields for type labels and dates) and refuses to run if the configured model/dimensions differ from the index and stored docs, until an explicit 're-embed --apply' rebuilds them
  - instruction: test: config dims 768 vs index 512 -> CliError with remediation; re-embed dry-run lists affected docs
  - honesty: vectors from two different models or dimensions never coexist in one index

## Honesty conditions

- a recording made after go-live appears in pocketsync-mongo without any manual step
- no tracked file contains a mongodb:// URI with credentials or a non-localhost host
- no code path calls production.heypocketai.com or reads browser tokens
- the API key and webhook secret are read only from env/keyring/git-ignored config, and scan-secrets passes
- both readers can read the doc schema from README without reading pocketsync source
- no Pocket/sink/sync code exists on main before this work
- the operator confirms an owned searchable copy is the goal, not a Pocket UI replacement
- every field listed is present on a real synced recording doc
- latencies are measured from real deliveries, not estimated
- the 0-writes check runs in CI against the fake adapters
- the restart test re-creates the container, not just restarts it
- no code path stores or processes a webhook body whose signature was not verified
- the Pocket adapter exposes no write methods

## Success signals

- p95 latency from Pocket's summary.completed webhook delivery to the recording document being present in pocketsync-mongo is <= 10 s, and to vectors + type labels being present is <= 60 s while the Orin lanes are up
  - instruction: measure receipt time vs doc `landed_at`/`enriched_at` over >= 10 real deliveries; report p95
- With webhooks disabled, the reconciler lands a new recording within <= 1 polling interval (default 5 min) and a re-run with no remote changes performs 0 Mongo writes and emits 0 events
  - instruction: test: fake Pocket adapter + fake Mongo; run reconcile twice and assert write/event counters == 0 on the 2nd run
- After docker compose down && up (container re-created) the recording count, vectors and vector-search index are unchanged and the first $vectorSearch returns results with 0 re-embeds
  - instruction: manual/integration: count docs + run a fixed query before and after re-create; compare

## Scope / boundaries

- Mongo connection URI comes from env/git-ignored config only; no mongodb:// URI with credentials or non-localhost host is ever committed — scan-secrets.py flags secret-ish assignments, and JSON endpoint check covers only http(s) url/host keys, so a mongodb:// URI would NOT be caught by the endpoint check and needs discipline (or a scanner extension)
  - instruction: python3 scripts/scan-secrets.py; consider extending it to flag non-localhost mongodb:// URIs
- pocketsync does NOT use the reverse-engineered production.heypocketai.com API or scraped Firebase browser tokens — Pocket's ToS (heypocket.com/pages/terms, 2025-09-15) bars automated access 'for unauthorized purposes', while the official `pk_` key is authorized by design
  - instruction: grep -r 'production.heypocketai' pocketsync/ returns nothing
- The Pocket `pk_` API key and webhook signing secret live only in env vars / keyring / git-ignored config (e.g. `POCKETSYNC_API_KEY`, `POCKETSYNC_WEBHOOK_SECRET`), never in tracked files; scan-secrets.py's token/secret assignment rule is the CI backstop
  - instruction: scan-secrets passes; README documents `POCKETSYNC_API_KEY` / `POCKETSYNC_WEBHOOK_SECRET` env vars
- The /hooks/pocket receiver fails closed: unsigned requests (Pocket's legacy unsigned webhooks, possibly org webhooks) are rejected, oversized bodies (> a configured limit, e.g. 5 MB) are refused before HMAC, and only personal-webhook signatures with a configured secret are accepted
  - instruction: tests: unsigned -> 401; oversize -> 413 without reading the full body; valid -> 2xx
- pocketsync only reads from Pocket: it never calls mutating Pocket endpoints (update action items, uploads, org admin), even though the `pk_` key may permit them
  - instruction: grep the Pocket adapter: only GET list/details/audio-url calls exist

## Non-goals

- pocketsync does not route Pocket data into other systems (Notion, mesh, eidetic, email); it lands data in stores it owns (pocketsync-mongo, local files) and enriches only its own landed docs (c33); fan-out stays culture-rules' job (issue #1 non-goals)
- This frame does not deliver the S3 sink or the example culture-rules rule from issue #1; it builds the Pocket adapter, the local file layout (needed for audio) and the Mongo sink, leaving S3 for issue #1's remaining work
  - instruction: n/a - scope note

## Assumptions

- Pocket exposes an OFFICIAL public API at public.heypocketai.com/api/v1/public/\* authenticated with a static 'Authorization: Bearer `pk_`...' API key (Settings -> Developer -> API Keys), so the Pocket source adapter uses that key and sync runs unattended with no hourly token refresh
- End-to-end latency floor is set by the device, not the API: Pocket records offline and uploads when the phone app syncs; cloud transcript+summary then appear in about a minute (single reviewer report), and webhooks fire per stage (transcription.completed before summary.completed) — so the Mongo doc may land in stages

## Scope exploration

- `s1` — `CLAUDE.md target architecture + issue #1 item 2`: Brief plans local+s3 sinks behind one interface explicitly extensible; MongoDB fits as a new sink (landing zone we own), not as downstream fan-out \[seeded c2; --seeds link lost to a capture-id parsing slip\]
- `s2` — `pyproject.toml`: dependencies = \[\] today; CLAUDE.md mandates third-party clients (boto3 example) behind optional extras with lazy import and test libs in the dev group \[seeded c3; --seeds link lost to a capture-id parsing slip\]
- `s3` — `issue #1 item 3 (incremental, idempotent sync)`: Brief requires persisted cursor, no-op re-runs and content-hash change detection; for Mongo that maps to upsert-by-recording-id, and the 'meta.json last' atomicity rule maps to writing the document in one upsert \[seeded c4; --seeds link lost to a capture-id parsing slip\]
- `s4` — `scripts/scan-secrets.py`: Endpoint check (lines 176-229) only inspects http(s):// values under url/host/endpoint/baseUrl keys in JSON; password/token assignments are flagged (line 70). A mongodb:// URI is outside the endpoint check \[seeded c5; --seeds link lost to a capture-id parsing slip\]
- `s5` — `issue #1 non-goals + culture-rules README`: Brief forbids downstream routing; culture-rules (rules->conditions->workflows->actions) owns fan-out. Mongo-as-sink is in-lane only if it's the store we own, not a destination feed \[seeded c6; --seeds link lost to a capture-id parsing slip\]
- `s6` — `culture-rules culture_rules/events/ingest.py`: culture-rules drains a durable events-cli subscription into its own Mongo 'events' collection keyed by envelope id; it does not tail files, so events.jsonl alone won't trigger rules in realtime \[seeded c7; --seeds link lost to a capture-id parsing slip\]
- `s7` — `spark host (docker ps, ss -ltn :27017)`: Four mongo:8.0 containers run: qq-mongodb publishes 0.0.0.0:27017, eidetic-mongo 27018, culture-rules-mongod and weather-mongodb unpublished; mongod systemd unit inactive. Target instance is a user decision (raised as a devague question)
- `s8` — `docs.heypocketai.com/docs/api/recordings/list-recordings`: Verified: GET /api/v1/public/recordings, Bearer `pk_xxx` auth; params `start_date`/`end_date` (YYYY-MM-DD UTC, day-granular), `tag_ids`, page, limit<=100; page-based pagination with `has_more`; no cursor or `updated_since` documented
  - seeds: `c9`
- `s9` — `CLAUDE.md 'Verify the API premise first' + issue #1 'Verify first'`: Both state no official API exists and assume a Firebase ID token (~1h expiry); the official docs contradict this, so the premise and the first #1 deliverable (access report) change
  - seeds: `c10`
- `s10` — `docs.heypocketai.com/docs/api/webhooks`: Verified: events transcription.completed, summary.completed/regenerated/updated, `action_items`.regenerated/updated, speakers.labeled, transcript.edited, recording.created/deleted/merged, translation.completed, `mind_map`.completed; POST JSON, 30s timeout, 3 attempts with 1-30s backoff, at-least-once; HMAC-SHA256 signature + ms timestamp headers; payload carries recording meta, summarizations (summary, action items) and transcript segments with speakers+times, no audio; personal webhooks set in app integrations UI, secret shown once
  - seeds: `c11`
- `s11` — `list-recordings 429 response + webhook retry policy`: Webhook retries end after 3 attempts over ~30s, so downtime loses events; list has a documented 429 with no numbers, so polling must back off and stay coarse
  - seeds: `c12`
- `s12` — `heypocket.com/pages/terms + github.com/tmustier/heypocket-reader`: Community reader uses production.heypocketai.com with a Chrome-extracted Firebase token (1h); ToS prohibits automated access for unauthorized purposes and device reverse engineering (quotes via summarizing fetch, verify before relying). With an official API this path is unnecessary risk
  - seeds: `c13`
- `s13` — `Pocket processing latency (heypocket.com/pages/pocket-summary, tomross.substack.com/p/pocket-review)`: No SLA; marketing says 'seconds', one review ~1 min after sync; clock starts at phone sync. Low confidence — measure empirically via recordingAt vs createdAt
  - seeds: `c14`
- `s14` — `pk_ key + webhook secret handling (CLAUDE.md 'Constraints that bite', scan-secrets.py line 70)`: Repo is public; credentials must be env/keyring/git-ignored; scanner flags token/secret/`api_key` assignments in tracked files
  - seeds: `c15`
- `s15` — `cultureflare README.md:50-68 + _remote_login/_common.py:29 + culture-rules deploy/cloudflared/cloudflared-rules.service`: 'cultureflare remote-login setup --hostname H --service <http://127.0.0.1:P> \[--no-access\] --apply' provisions tunnel (name pocketsync-culture-dev), ingress, proxied CNAME; TLS ends at the Cloudflare edge; rules.culture.dev runs cloudflared as a user unit with `TUNNEL_TOKEN` from grant
  - seeds: `c21`
- `s16` — `culture-rules docs/operations/rules-culture-dev.md:204-231 (Webhook receivers and the Access Bypass paths)`: Access is applied by default by remote-login setup and 302s unauthenticated POSTs to SSO; culture-rules precedent: one Bypass Access app per exact path (/hooks/github, /hooks/jira) created via Cloudflare API, app authenticates every delivery itself
  - seeds: `c22`
- `s17` — `lobes-cli lobes/embed_lanes.py:101-109 + docs/embeddinggemma-2.md + live GET localhost:8001/capabilities`: gemma2-embed = google/embeddinggemma-2, 768-d, MRL 128-768, L2-norm, 8192-token window, Sentence-Transformers sidecar; spark capabilities show it ready, member orin, proxied; unauthenticated POST /v1/embeddings -> 401 `invalid_api_key` (key in `GATEWAY_API_KEY`, not read)
  - seeds: `c24`
- `s18` — `docs/embeddinggemma-2.md:30 (native_max_model_len=8192)`: Declared window 8192 tokens; over-length behaviour (truncate vs 400) and batch cap not documented — pocketsync must chunk rather than rely on it
  - seeds: `c25`
- `s19` — `lobes-cli docs/orin-embed-deployment.md:180-187 (no cross-lane fallback) + hard question q1 on c20`: A stopped lane returns 503/404, never another model's vector; so enrichment must be a retryable stage after the landing
  - seeds: `c26`
- `s20` — `lobes-cli-orin-deciders docs/decider.md:10-11,140-142 + spark /capabilities`: decider does classification/triage via caller-defined choice questions with probabilities; render DECLARED/UNVALIDATED (#108); only on branch scope/orin-decision-models; spark /capabilities has no decider key. No existing type taxonomy anywhere — raised as devague question q7
- `s21` — `eidetic-cli eidetic/memory/scoring.py + live $vectorSearch probe`: No project stores vectors in Mongo; eidetic does client-side cosine. $vectorSearch on mongo:8.0 community -> SearchNotEnabled. Vector engine choice raised as devague question q6
- `s22` — `docs.heypocketai.com/docs/api/recordings/get-audio-download-url + webhooks payload`: Audio is a separate pre-signed S3 URL (`expires_in` default 3600); webhook payload has no audio. Operator states free tier includes audio download, resolving v2
  - seeds: `c27`, `c28`
- `s23` — `docker inspect eidetic-mongo / culture-rules-mongod / qq-mongodb`: All three: restart=unless-stopped, bind mount host dir -> /data/db; /data/configdb left as an anonymous docker volume (lost on re-create). culture-rules keeps data in ~/.local/share/culture-rules/mongo-data, outside any repo; host ports 27017 (qq) and 27018 (eidetic) are taken
  - seeds: `c29`, `c30`
- `s24` — `challenge pass / concurrency lens: c42 serve (receiver + reconciler + worker) + webhooks doc staged events`: Three writers update one recording doc; transcription.completed and summary.completed arrive as separate, possibly near-simultaneous or reordered deliveries; nothing in the spec ordered or guarded the writes
  - seeds: `c48`
- `s25` — `challenge pass / failure-mode lens: docs.heypocketai.com/docs/api/webhooks (30 s timeout, 3 attempts, 1-30 s backoff)`: A synchronous handler that embeds or downloads audio inside the request risks timeouts -> retries -> duplicate work, and after 3 failures the event is dropped
  - seeds: `c49`
- `s26` — `challenge pass / data-flow lens: webhooks doc payload ('transcript … can be excluded by the webhook configuration') vs get-recording-details`: Building from the payload would make webhook-landed and poll-landed docs differ, and could store a doc without a transcript
  - seeds: `c50`
- `s27` — `challenge pass / lifecycle lens: webhooks doc events recording.deleted, recording.merged`: The spec handles create/update only; delete and merge semantics were never decided and touch data loss — raised as a devague question
- `s28` — `challenge pass / security lens: webhooks doc (unsigned legacy webhooks, org signing undocumented), atlas-local docs (MONGODB_INITDB_ROOT_* env), lobes gateway auth (401 invalid_api_key)`: The public path needs fail-closed + size limits; c15 listed only two secrets but four exist; the `pk_` key's write scope is unneeded
  - seeds: `c51`, `c52`, `c53`
- `s29` — `challenge pass / observability lens: c12 reconciler + c21/c22 tunnel and Access bypass`: If the tunnel or bypass breaks, the reconciler silently masks it at 5-min latency; nothing in the spec surfaced the degradation
  - seeds: `c54`
- `s30` — `challenge pass / adjacent-systems lens: events-cli CHANGELOG.md:12,16,23 + culture-rules events_cli_adapter.py`: Durable subscriptions ship (README 'not built yet' is stale); `publish_event` defaults to QoS 1 and QoS 0 silently bypasses durable capture; culture-rules drains durable subs per type depth
  - seeds: `c55`
- `s31` — `challenge pass / operations lens: list-recordings pagination (limit <= 100, 429), lobes gateway shared lanes`: Spec covered steady-state (2-day overlap) only; the first sync of all history and its load on shared Orin lanes were unaddressed
  - seeds: `c56`
- `s32` — `challenge pass / migration lens: lobes docs/orin-embed-deployment.md:180-187 (spaces not comparable) + c24 stores model id`: A model or MRL-dimension change silently mixing vector spaces would corrupt search; the spec stored the model id but defined no guard or rebuild path
  - seeds: `c57`
- `s33` — `challenge pass / recovery lens: c30 bind mounts`: Persistence across restarts is covered; disk loss or corruption is not — parked as a follow-up
- `s34` — `challenge pass / probe: mongodb.com/docs/atlas/cli/current/atlas-cli-deploy-docker`: Fetched: volumes data:/data/db, config:/data/configdb; auth via `MONGODB_INITDB_ROOT_USERNAME`/PASSWORD; no mention of mongot data path or index persistence
- `s35` — `challenge pass / hardware lens: Orin (lobes gateway, mesh-proxied)`: Covered via c26 (enrichment never blocks landing, retries while lanes are down); no further hardware-specific gap found. Residual: Orin capacity under backfill (see c56)
- `s36` — `challenge pass / destructive-ops lens: delete/merge, re-embed, docker re-create`: Destructive paths are delete/merge (raised as question), re-embed (guarded by explicit --apply), and container re-create (c41). No other destructive operation found

## Decisions

- pocketsync writes to its own dedicated Docker container, pocketsync-mongo, not a database inside qq-mongodb, eidetic-mongo, culture-rules-mongod or weather-mongodb (image per c31)
- Webhooks are the primary realtime path; Pocket POSTs to <https://pocketsync.culture.dev>, opened through the existing culture.dev ingress, with polling as the reconciliation fallback
- MongoDB is an extra sink beside local and S3; it replaces neither those sinks nor any other Mongo on spark
- One Mongo document per recording (metadata, transcript segments, summary, action items embedded), indexed with orin-embed vectors and carrying filter fields such as type labels from the classifier model on Orin
- Embeddings for each recording document come from the gemma2-embed model served on Orin (the orin-embed lane)
- Audio IS in scope: per the operator, Pocket's free tier includes audio download and text export. pocketsync downloads each recording's audio and saves it as a file on disk (not in Mongo/GridFS); supersedes c19 (skip audio)
- pocketsync-mongo persists its data on a host bind mount that is readable outside Docker and survives container restarts and re-creation, with restart policy unless-stopped
- pocketsync-mongo runs the mongodb/mongodb-atlas-local image (Mongo 8 + mongot) so recordings get a native vector index and $vectorSearch; after it is tested in pocketsync, open an issue on eidetic-cli proposing the same migration
- The 'type' filter fields come from the lobes decider role (Clef on the Orin, POST /v1/systemone via the gateway); the operator states it is validated and staying on the Orin, and tracks its rollout in a separate session
- pocketsync itself does the enrichment: chunking, gemma2-embed embeddings and decider classification run inside pocketsync after the landing, not in a culture-rules workflow
- Keep Cloudflare Access on pocketsync.culture.dev and exempt only the exact path pocketsync.culture.dev/hooks/pocket with a Bypass (Include: Everyone) Access app, as rules.culture.dev does for /hooks/github; never widen it
- Deleted in Pocket -> tombstone: pocketsync keeps the doc and audio file and marks the doc `deleted_in_pocket` (with the deletion time); merged recordings keep their source docs marked `merged_into` the surviving recording id. Nothing is purged automatically
- For now pocketsync emits a single handoff event per recording content hash, after enrichment (vectors + type labels) completes; more event types can be added later

## Hard questions

- Does pocketsync itself call orin-embed and the Orin classifier at ingest time (pocketsync then depends on Orin being up, beyond issue #1's 'land it and stop' lane), or does pocketsync land the raw doc and emit its event while a culture-rules workflow or a pocketsync 'enrich' stage fills the vector + type fields asynchronously? Either way, sync must not block or fail when Orin is down. (resolved: pocketsync does embeddings, chunking and processing (c33))
- Access posture for pocketsync.culture.dev: --no-access (whole host public, receiver-only auth; simplest), or Access on with an exact-path Bypass for /hooks/pocket (matches the rules.culture.dev precedent, keeps any future status/admin route behind SSO)? (resolved: Access on + exact-path Bypass for /hooks/pocket (c34))

## Open parks

- [unknown_nonblocking] Pocket API rate limits: a 429 is documented but no numbers, headers or polling guidance — needs a live probe with a real `pk_` key before choosing the reconcile interval
- [unknown_nonblocking] Recording 'state' values are not enumerated in the docs, so 'processing finished' must be inferred from webhook stage events or non-empty transcript/summarizations until observed live
- [unknown_nonblocking] Webhook timestamp unit: docs say Unix ms, a real-world receiver (Lifecycle-Innovations-Limited/claude-ops webhook-receiver) reports seconds and `sha256=<hex>` signature forms — receiver should accept both until verified
- [unknown_nonblocking] Zone-level WAF / Bot Fight Mode on culture.dev could challenge Pocket's HeyPocket-Webhook/1.0 POSTs even with Access bypassed — verify with a test delivery (Pocket app integrations UI) once the hostname is up
- [unknown_nonblocking] gemma2-embed retrieval quality on prose/voice transcripts is UNVALIDATED (lobes-cli issue #296; only code retrieval measured) — evaluate on synthetic or private transcripts before relying on semantic search quality
- [unknown_nonblocking] mongodb-atlas-local persistence layout: confirm which container paths hold data besides /data/db and /data/configdb (the mongot search-index directory) so the c30 bind mounts cover the vector index too and it survives re-create without a full re-index
- [unknown_nonblocking] atlas-local docs (mongodb.com atlas-cli-deploy-docker) list only /data/db and /data/configdb volumes and do not say where mongot keeps search-index data or whether indexes persist across re-create — the c41 restart test is the probe that settles it (reinforces v9)
- [unknown_nonblocking] Concurrent personal + org webhooks: if the operator is also in a Pocket organization, one recording event fans out to personal and every org webhook — dedupe must key on (event, recording id, timestamp), not delivery
- [unknown_nonblocking] With the event emitted only after enrichment, a long Orin/gateway outage delays every handoff event; decide later whether a doc pending enrichment for more than some threshold should emit anyway (with `enrichment_status` pending) or keep waiting
- [follow_up] After pocketsync's atlas-local vector search is tested, open an issue on agentculture/eidetic-cli proposing migration from client-side cosine to mongodb-atlas-local $vectorSearch
- [follow_up] Backups: the owned copy (c37 'survives account loss') lives on one host bind mount with no backup; a mongodump/restic schedule for ~/.local/share/pocketsync is a follow-up outside this frame (could later be a culture-rules scheduled rule)

## Resolved vagueness

- [unknown_nonblocking] Audio download plan tier: one secondary source says audio needs Pocket Pro; get-audio-download-url docs don't say — confirm with the operator's account — resolved: Operator: free tier includes audio download and text export
- [unknown_nonblocking] Decider (Clef) readiness on the Orin: not merged/advertised; whether it accepts multi-thousand-token transcripts within its 16,384 context is unmeasured — classify on summary text if transcripts are too long — resolved: Operator: decider is validated and stays on the Orin; tracked in another session
