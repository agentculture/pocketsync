# Delivery Summary — pocket to mongodb near-realtime

plan: `pocket-to-mongodb-near-realtime` · run: `partial` · date: `2026-10-14`
baseline: `devague summary skeleton`

## Intent

This run set out to make pocketsync keep a local MongoDB up to date with every
Pocket recording's transcript, summary, action items and metadata, as close to
realtime as Pocket's API limits allow. It executed the converged 20-task plan
`docs/plans/2026-10-14-pocket-to-mongodb-near-realtime.md` (spec
`docs/specs/2026-10-14-pocket-to-mongodb-near-realtime.md`) through
`/assign-to-workforce` on branch `feat/pocket-to-mongodb`. The pieces are the
official Pocket API, signed webhooks via `pocketsync.culture.dev`, a dedicated
`mongodb/mongodb-atlas-local` store with `gemma2-embed` vectors and decider
type labels, and audio saved as files. The code-side plan (t1–t18) merged. The
operator-side deploy and acceptance (t18's live steps, t19) are pending in
issue #3.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Core model, content hash and config/secrets loader
- `t2` — Pocket source adapter: interface, official HTTP client, fake
- `t3` — pocketsync-mongo deployment (atlas-local, bind mounts, auth, loopback)
- `t4` — Update the API premise in all four harness prompt files
- `t5` — Enrichment clients: transcript chunking, gemma2-embed and decider gateway client
- `t6` — Webhook signature verification and parsing (pure module)
- `t7` — Sink interface and local file sink with atomic audio download
- `t8` — Mongo sink and sync-state store (upserts, field groups, CAS, tombstones)
- `t9` — Vector search index manager with model/dimension guard
- `t10` — Handoff events: events.jsonl outbox and events-cli QoS 1 publish
- `t11` — Landing engine: canonical fetch, files, mongo upsert, delete/merge
- `t12` — Enrichment worker: pending queue, CAS writes, concurrency cap, event after enrichment
- `t13` — Reconciler and backfill with overlap, 429 backoff and webhook-miss counter
- `t14` — Webhook HTTP receiver: loopback server, size limit, fast ack, dedupe, enqueue
- `t15` — CLI verbs: auth, list, sync, watch, serve, status, reembed (+ catalog, learn, overview, doctor)
- `t16` — README: document schema, event contract, env table and audiences
- `t17` — Test gates: offline guarantee, mongodb:// secret scan, opt-in atlas-local integration test
- `t18` — Deploy: systemd serve unit, cultureflare tunnel, Access Bypass for /hooks/pocket, Pocket webhook
- `t19` — End-to-end acceptance: latency, restart durability, real doc fields
- `t20` — Open the eidetic-cli migration issue after atlas-local is validated

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `pocketsync/model.py`, `pocketsync/config.py`, extras `mongo`/`events` in `pyproject.toml`; merge `880e412` |
| `t2` | delivered | `pocketsync/pocket/{base,http,fake}.py`; merge `1fed764`. Wire mapping corrected to the live API by main-agent fixer `3b401f5` (see drift) |
| `t3` | delivered | `deploy/mongo/` (atlas-local `8.0.34`, `127.0.0.1:27019`, mounts db/configdb/mongot, fixed hostname), `docs/operations/pocketsync-mongo.md`; merge `6ca0e6a` |
| `t4` | delivered | API premise rewritten in the five harness prompt files; host typo fixed by main agent before merge; merge `12c86c9` |
| `t5` | delivered | `pocketsync/enrich/{chunking,gateway}.py`, `default_taxonomy.toml`; merge `3221dc8` |
| `t6` | delivered | `pocketsync/webhook/verify.py`; merge `6d8f56a` |
| `t7` | delivered | `pocketsync/sinks/{base,local}.py`, shared sink contract test; merge `7f2f6ed` |
| `t8` | delivered | `pocketsync/sinks/mongo.py`, `pocketsync/state.py`; merge `8d8c9b1` |
| `t9` | delivered | `pocketsync/vindex.py` (summary + `nestedRoot` chunk indexes, model/dims guard); merge `de68527` |
| `t10` | delivered | `pocketsync/events.py` (outbox, QoS 1 publish, connect-wait added on rework); merge `44b6aed` |
| `t11` | delivered | `pocketsync/sync.py`; merge `3fb3d07` |
| `t12` | delivered | `pocketsync/enrich/worker.py`; merge `ea39c78` |
| `t13` | delivered | `pocketsync/reconcile.py`; merge `5490609` |
| `t14` | delivered | `pocketsync/webhook/server.py`; merge `0e2584b` |
| `t15` | delivered | 7 verbs, `pocketsync/serve.py`, doctor checks, explain/learn/overview entries; merge `1378b5d` |
| `t16` | delivered | README data/event contract and secrets table; merge `08cd0dc`. Stale API-premise paragraph corrected in `7a6e3f7` |
| `t17` | delivered | offline socket guard, `mongodb://` scanner rule, `tests/integration/`; merge `6a1476c`. Chunk-filter test corrected to `parentFilter` in `7a6e3f7` |
| `t18` | partial | units + runbook + tests merged (`1c84600`); live provisioning (cultureflare, Access Bypass, grant secrets, Pocket webhook, verification curls) is operator-only and **not done**, so it's tracked in issue #3 |
| `t19` | blocked | needs the deployed stack and ≥ 10 real recordings, so it's tracked in issue #3; nothing measured yet |
| `t20` | delivered | proposal filed as `agentculture/eidetic-cli#44`, ahead of t19 (see drift, `d5`) |

## Mid-work Decisions

All five deviation records were filed `--origin llm` and have been
**approved** by the operator (2026-10-15). They are quoted here as recorded.

- `d1` — Run t2 after t1 merges (add dependency t2 -> t1) instead of in wave 0
  — t2's acceptance criterion 2 maps API responses to `model.Recording`, which
  t1 creates.
- `d2` — Exclude `Recording.raw` from `content_hash`. Volatile wire fields
  would make every poll rewrite every document. Approved 2026-10-15 and
  implemented test-first (`HASH_EXCLUDED_FIELDS` in `pocketsync/model.py`;
  `tests/test_model.py::test_hash_ignores_raw_wire_payload`).
- `d3` — `pocketsync-mongo` compose also pins `hostname: pocketsync-mongo`. A
  re-created single-node replica set otherwise comes up
  RSGhost/ReplicaSetNoPrimary (verified in a throwaway container).
- `d4` — Fixer: correct t2's Pocket adapter mapping to the live API shapes and
  add X-Ratelimit header throttling. A read-only live probe showed action items
  at `v2.actionItems.actions` and audio at `data.signed_url`, plus a limit of
  50 requests per ~60 s.
- `d5` — File the eidetic-cli migration issue (t20) before t19's operator
  acceptance. atlas-local was validated by t17's integration test against a
  real container.
- t10 was sent back once before merge. Its first version never waited for the
  broker connection, so a fresh `EventClient` would report `no_conn` forever.
  This was fixed with a bounded connect-wait and delivery confirmation (no
  deviation record; it was a defect against h38, fixed within the task).
- r9's uncertainty (the contradictory `nestedRoot` docs) was settled by a
  main-agent probe on a throwaway atlas-local. Root-level filters must use
  `$vectorSearch.parentFilter`. The index design was kept and the test
  corrected (`7a6e3f7`).
- The `serve` landing queue is unbounded by design: the receiver marks dedupe
  keys before enqueueing (t14 concern carried into t15's brief).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t2` (`d1`, approved) | scheduled after t1 instead of wave 0 because it depends on `model.Recording`. Content unchanged | acceptable |
| `t2` (`d4`, approved) | as built, the adapter dropped every action item and failed every audio download against the live API. A main-agent fixer remapped the shapes and added rate-limit throttling | needs-follow-up |
| `t1` (`d2`, approved) | the hash covers the mapped content fields only, not Pocket's raw wire payload. Implemented after approval, so a wire-only change no longer re-triggers writes and enrichment | acceptable |
| `t3` (`d3`, approved) | fixed hostname added beyond the acceptance criteria to make h25/c41 achievable | acceptable |
| `t18` | the plan's live acceptance criteria (Access redirect, Pocket test delivery reaching the receiver, receiver on 127.0.0.1) are unmet because they need operator credentials | needs-follow-up |
| `t19` | not started: blocked on t18's live steps and real recordings | needs-follow-up |
| `t20` (`d5`, approved) | filed before t19 instead of after it, citing integration-test validation | acceptable |
| `t9`/`t17` | the chunk index filter contract changed from `filter` to `parentFilter` (no record; settled by probe evidence) | acceptable |

## Evidence

- tests (agent-side behavioral run at `1c84600`, 2026-10-14T21:10:01): 321
  passed across the 24 behavior-bearing test files. Filed as evidence `e1`–`e54`
  against obligations `o1`–`o32`. Examples:
  - `tests/test_reconcile.py::test_second_pass_no_changes_writes_nothing` — pass
  - `tests/test_webhook_server.py::test_ack_latency_with_slow_consumer` — pass
  - `tests/test_enrich_worker.py::test_content_change_mid_enrichment_discards_and_requeues` — pass
  - `tests/test_sync.py::test_webhook_and_reconciler_paths_identical` — pass
  - `tests/test_cli_sync.py::test_dry_run_is_default_and_performs_zero_writes` — pass
  - `tests/test_pocket_http_live_shapes.py::test_live_action_items_under_actions_are_mapped` — pass
- full suite: `uv run pytest -n auto` — 437 passed, 5 skipped. The skips are 4
  opt-in integration tests and 1 report-only cross-repo gap.
- integration: `tests/integration/test_atlas_local.py` against a throwaway
  `mongodb/mongodb-atlas-local:8.0.34`. 4/4 passed at `7a6e3f7` (evidence `e55`, obligation `o33`). Obligations `o34`–`o38`
  (c1, c39, c41, c37, c35) have no evidence yet.
- lint/gates at `1c84600`:
  - `flake8`, `black --check`, `isort --check` — clean;
  - `bandit` — 0 issues;
  - `python3 scripts/scan-secrets.py` — clean;
  - `uv run teken cli doctor . --strict` — exit 0;
  - `scripts/harness-smoke.py --stage config` — 6 passed (evidence `e54`).
- live API: a read-only shape probe with the operator's key printed key names
  and types only. It returned 200 on list, details and audio-url, and showed
  `X-Ratelimit-Limit: 50`. No recording content was printed or committed.
- commits: `836e47b..1c84600` (88 files changed).
- issues: `agentculture/pocketsync#3` (operator hand-turn),
  `agentculture/pocketsync#4` (CLI follow-ups),
  `agentculture/eidetic-cli#44` (t20).
- behavioral deltas filed: `b1` (parentFilter), `b2` (adapter live shapes +
  throttle), `b3` (hostname pin). All are proposed.

## Delivery Claims

Lapse ledger: `l1`–`l7` are **approved** (2026-10-15), so they count as
evidence here. They record that t2, t7, t11 and t15 did not strictly work
test-first, that t2 guessed wire shapes (confirmed real and fixed by `d4`), and
that t13 started from a wrong test premise; `l7` records a scan-secrets run
before `.devague/` was tracked. Confidence below is capped where those facts
bear on a claim. Evidence `e1`–`e55` is also approved.

| Claim | Confidence | Evidence |
|-------|------------|----------|
| Mongo writes are idempotent: identical re-delivery changes nothing; a no-change reconcile pass performs 0 writes | high | test `tests/test_sink_mongo.py::test_identical_redelivery_is_noop` · test `tests/test_reconcile.py::test_second_pass_no_changes_writes_nothing` |
| A stale enrichment never overwrites newer content (CAS on content hash) | high | test `tests/test_sink_mongo.py::test_enrichment_race_content_changes_mid_flight` · test `tests/test_enrich_worker.py::test_content_change_mid_enrichment_discards_and_requeues` |
| The webhook receiver fails closed, acks in < 2 s and never processes unverified bodies | high | test `tests/test_webhook_server.py::test_ack_latency_with_slow_consumer` · test `tests/test_webhook_verify.py::test_verify_and_parse_never_parses_unverified` |
| Webhook and reconciler paths produce the same document | medium | test `tests/test_sync.py::test_webhook_and_reconciler_paths_identical` (excludes `files.meta`, whose `meta.json` carries a wall-clock `synced_at`) |
| The Pocket adapter maps the live API's action items and audio URL and respects its rate limit | medium | commit `3b401f5` · test `tests/test_pocket_http_live_shapes.py::test_live_action_items_under_actions_are_mapped` (synthetic fixtures mirroring the live shape; l2) |
| `$vectorSearch` with type filters works on atlas-local, including nested chunk vectors | high | test `tests/integration/test_atlas_local.py::test_chunk_vector_search_with_parent_label_filter` at commit `7a6e3f7` |
| `pocketsync-mongo` data survives container re-create | medium | file `deploy/mongo/docker-compose.yml` · test `tests/test_mongo_deploy.py::test_bind_mounts_cover_db_configdb_and_mongot` (verified only in t3's and t17's throwaway containers, not on the real stack) |
| Handoff events are published once per content hash, QoS 1, surviving broker outages | medium | test `tests/test_events.py::test_broker_down_leaves_pending_then_publishes` (fake client; the real events-cli path never ran against a live broker) |
| The CLI surface (7 verbs, `--json`, dry-run default) passes the agent-first rubric | high | test `tests/test_cli_sync.py::test_dry_run_is_default_and_performs_zero_writes` · `teken cli doctor --strict` exit 0 at `1c84600` |
| A new Pocket recording lands in `pocketsync-mongo` with no manual step (c1/h1) | unverified | (no evidence: not deployed; issue #3) |
| p95 webhook→doc ≤ 10 s and →enriched ≤ 60 s (c39) | unverified | (no evidence: needs real deliveries; issue #3) |
| Restart durability on the real stack (c41) | unverified | (no evidence: throwaway-only; issue #3) |
| `pocketsync.culture.dev` serves `/hooks/pocket` behind Access with an exact-path Bypass (c21/c22 live) | unverified | (no evidence: operator-only provisioning; issue #3) |

## Remaining Work / Follow-up

- `t18` (partial): run the operator steps in `docs/operations/pocketsync-culture-dev.md`.
  These are cultureflare dry-run then `--apply`, the exact-path Access Bypass via
  the Cloudflare API, the grant secrets, the systemd units, the Pocket personal
  webhook, and the verification curls. **Owner:** operator, issue #3.
- `t19` (blocked): after t18, record ≥ 10 private clips. Compute the p95
  latencies into `docs/operations/acceptance-2026-10.md` (numbers only), run the
  real-stack re-create check, and confirm the after-state fields. **Owner:**
  operator + agent, issue #3.
- Adjudicate deltas `b1`–`b3`. Deviations `d1`–`d5`, lapses `l1`–`l7` and
  evidence `e1`–`e55` were approved on 2026-10-15, and `d2` is implemented.
  **Owner:** operator.
- t15 follow-ups (issue #4):
  - `reembed --apply` vs `serve`'s vector-space guard;
  - the `recording.merged` payload keys;
  - daemon dry-run semantics;
  - the `webhook_missed` window;
  - re-classification on start.
- q8 (the decider label taxonomy) is resolved provisionally (2026-10-15). The
  default labels stay, a `sensitive` filter was added, and the set is revisited
  after the live deploy. Making labels configurable via API, MCP and CLI, with
  human approval through culture-rules, is the next feature.
- Untested paths:
  - the real events-cli publish against a live broker;
  - `gemma2-embed` retrieval quality on prose (v6, lobes #296);
  - Cloudflare WAF behaviour on Pocket's POSTs (v5).
- Backups of `~/.local/share/pocketsync` are out of scope (v10).
- Operational note: during t3 a task agent ran `pkill -f mongosh` on the shared
  host. It only hit its own shell, but future briefs should forbid
  pattern-based kills.
