# Delivery Summary — talk to recordings

plan: `talk-to-recordings` · run: `complete` · date: `2026-10-16`
baseline: `devague plan (talk-to-recordings)`

## Intent

> Talk to my Pocket data: on-demand re-enrichment (pocketsync enrich), an MCP server (pocketsync mcp) and a private chat site (pocketsync web at pocketchat.culture.dev, decision c32) — one retrieval/enrich core, three surfaces with CLI↔MCP↔site parity

After: the operator asks 'what did we decide about X last week?' in the browser or any agent and gets a grounded answer citing recordings and timestamps, and can re-enrich any selection with one dry-run-then-apply action from CLI, MCP or a button

## Planned Work

- `t1` — settings: `chat_model`, `web_port`, `access_team_domain`, `access_aud`, `site_origin`
- `t2` — gateway: query embeddings and a chat-completions call
- `t3` — vindex: sensitive and `has_action_items` filter fields, in-place index update
- `t4` — sink: requeue with generation, generation-aware CAS, persisted deferral, partial writes
- `t5` — web access: cite culture-rules' stdlib Cloudflare Access JWT verifier
- `t6` — worker: enrichment modes, generation CAS, persisted deferrals, reclassified event, status deferral report
- `t7` — recall core: search and read operations
- `t8` — recall core: re-enrich selection
- `t9` — verb registry and CLI verbs: enrich, search, get, transcript, recordings, actions (+ mcp/web entry stubs)
- `t10` — pocketsync mcp: stdio MCP server over the verb registry
- `t11` — pocketsync web: loopback server, Access-gated JSON API, RAG chat with citations, single page
- `t12` — docs, harness files and deploy unit
- `t13` — live deploy and acceptance probes

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `chat_model`, `web_port`, `access_team_domain`, `access_aud`, `site_origin` in `pocketsync/config.py` (merge 0457f76) |
| `t2` | delivered | `GatewayClient.embed(prompt_name=)` and `.chat()` (thinking off, no tools, think blocks stripped) (537ad19) |
| `t3` | delivered | new filter fields; in-place update, plus a drop+recreate fallback added after the live upgrade found atlas-local rejects `updateSearchIndex` for vector indexes |
| `t4` | delivered | `requeue(ids, mode)` with generation, generation-aware CAS, `set_deferred`, due-filter in `pending_enrichment` (914674f) |
| `t5` | delivered | `pocketsync/web/access.py` cited from culture-rules (feeb5f4) |
| `t6` | delivered | modes, generation CAS, persisted deferrals, `pocketsync.recording.reclassified`, `status.enrichment_deferred` (9d0bb87) |
| `t7` | delivered | `pocketsync/recall/{search,read}.py`; query prompt name amended to lowercase `query` after the live e2e run (e651a57) |
| `t8` | delivered | `pocketsync/recall/enrich.py` `plan_enrich` (65e2983) |
| `t9` | delivered | `pocketsync/verbs.py` registry; CLI verbs enrich/search/get/transcript/recordings/actions; mcp/web entry modules (995a3c5) |
| `t10` | delivered | `pocketsync/mcp/` stdio server over the registry, `pocketsync[mcp]` extra, parity MCP half (1059d66) |
| `t11` | delivered | `pocketsync/web/{server,chat}.py` to `docs/web-api.md`; the page itself delivered by the main agent under `d1` (addad10, 35733ad) |
| `t12` | delivered | README, CLAUDE.md, QWEN.md, AGENTS.override.md, AGENTS.colleague.md, .pi/SYSTEM.md, `deploy/systemd/pocketsync-web.service`, `docs/operations/pocketchat-culture-dev.md` (e99ed94); version 0.12.0 by the main agent |
| `t13` | partial | live on spark: Access app cc517906, tunnel ingress and CNAME for pocketchat.culture.dev, pocketsync-web unit, 0.12.0 reinstalled, live index fields updated, probes green; the 3 operator-chosen search probes are still open (PR CI and the Sonar gate are green) |

## Mid-work Decisions

- `d1` — the main agent designs the pocketchat page (pocketsync/web/static/{index.html,app.css,app.js}) with the frontend-design skill and a web API contract doc while wave 2 runs; t11 implements the server/API to that contract and does not author the page — user request: 'let's design, while agents work, that amazing chat experience' with the frontend-design and playwright plugins
- pending approval (not yet a decision): `d2`, query embeddings use `prompt_name` `query`, not `Query` (the live gateway returns 400 for `Query`; `Document` and `document` give identical vectors)
- The page's motion layer (waveform hero, word-by-word answers, citation fly-to-reader, view transitions) was added on the user's request "needs ... to be dynamic, animated, smooth, and feeling of wow", within `d1`.
- vindex falls back to drop and re-create when atlas-local rejects an in-place vector index update, found on the live upgrade; covered by `tests/test_vindex.py::test_stale_index_is_recreated_when_in_place_update_is_rejected` and the atlas-local integration test.
- The web API accepts singular and plural spellings for repeatable params (`label`/`labels`, `id`/`ids`) so the page and the verbs agree; documented in `docs/web-api.md`.
- `Runtime._once` is serialized by a re-entrant lock for the threaded web server (t11 reported the race).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|------------------------|-----------------|
| `t11` (`d1`) | user request: 'let's design, while agents work, that amazing chat experience' with the frontend-design and playwright plugins | `acceptable` |
| `t7` (`d2`, proposed) | live gateway rejects `prompt_name` `Query`; lowercase `query` used | `acceptable` |
| `t3` | in-place update is impossible on atlas-local for vector indexes; drop and re-create fallback added (seconds of unavailability per index) | `acceptable` |
| `t13` | the 3 operator-chosen probe questions of `c28` were not run (they need the operator to read their own content) | `needs-follow-up` |

## Evidence

- tests: `uv run pytest -n auto --cov=pocketsync` passed: 914 passed, 7 skipped, coverage 94% (35733ad)
- tests: `POCKETSYNC_IT_MONGO=1 pytest tests/integration/test_atlas_local.py` passed: 6 passed against atlas-local (throwaway db)
- lint: black, isort, flake8, bandit, `scripts/scan-secrets.py`, markdownlint, `teken cli doctor . --strict`, `harness-smoke --stage config` all pass
- review: `ask-colleague review` (task c45c715972d9) found no blocking findings
- end-to-end: real `pocketsync web`, real gateway (Qwen, gemma2-embed) and atlas-local with synthetic recordings in a throwaway db, driven in Playwright
- live probes (status codes, ids and counts only): evidence e82 to e87
- commits: `387f6a3..35733ad` on `feat/talk-to-recordings`
- PRs / issues: PR #11 (CI green, SonarCloud gate OK, 0 open issues after fixing 119 new issues and a scanner parse error, evidence e88); closes #8, #9, #10

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| `c1` — Talk to my Pocket data: on-demand re-enrichment (pocketsync enrich), an MCP server (pocketsync mcp) and a private chat site (pocketsync web at recall.culture.dev) — one retrieval/enrich core, three surfaces with CLI↔MCP↔site parity | pending adjudication | `tests/test_surface_parity.py::test_cli_calls_the_shared_core_function` (run 2026-10-16) |
| `c2` — pocketsync mcp uses the official mcp SDK low-level Server over stdio behind a pocketsync\[mcp\] extra (mcp>=1.2,<2), lazily imported (ServerExtraMissing -> CliError exit 2 with an install hint), stdout reserved for protocol — mirroring culture-rules | pending adjudication | `tests/test_mcp.py::test_importing_the_package_does_not_import_mcp` (run 2026-10-16) |
| `c3` — one verb registry drives CLI, MCP tools and site actions; a data-driven parity test asserts every registry verb exists as a CLI command and an MCP tool (and no extra tools), mutating verbs expose apply (default false) and dry-run otherwise | pending adjudication | `tests/test_surface_parity.py::test_surface_in_parity_with_registry` (run 2026-10-16) |
| `c4` — pocketsync enrich selects recordings (--all/--since/--until/--id/--label/--status/--only) and re-queues them by flipping enrichment.status to pending with attempts=0 and clearing error/`next_attempt_at`, dry-run unless --apply; the existing EnrichmentWorker does the work (no second enrichment path) | pending adjudication | `tests/test_recall_enrich.py::test_dry_run_changes_nothing` (run 2026-10-16) |
| `c5` — --only classify re-runs the decider on stored chunk/summary text and writes only labels/filters/`taxonomy_version`, keeping vectors; --only embed re-embeds keeping labels — the worker gains a per-doc enrichment.mode (full\|classify\|embed) because `_build_payload` today does embed+classify all-or-nothing | pending adjudication | `tests/test_enrich_worker.py::test_classify_mode_reuses_stored_text_and_keeps_vectors` (run 2026-10-16) |
| `c6` — gateway deferrals are persisted: when EnrichmentUnavailable defers a pending doc, the worker writes enrichment.`deferred_reason` and enrichment.`next_attempt_at` so status/doctor can say why the backlog is not draining and when it retries (today the reason lives only in the in-memory `_retry` map) | pending adjudication | `tests/test_enrich_worker.py::test_unavailable_persists_the_deferral_and_survives_restart` (run 2026-10-16) |
| `c7` — a re-enrich that changes labels without changing `content_hash` emits one pocketsync.recording.reclassified event via the outbox (today Outbox dedupes on (id, `content_hash`) so reclassify publishes nothing) | pending adjudication | `tests/test_enrich_worker.py::test_label_change_with_same_content_emits_one_reclassified_event` (run 2026-10-16) |
| `c8` — GatewayClient gains a chat-completions call (/chat/completions) with the model from a new non-secret setting `POCKETSYNC_CHAT_MODEL` defaulting to unsloth/Qwen3.8-27B-NVFP4, so the chat lane can be switched (e.g. to Nemotron 3.5 Super VL) without code changes | pending adjudication | `tests/test_config.py::test_talk_settings_defaults` (run 2026-10-16) |
| `c9` — pocketsync enrich replaces the narrow reembed selector but reembed stays as an alias/compat verb (its JSON keys `dry_run`/model/dims/ids/count/updated are an existing contract) | pending adjudication | `tests/test_cli_status_reembed.py::test_reembed_dry_run_lists_ids_and_writes_nothing` (run 2026-10-16) |
| `c10` — a read-only retrieval core (pocketsync/recall/ or similar) runs $vectorSearch over `recordings_chunk_vec` (root filters in parentFilter) and `recordings_summary_vec` (filters in filter), embeds the query via the gateway with `prompt_name` Query (embed() hardcodes Document today), excludes `deleted_in_pocket` and `merged_into` docs, and returns ids, titles, `recording_at`, score and the best-matching chunks with start/end offsets | pending adjudication | `tests/test_recall_search.py::test_label_filter_goes_in_parent_filter_for_chunks_and_filter_for_summary` (run 2026-10-16) |
| `c11` — the vector index filter fields grow to include enrichment.filters.sensitive and enrichment.filters.`has_action_items` (today `FILTER_PATHS` is labels/`deleted_in_pocket`/`recording_at` only, so sensitive cannot be filtered inside $vectorSearch); vindex updates an existing index definition in place instead of only creating missing ones | pending adjudication | `tests/test_vindex.py::test_stale_definition_is_updated_in_place` (run 2026-10-16) |
| `c12` — sensitive recordings are searchable by default on every read surface (user decision: internal network); an opt-in `exclude_sensitive` filter drops docs whose enrichment.filters.sensitive is at or above threshold; absent scores are treated as not sensitive | pending adjudication | `tests/test_recall_search.py::test_exclude_sensitive_is_opt_in_and_keeps_absent_scores` (run 2026-10-16) |
| `c14` — $vectorSearch is unit-tested through an injectable search seam (fake returns canned hits) since mongomock cannot run it; real behaviour (parentFilter on `recording_at`, `deleted_in_pocket`, sensitive) is covered in the opt-in tests/integration/`test_atlas_local.py` | pending adjudication | `tests/test_recall_search.py::test_results_merge_by_recording_and_never_include_vectors` (run 2026-10-16) |
| `c15` — read verbs get CLI parity with the MCP tools: pocketsync search, get, transcript, recordings (list), actions — all --json, all read-only, all runtime verbs under `_grant` re-exec | pending adjudication | `tests/test_surface_parity.py::test_cli_json_equals_registry_invoke` (run 2026-10-16) |
| `c16` — pocketsync web is a stdlib ThreadingHTTPServer (like webhook/server.py) bound to loopback only on a new setting `POCKETSYNC_WEB_PORT` (default 8788), serving one self-contained HTML/JS page plus a JSON API (/api/chat, /api/search, /api/recordings, /api/recordings/{id}, /api/actions, /api/enrich) that calls the same retrieval/enrich core as CLI and MCP — no extra dependency and no second retrieval implementation | pending adjudication | `tests/test_web_server.py::test_refuses_non_loopback_bind` (run 2026-10-16) |
| `c17` — chat answers are generated by the configured chat model from retrieved chunks only, and every answer cites recordings with ids and start/end timestamps; clicking a citation opens that recording's transcript at the timestamp; a per-recording and per-result-set Re-enrich button calls the enrich operation (dry-run preview, then apply) | pending adjudication | `tests/test_web_chat.py::test_every_citation_references_a_retrieved_hit_and_in_range_time` (run 2026-10-16) |
| `c18` — deploy adds deploy/systemd/pocketsync-web.service (grant run --inject, like pocketsync-serve) and a runbook section adding pocketchat.culture.dev as a second ingress hostname on the existing remote-managed pocketsync-culture-dev tunnel with a Cloudflare Access app allowing only the operator email and no Bypass; web verifies the Access JWT (team domain + audience settings) on every request | pending adjudication | `tests/test_deploy_units.py::test_web_unit_injects_only_its_secrets_and_sets_access_env` (run 2026-10-16) |
| `c19` — docs: CLAUDE.md, README.md and QWEN.md amend the 'no UI' non-goal to 'UI only as a thin client over the CLI/MCP surface'; all four harness files plus README, explain catalog, learn (text+JSON), overview.`_VERBS`, tests/`test_cli_surface.py` `NEW_VERBS` and `_grant`.`RUNTIME_VERBS` list the new verbs | pending adjudication | `tests/test_cli_surface.py::test_every_command_has_json_flag` (run 2026-10-16) |
| `c20` — the sync contract is unchanged: file layout, landing schema, `content_hash` and the pocketsync.recording.enriched event stay as they are; new surfaces only read recordings and, for enrich, write enrichment.\* fields through the sink's compare-and-set | pending adjudication | `tests/test_sync.py` (run 2026-10-16) |
| `c21` — the webhook Bypass stays exactly pocketsync.culture.dev/hooks/pocket; pocketchat.culture.dev gets no Bypass; web and webhook servers refuse non-loopback binds | pending adjudication | `tests/test_web_server.py::test_refuses_non_loopback_bind` (run 2026-10-16) |
| `c22` — core stays dependency-free: mcp SDK only behind pocketsync\[mcp\]; web uses stdlib only; no numpy; the lobes gateway and other Mongo containers on spark are not modified | pending adjudication | `tests/test_surface_parity.py::test_verbs_module_imports_no_third_party_code` (run 2026-10-16) |
| `c28` — on the live deploy: pocketsync search returns the matching recording first for >= 3 of 3 operator-chosen probe questions; a chat answer cites >= 1 recording with a timestamp; pocketsync enrich --status error --apply drains to 0 retrying within one worker cycle when the gateway is up; recall.culture.dev returns 302 unauthenticated | pending adjudication | `live: chat.answer on the real stack` (run 2026-10-16) |
| `c29` — test suite: CLI↔MCP parity test passes for 100% of registry verbs, the full suite stays green and coverage stays >= 60%, teken cli doctor --strict passes, Sonar new issues = 0 | pending adjudication | `CI: uv run pytest -n auto --cov` (run 2026-10-16) |
| `c33` — re-queue is race-safe against an in-flight worker: enrich --apply bumps enrichment.generation and the worker's `set_enrichment` / `set_enrichment_error` CAS matches `content_hash` AND the generation it claimed, so a requeue issued mid-processing is not overwritten to done (today the CAS is on `content_hash` only, sinks/mongo.py `set_enrichment`) | pending adjudication | `tests/test_enrich_worker.py::test_requeue_during_processing_is_not_overwritten` (run 2026-10-16) |
| `c34` — web output is XSS-safe and the mutating API is CSRF-safe: transcript/summary/model text is rendered only via textContent (never innerHTML), responses carry a strict Content-Security-Policy (no inline script from data, default-src self), and POST /api/enrich and /api/chat require Content-Type application/json plus an Origin header equal to the configured site origin | pending adjudication | `tests/test_web_static.py::test_app_js_has_no_html_or_code_sinks` (run 2026-10-16) |
| `c35` — the chat model and the MCP surface get no tools or side effects driven by model output: retrieved transcript text is untrusted context (prompt-injection), the chat model only produces text, and enrich runs only from an explicit operator click or CLI/MCP call with apply | pending adjudication | `tests/test_web_chat.py::test_prompt_marks_sources_as_untrusted_quoted_data` (run 2026-10-16) |
| `c36` — the Access JWT check fails closed: pocketsync web refuses to start without `POCKETSYNC_ACCESS_TEAM_DOMAIN` and `POCKETSYNC_ACCESS_AUD` unless run with an explicit --no-access-check (loopback dev only, loud warning); JWKS (https://{team}.cloudflareaccess.com/cdn-cgi/access/certs) is fetched lazily, cached, refreshed on unknown kid, and an unreachable JWKS yields 503 not allow | pending adjudication | `tests/test_web_server.py::test_bad_or_missing_jwt_is_403` (run 2026-10-16) |
| `c37` — the chat request disables reasoning output for thinking models (`chat_template_kwargs` `enable_thinking` false and strips any leftover think block), caps the retrieved context (top 8 chunks, ~6000 chars) and the answer length, and returns 503 with the retrieved citations still listed when the chat lane is down | pending adjudication | `tests/test_gateway_chat.py::test_chat_strips_think` (run 2026-10-16) |
| `c38` — when query embedding fails (embed lane down) search degrades to a case-insensitive keyword match over title, summary and chunk text with recording-level filters, and says so in the result (mode: keyword) | pending adjudication | `tests/test_recall_search.py::test_embed_failure_falls_back_to_keyword_mode` (run 2026-10-16) |
| `c39` — web access logging records method, path without query string, status and duration only — never question text, request bodies or answers | pending adjudication | `tests/test_web_server.py::test_access_log_has_method_path_status_duration_only` (run 2026-10-16) |

Lapse ledger evidence:

pending approval (not yet evidence): `l1`, `l2`

## Remaining Work / Follow-up

- `c28`: the 3 operator-chosen probe questions; the operator asks three questions on pocketchat.culture.dev whose answer recording they know
- adjudication by the operator: `d2`, lapses `l1`/`l2`, evidence e1 to e87, deltas b5 to b8; reject the mis-linked deltas b1 to b3 (wrong evidence ids; b5 to b8 supersede them); keep or reject b4 (no evidence attached)
- a taxonomy change still re-queues full enrichment, not classify-only (t6 note); follow-up together with #6
- colleague low suggestions: document the `has_action_items=false` `$not` filter shape; keep any future mutating verb behind the web POST guard
- t19 acceptance numbers on #3 (p95 latency, re-create) are unchanged; operator
