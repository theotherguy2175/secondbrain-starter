# AGENTS.md: guide for AI coding agents working on this repo

`brain` is a self-hosted "second brain" for AI agents: a Python server that stores notes in SQLite (FTS5/BM25 search with
recency weighting), exposes them over **MCP** (`/mcp`), a small **REST API** (`/api`) and a single-file **web UI** (`/`),
and ingests more data through **connectors**. Read `README.md` first for setup and configuration; this file is the map.

## Commands

```bash
pip install -e '.[dev]'      # Python 3.11+
pytest -q                    # ~75 tests, ~10s, no network or external services needed
export BRAIN_DB=./brain.db BRAIN_TOKEN=dev && brain serve --port 8080   # UI + API + MCP at http://localhost:8080
brain import-md examples/demo-notes --source demo                        # load demo data
docker compose up -d --build                                             # the turnkey path (see README)
```

## Layout (`src/brain/`)

| File | Role |
|---|---|
| `store.py` | **Single source of truth.** One `items` table + FTS5 index; `search` (BM25 then recency decay, pinned exempt), `put` (upsert by `(source, source_ref)`), `set_enrichment`, `needing_llm`, `list_items`. All SQL lives here. |
| `server.py` | Starlette app: auth (static bearer token, optional OAuth/OIDC JWTs), REST routes, the **MCP tools** (`brain_search`, `brain_read`, `brain_write`, `brain_append`, `brain_recent`, `brain_status`) and `MCP_INSTRUCTIONS` (sent to every MCP client; it is the routing guidance for agents). |
| `enrich.py` | Turns an item into `summary` + `questions` (how you'd search for it) + `atoms` (exact errors/commands/decisions). Enrichers: `HeuristicEnricher` (default, no model), OpenAI-compatible, Anthropic, `ClaudeCliEnricher`. |
| `connectors.py` | `markdown_dir`, `claude_sessions`, `git_log`. A connector yields item dicts from `collect(cursor)`; secrets are stripped by `redact.py` before anything leaves the device. |
| `sink.py` | Where connectors write: `LocalSink` (direct to the store) or `HttpSink` (push to a remote server). |
| `worker.py` | Background loop in the server pod: runs connectors, enrichment, maintenance. |
| `maintenance.py` | Health report, pruning (archive, never delete), consistent daily backups. |
| `notes_io.py` | Markdown <-> item conversion (frontmatter). Used by import/export. |
| `redact.py` | Regex secret scrubbing (keys, JWTs, passwords, URL credentials). Extend it when you add a source. |
| `cli.py` | `brain serve / worker / push / enrich-remote / search / add / import-md / export-md / maintain / stats`. |
| `ui/index.html` | The whole web UI: one file, no build step, no CDN (built-in markdown renderer that escapes HTML first). |

`tests/` mirrors this layout (`test_store.py`, `test_server.py`, `test_oauth.py`, ...). `k8s/` and `docker-compose.yml` are deployment only.

## Invariants: do not break these

- **Single writer.** SQLite WAL with one server+worker pod/volume. Never add a second writer process on another host or an NFS-backed DB.
- **Idempotent ingestion.** Connectors upsert by `(source, source_ref)`; re-running must not duplicate.
- **Never delete automatically.** Maintenance archives; hand-written notes (source `agent`/`ui`/`api`) are never auto-archived.
- **Enrichment levels:** `enriched` 0 = pending, 1 = heuristic, 2 = LLM. `set_enrichment` never downgrades level 2.
- **No secrets in storage.** Redact on ingest; agents are told never to write secrets. Keep `redact.py` covering new sources.
- **Auth stays on.** The server refuses to start without `BRAIN_TOKEN` (unless `BRAIN_ALLOW_NO_AUTH=1`). Public paths are only `/`, `/ui`, `/healthz`, `/ui-config`.
- **UI stays one file** with no external script/CDN dependencies; escape all user text before inserting HTML.

## Common tasks

- **Add a connector:** subclass `Connector` in `connectors.py` (`type = "..."`, implement `collect(cursor)`, set `self.cursor`), register it in the type map near the bottom, redact text, add a test in `tests/test_pipeline.py`.
- **Add an MCP tool or REST route:** edit `server.py` (tools in `build_mcp`, routes in the `Route(...)` list), keep the response small (agents pay per token), add a test in `tests/test_server.py`. Update `MCP_INSTRUCTIONS` if agent behavior should change.
- **Change search ranking:** `store.search`; verify with `tests/test_store.py`.
- **Add an enricher:** implement `enrich(item) -> {"summary","questions","atoms"}` in `enrich.py` and wire it in `get_enricher` (selected by `BRAIN_ENRICH`).

## Conventions

- Keep dependencies minimal (`pyproject.toml` lists them all; stdlib `urllib` is used for HTTP on purpose).
- Tests must pass offline. Use the fixtures in `tests/conftest.py`.
- If a call goes through Cloudflare, set a custom `User-Agent`; Python's default is blocked (error 1010).
- Do not commit `.env`, `*.db`, or real hostnames/tokens.
