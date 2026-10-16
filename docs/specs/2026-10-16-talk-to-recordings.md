# talk to recordings

> Talk to my Pocket data: on-demand re-enrichment (pocketsync enrich), an MCP server (pocketsync mcp) and a private chat site (pocketsync web at recall.culture.dev) — one retrieval/enrich core, three surfaces with CLI↔MCP↔site parity
> instruction: tests assert the MCP tool handler, CLI handler and web API handler call the same core function for search and enrich

## Audience

- the operator (the one Pocket account owner) in a browser at pocketchat.culture.dev, and any agent harness (Claude Code, Qwen, colleague, the mesh) via pocketsync mcp, plus the operator's shell via the CLI
  - instruction: check README names the three surfaces and who uses each

## Before → After

- Before: recordings land in pocketsync-mongo enriched with vectors and labels, but nothing reads them back: no search, no chat, no MCP, and the only enrichment lever is reembed for model/dims mismatches (error docs and taxonomy or outage re-runs have no operator control)
  - instruction: compare against 0.11.1: pocketsync --help shows no search/enrich/mcp/web verbs
- After: the operator asks 'what did we decide about X last week?' in the browser or any agent and gets a grounded answer citing recordings and timestamps, and can re-enrich any selection with one dry-run-then-apply action from CLI, MCP or a button
  - instruction: run pocketsync web, ask a question on synthetic data, see citations; run pocketsync enrich --id X then --apply

## Why it matters

- the point of owning the data is using it: Pocket's own app chats only inside its cloud, while our copy can be searched by every agent we run, privately on our own gateway and network
  - instruction: README section states the privacy model (local gateway, sensitive excluded by default, Access-only site)

## Requirements

- pocketsync mcp uses the official mcp SDK low-level Server over stdio behind a pocketsync\[mcp\] extra (mcp>=1.2,<2), lazily imported (ServerExtraMissing -> CliError exit 2 with an install hint), stdout reserved for protocol — mirroring culture-rules
  - instruction: tests/`test_mcp.py`: subprocess import check + CliError path; in-process session via `create_connected_server_and_client_session` (importorskip mcp)
  - honesty: importing pocketsync, pocketsync.cli and pocketsync.mcp leaves mcp out of sys.modules; pocketsync mcp without the extra exits 2 with an install hint and nothing on stdout
- one verb registry drives CLI, MCP tools and site actions; a data-driven parity test asserts every registry verb exists as a CLI command and an MCP tool (and no extra tools), mutating verbs expose apply (default false) and dry-run otherwise
  - instruction: tests/`test_surface_parity.py` data-driven over the registry, with mutation cases like culture-rules
  - honesty: removing a verb from the registry, a CLI parser entry or an MCP tool each makes the parity test fail
- pocketsync enrich selects recordings (--all/--since/--until/--id/--label/--status/--only) and re-queues them by flipping enrichment.status to pending with attempts=0 and clearing error/`next_attempt_at`, dry-run unless --apply; the existing EnrichmentWorker does the work (no second enrichment path)
  - instruction: tests/`test_cli_enrich.py` over FakeRuntime with mongomock; each selector covered
  - honesty: dry-run writes nothing (collection snapshot unchanged) and lists exactly the selected ids; apply flips only selected non-deleted docs and only when `content_hash` still matches
- --only classify re-runs the decider on stored chunk/summary text and writes only labels/filters/`taxonomy_version`, keeping vectors; --only embed re-embeds keeping labels — the worker gains a per-doc enrichment.mode (full|classify|embed) because `_build_payload` today does embed+classify all-or-nothing
  - instruction: worker tests with a fake gateway counting embed/classify calls
  - honesty: --only classify leaves enrichment.chunks and `summary_vector` byte-identical and makes no embed call; --only embed leaves labels/filters identical and makes no classify call
- gateway deferrals are persisted: when EnrichmentUnavailable defers a pending doc, the worker writes enrichment.`deferred_reason` and enrichment.`next_attempt_at` so status/doctor can say why the backlog is not draining and when it retries (today the reason lives only in the in-memory `_retry` map)
  - instruction: worker + status tests with a gateway raising EnrichmentUnavailable
  - honesty: after a 503 the doc stays pending with enrichment.`deferred_reason` and `next_attempt_at` set; status --json reports them; a successful run clears both
- a re-enrich that changes labels without changing `content_hash` emits one pocketsync.recording.reclassified event via the outbox (today Outbox dedupes on (id, `content_hash`) so reclassify publishes nothing)
  - instruction: tests/`test_events.py` + worker test; README event schema documents pocketsync.recording.reclassified
  - honesty: exactly one reclassified event per label change; none when labels are unchanged; enriched events keep their (id, `content_hash`) dedupe
- GatewayClient gains a chat-completions call (/chat/completions) with the model from a new non-secret setting `POCKETSYNC_CHAT_MODEL` defaulting to unsloth/Qwen3.8-27B-NVFP4, so the chat lane can be switched (e.g. to Nemotron 3.5 Super VL) without code changes
  - instruction: tests/`test_config.py` + gateway test capturing the request body
  - honesty: changing `POCKETSYNC_CHAT_MODEL` changes the model field of the /chat/completions request with no code change; it is not a secret and not injected via grant
- pocketsync enrich replaces the narrow reembed selector but reembed stays as an alias/compat verb (its JSON keys `dry_run`/model/dims/ids/count/updated are an existing contract)
  - instruction: keep the existing reembed tests unedited
  - honesty: pocketsync reembed output keys and behaviour are byte-compatible with 0.11.1 tests (tests/`test_cli_status_reembed.py` passes unchanged)
- a read-only retrieval core (pocketsync/recall/ or similar) runs $vectorSearch over `recordings_chunk_vec` (root filters in parentFilter) and `recordings_summary_vec` (filters in filter), embeds the query via the gateway with `prompt_name` Query (embed() hardcodes Document today), excludes `deleted_in_pocket` and `merged_into` docs, and returns ids, titles, `recording_at`, score and the best-matching chunks with start/end offsets
  - instruction: unit tests on the built pipeline + fake search seam; opt-in atlas-local integration test
  - honesty: search with a label filter puts it in parentFilter for the chunk index and filter for the summary index; deleted and merged docs never appear; query embed uses `prompt_name` Query
- the vector index filter fields grow to include enrichment.filters.sensitive and enrichment.filters.`has_action_items` (today `FILTER_PATHS` is labels/`deleted_in_pocket`/`recording_at` only, so sensitive cannot be filtered inside $vectorSearch); vindex updates an existing index definition in place instead of only creating missing ones
  - instruction: tests/`test_vindex.py` with a stub collection; atlas-local integration test
  - honesty: an existing index lacking the new filter fields is updated in place (`update_search_index`) and stays queryable; check() no longer reports a mismatch afterwards
- sensitive recordings are searchable by default on every read surface (user decision: internal network); an opt-in `exclude_sensitive` filter drops docs whose enrichment.filters.sensitive is at or above threshold; absent scores are treated as not sensitive
  - instruction: parametrized test across CLI, MCP and web API handlers
  - honesty: with default arguments a sensitive recording is returned; with `exclude_sensitive`=true no recording with sensitive >= threshold is returned and absent-score recordings still are
- $vectorSearch is unit-tested through an injectable search seam (fake returns canned hits) since mongomock cannot run it; real behaviour (parentFilter on `recording_at`, `deleted_in_pocket`, sensitive) is covered in the opt-in tests/integration/`test_atlas_local.py`
  - instruction: inspect core signature; `POCKETSYNC_IT_MONGO`=1 run on spark against a throwaway db
  - honesty: the core takes a search callable; unit tests never need $vectorSearch from mongomock
- read verbs get CLI parity with the MCP tools: pocketsync search, get, transcript, recordings (list), actions — all --json, all read-only, all runtime verbs under `_grant` re-exec
  - instruction: parity test compares CLI --json output with MCP tool output on fake data
  - honesty: each read verb exists in CLI and MCP with the same parameters and returns the same JSON for the same inputs
- pocketsync web is a stdlib ThreadingHTTPServer (like webhook/server.py) bound to loopback only on a new setting `POCKETSYNC_WEB_PORT` (default 8788), serving one self-contained HTML/JS page plus a JSON API (/api/chat, /api/search, /api/recordings, /api/recordings/{id}, /api/actions, /api/enrich) that calls the same retrieval/enrich core as CLI and MCP — no extra dependency and no second retrieval implementation
  - instruction: tests/`test_web.py` starting the server on an ephemeral port
  - honesty: pocketsync web refuses non-loopback binds, adds no dependency, and its API handlers are the registry verbs
- chat answers are generated by the configured chat model from retrieved chunks only, and every answer cites recordings with ids and start/end timestamps; clicking a citation opens that recording's transcript at the timestamp; a per-recording and per-result-set Re-enrich button calls the enrich operation (dry-run preview, then apply)
  - instruction: web/chat tests with a fake chat client; prompt instructs cite-only-from-context
  - honesty: a chat response's citations each reference a retrieved recording id and an in-range timestamp; with no retrieval hits the answer says it found nothing instead of inventing
- deploy adds deploy/systemd/pocketsync-web.service (grant run --inject, like pocketsync-serve) and a runbook section adding pocketchat.culture.dev as a second ingress hostname on the existing remote-managed pocketsync-culture-dev tunnel with a Cloudflare Access app allowing only the operator email and no Bypass; web verifies the Access JWT (team domain + audience settings) on every request
  - instruction: curl status codes only; record ids on issue #3 style comment
  - honesty: a request without a valid Access JWT (missing, bad signature, wrong aud, expired) gets 403 from pocketsync web even on loopback
  - honesty: after deploy: GET <https://pocketchat.culture.dev> unauthenticated -> 302 to Access; pocketsync.culture.dev routes unchanged (GET / 302, unsigned hook POST 401)
- docs: CLAUDE.md, README.md and QWEN.md amend the 'no UI' non-goal to 'UI only as a thin client over the CLI/MCP surface'; all four harness files plus README, explain catalog, learn (text+JSON), overview.`_VERBS`, tests/`test_cli_surface.py` `NEW_VERBS` and `_grant`.`RUNTIME_VERBS` list the new verbs
  - instruction: run lint job locally
  - honesty: teken cli doctor --strict passes and tests/`test_cli_surface.py` covers every new verb

## Honesty conditions

- one shared core module serves all three surfaces: a grep shows no $vectorSearch or enrichment-selection query outside it
- existing sync/landing/event tests pass without edits
- the Bypass app path is unchanged after deploy (Cloudflare read shows exactly pocketsync.culture.dev/hooks/pocket)
- pyproject dependencies stay \[\]; core import graph has no third-party modules
- each named audience has a documented entry point (README: browser URL, .mcp.json snippet, CLI verbs)
- 0.11.1 has no read-back verb (pocketsync --help lists none) and reembed selects only model/dims mismatches
- the after-state is demonstrated on synthetic data in tests and on live data by the c28 probes
- no request from any surface leaves spark except via the Cloudflare tunnel to the Access-gated browser; generation and embedding go to localhost:8001
- the probes ran against the live deploy and their outputs (ids, counts, status codes only) are recorded on the PR
- CI on the PR shows all jobs green with the parity test included

## Success signals

- on the live deploy: pocketsync search returns the matching recording first for >= 3 of 3 operator-chosen probe questions; a chat answer cites >= 1 recording with a timestamp; pocketsync enrich --status error --apply drains to 0 retrying within one worker cycle when the gateway is up; recall.culture.dev returns 302 unauthenticated
  - instruction: probe live, print only ids/counts/status codes (never content), record numbers on the PR
- test suite: CLI↔MCP parity test passes for 100% of registry verbs, the full suite stays green and coverage stays >= 60%, teken cli doctor --strict passes, Sonar new issues = 0
  - instruction: uv run pytest -n auto --cov; teken cli doctor . --strict; sonar gate on the PR

## Scope / boundaries

- the sync contract is unchanged: file layout, landing schema, `content_hash` and the pocketsync.recording.enriched event stay as they are; new surfaces only read recordings and, for enrich, write enrichment.\* fields through the sink's compare-and-set
  - instruction: git diff of tests/`test_sync`\*.py, `test_events.py` enriched-event assertions untouched
- the webhook Bypass stays exactly pocketsync.culture.dev/hooks/pocket; pocketchat.culture.dev gets no Bypass; web and webhook servers refuse non-loopback binds
  - instruction: read the Access apps via the cf read script; print names/paths only
- core stays dependency-free: mcp SDK only behind pocketsync\[mcp\]; web uses stdlib only; no numpy; the lobes gateway and other Mongo containers on spark are not modified
  - instruction: test that imports pocketsync.recall/web with a sys.modules check

## Non-goals

- no multi-user accounts, no chat history persistence beyond the browser session, no taxonomy editing (that is #6), no streaming STT or audio playback transcoding in this increment

## Assumptions

- nestedRoot $vectorSearch returns parent documents, not the matched chunk; the core identifies the best chunks by cosine over the returned doc's stored chunk vectors locally (pure-Python, no numpy dependency)

## Scope exploration

- `s1` — `culture-rules/culture_rules/mcp/{server,tools}.py + pyproject.toml:26`: official mcp SDK low-level Server over `stdio_server`, extra mcp>=1.2,<2, lazy `_sdk`() raising ServerExtraMissing -> CliError exit 2; tools.py has no third-party imports
  - seeds: `c2`
- `s2` — `culture-rules/culture_rules/cli/{registry,verbs}.py + tests/test_surface_parity.py + tests/mcp/test_mcp.py`: one Verb registry drives argparse and MCP `tool_specs`; `params_schema` adds apply(default False) for mutating verbs; parity test is data-driven; MCP tests use mcp.shared.memory.`create_connected_server_and_client_session` in-process
  - seeds: `c3`
- `s3` — `pocketsync/cli/_commands/reembed.py:133-176 + vindex.reembed_plan (vindex.py:131)`: reembed only selects model/dims mismatches and raw `update_many` sets status pending/attempts 0 without clearing error/`next_attempt_at`; JSON keys `dry_run`/model/dims/ids/count/updated are an existing contract
  - seeds: `c4`, `c9`
- `s4` — `pocketsync/enrich/worker.py:155-186 (_build_payload) + worker.py:68-81 (mark_for_reclassification)`: embed and classify run together all-or-nothing; there is no classify-only or embed-only path; reclassify on taxonomy change re-runs everything
  - seeds: `c5`
- `s5` — `pocketsync/enrich/worker.py:190-194 (_defer) + gateway.py:264 (EnrichmentUnavailable)`: 404/429/5xx defer a pending doc via the in-memory `_retry` map only; the reason and retry time are discarded, so status cannot explain a non-draining backlog and a restart retries immediately
  - seeds: `c6`
- `s6` — `pocketsync/events.py:111-154 (Outbox.append dedupe on (id, content_hash))`: a re-enrich with unchanged `content_hash` emits nothing, so label changes from reclassification are never published
  - seeds: `c7`
- `s7` — `pocketsync/enrich/gateway.py:256,383,416,433 + config.py:25-60`: GatewayClient has embed (`prompt_name` Document hardcoded) and classify (/systemone) but no chat call; settings are named `POCKETSYNC_` plus the upper-cased setting name, non-secret settings sit outside `SECRET_SETTINGS`
  - seeds: `c8`, `c10`
- `s8` — `pocketsync/vindex.py:41-74 + tests/integration/test_atlas_local.py:95-189 + README Vector search`: indexes `recordings_summary_vec` (filter) and `recordings_chunk_vec` (nestedRoot, parentFilter); `FILTER_PATHS` are labels/`deleted_in_pocket`/`recording_at` only; `ensure_index` only creates missing indexes; $vectorSearch is tested only in opt-in atlas-local integration tests
  - seeds: `c10`, `c11`, `c14`
- `s9` — `pocketsync/enrich/default_taxonomy.toml:38 + gateway.py:455-461 + README ~236-241`: sensitive is enrichment.filters.sensitive (probability), absent when the decider did not answer; README says shareable only if present and low
  - seeds: `c12`
- `s10` — `sinks/mongo.py:53-85 + model.py:23-65 + enrich/chunking.py`: chunks carry index/start/end/text/vector but no speaker (speaker lives in segments); nestedRoot search returns parent docs, so best-chunk selection must happen client-side
  - seeds: `c13`
- `s11` — `pocketsync/cli/__init__.py:65-111 + _runtime.py:44-150 + tests/cli_fakes.py:151-233 + tests/test_cli_surface.py:14-65 + _grant.py:36`: verbs register by hand, read through cached Runtime accessors that FakeRuntime overrides; surface tests require --json, --apply default off, explain/learn/overview entries; `RUNTIME_VERBS` gates grant re-exec
  - seeds: `c15`, `c19`
- `s12` — `pocketsync/webhook/server.py:15-171 + serve.py:115-207`: stdlib ThreadingHTTPServer, loopback-only (ValueError on non-loopback), daemon-thread start/stop, `run_until_signal`; `webhook_port` setting only
  - seeds: `c16`, `c21`
- `s13` — `deploy/systemd/pocketsync-serve.service + deploy/cloudflared/cloudflared-pocketsync.service + docs/operations/pocketsync-culture-dev.md`: remote-managed tunnel token (ingress lives in Cloudflare, set via cultureflare remote-login setup), Access on host + exact-path Bypass app; a second hostname needs only Cloudflare-side ingress, DNS and an Access app
  - seeds: `c18`, `c21`
- `s14` — `CLAUDE.md:19-20, README.md:22-23, QWEN.md:56`: 'no UI' non-goal stated in these three; AGENTS.override.md, AGENTS.colleague.md, .pi/SYSTEM.md have none; QWEN.md hard-codes the verb list
  - seeds: `c19`
- `s15` — `pyproject.toml (dependencies=[], extras mongo/events)`: runtime is stdlib-only; new third-party libs go behind extras with lazy import
  - seeds: `c22`

## Decisions

- web verifies the Cloudflare Access JWT on every request (defense in depth beyond Access at the edge and loopback bind)
- sensitive recordings are NOT excluded by default on any surface; `exclude_sensitive` is an opt-in filter
- the site hostname is pocketchat.culture.dev
