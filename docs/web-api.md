# pocketchat web API

`pocketsync web` serves the pocketchat page and a small JSON API on loopback.
Cloudflare Access sits in front of it at `pocketchat.culture.dev`, and the
server verifies the Access JWT on every request. The page in
`pocketsync/web/static/` talks only to the endpoints below, and every endpoint
calls a verb from `pocketsync/verbs.py` through `Verb.invoke`, the same entry
point the CLI and the MCP server use.

## Static files

| Path | Serves |
|------|--------|
| `GET /` | `static/index.html` |
| `GET /static/app.css` | `static/app.css` |
| `GET /static/app.js` | `static/app.js` |
| `GET /static/icon.svg` | `static/icon.svg` |

Every response carries:

```text
Content-Security-Policy: default-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Cache-Control: no-store            (API responses only)
```

The page has no inline script or style and loads nothing from another origin.

## Conventions

- Request and response bodies are JSON (`Content-Type: application/json`).
- A `POST` without `Content-Type: application/json`, or whose `Origin` is not
  the configured `site_origin`, gets `403`.
- Errors always have this shape, with the HTTP status set to match:

  ```json
  {"error": {"code": "not_found", "message": "no recording r123", "hint": "check the id"}}
  ```

  | Status | Codes |
  |--------|-------|
  | 400 | `bad_request` (a `CliError` with exit 1 from a verb, bad JSON, missing length) |
  | 403 | `forbidden` (Access JWT, Origin or Content-Type check failed) |
  | 404 | `not_found` |
  | 405 | `method_not_allowed` |
  | 413 | `too_large` (request body over 64 KiB) |
  | 503 | `unavailable` (JWKS, Mongo or a gateway lane down), `chat_unavailable` |
  | 500 | `internal` (never carries a traceback) |

- Times are ISO 8601 strings (`recording_at`). Offsets inside a recording
  (`start`, `end`) are seconds as numbers.
- Responses never contain vectors.

## Endpoints

### `GET /api/health`

```json
{"ok": true, "version": "0.12.0", "chat_model": "unsloth/Qwen3.8-27B-NVFP4"}
```

### `GET /api/recordings?since=&until=&label=&limit=`

This is the `recordings` verb.

```json
{"count": 2, "recordings": [
  {"id": "r1", "title": "Roadmap sync", "recording_at": "2026-10-15T09:30:00Z",
   "duration": 1830.5, "labels": ["meeting"], "action_items": 3, "enrichment_status": "done"}
]}
```

### `GET /api/recordings/{id}`

This is the `get` verb. It returns metadata, `summary` (markdown text),
`action_items [{text, done, owner}]`, `labels`, `filters`,
`enrichment_status`, `duration`, `speakers` and `segment_count`.

### `GET /api/recordings/{id}/transcript?start=&end=`

This is the `transcript` verb.

```json
{"id": "r1", "title": "Roadmap sync", "recording_at": "...", "start": null, "end": null,
 "segments": [{"start": 0.0, "end": 4.2, "text": "Okay, let's start.", "speaker": "S1", "speaker_name": "Dana"}]}
```

### `GET /api/actions?open_only=true&limit=`

This is the `actions` verb.

```json
{"count": 1, "action_items": [
  {"recording_id": "r1", "title": "Roadmap sync", "recording_at": "...", "index": 0,
   "text": "Send the draft to legal", "done": false, "owner": "Dana"}
]}
```

### `POST /api/search`

The request body follows the `search` verb's schema. Repeatable verb parameters may be
sent singular or plural (`label` or `labels`, `id` or `ids`); sending both spellings is a
`400`.

```json
{"query": "pricing decision", "labels": ["meeting"], "since": "2026-10-01", "limit": 10}
```

The response is the `search` verb's output. `mode` is `vector`, or `keyword`
together with a `reason` when query embedding is down.

```json
{"mode": "vector", "query": "pricing decision", "count": 1, "results": [
  {"id": "r1", "title": "Roadmap sync", "recording_at": "...", "labels": ["meeting"], "score": 0.81,
   "chunks": [{"index": 4, "start": 612.0, "end": 655.5, "text": "...", "score": 0.84}]}
]}
```

### `POST /api/chat`

The request body:

```json
{"question": "What did we decide about pricing?",
 "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
 "labels": [], "since": null, "until": null, "exclude_sensitive": false}
```

- `history` holds earlier turns of this browser session, oldest first. The
  server uses at most the last 6, and they are never stored server-side.
- The server retrieves with `search`, keeps the top 8 chunks (about 6000
  characters at most), and numbers them as sources `[1]`..`[n]`. It then asks
  the chat model to answer only from those sources and to cite them with
  bracketed numbers.

The response:

```json
{"answer": "You agreed to keep the starter tier at $9 [1] and revisit in Q1 [2].",
 "citations": [
   {"n": 1, "id": "r1", "title": "Roadmap sync", "recording_at": "...",
    "start": 612.0, "end": 655.5, "text": "...the chunk text..."}
 ],
 "retrieval": {"mode": "vector", "count": 3},
 "model": "unsloth/Qwen3.8-27B-NVFP4"}
```

- `citations` lists only sources the answer actually cites, in `n` order. The
  server drops any `[k]` in the answer that names no source.
- With zero retrieval hits there is no model call. The response is
  `{"answer": "", "citations": [], "retrieval": {"mode": ..., "count": 0}}`,
  and the page says nothing matched.
- When the chat lane is down, the response is a `503` with code
  `chat_unavailable`. The body still includes `citations` (every retrieved
  source, numbered in retrieval order, not a cited subset) and `retrieval`, so
  the page can show what it found.

### `POST /api/enrich`

The request body follows the `enrich` verb's schema, including `apply`:

```json
{"ids": ["r1"], "only": "classify", "apply": false}
```

The response is the verb's output:

```json
{"dry_run": true, "ids": ["r1"], "count": 1, "mode": "classify",
 "estimated_gateway_calls": 1, "updated": 0}
```

The page always sends `apply: false` first, shows the preview, and sends
`apply: true` only after the operator confirms.

## Logging

Each request writes one access line: method, path without the query string,
status and duration in ms. Question text, request bodies and answers are never
logged.
