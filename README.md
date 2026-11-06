# brain — a self-hosted second brain for agents

A small server that holds your notes and automatically ingested history in one
place, and lets **any MCP-capable agent** (Claude, self-hosted agents) search,
read and write it from any device — no git syncing.

```
 laptop / desktop                          Kubernetes (namespace: brain)
┌────────────────────┐   HTTPS + token    ┌─────────────────────────────────────┐
│ brain push (cron)  │ ─────────────────► │ pod                                  │
│  • Claude sessions │   POST /api/items  │  ┌─────────────┐   ┌──────────────┐ │
│  • git history     │                    │  │ server      │   │ worker       │ │
│  • markdown vault  │                    │  │ MCP  /mcp   │   │ connectors   │ │
└────────────────────┘                    │  │ REST /api   │   │ enrichment   │ │
 Claude Code / Desktop / agents           │  │ UI   /      │   │ prune/backup │ │
┌────────────────────┐   MCP over HTTP    │  └──────┬──────┘   └──────┬───────┘ │
│ brain_search/read/ │ ─────────────────► │         └─── SQLite+FTS5 ──┘         │
│ write/append/...   │                    │             PVC /data                │
└────────────────────┘                    └─────────────────────────────────────┘
```

**How it works** (the ideas from the video, minus the hype)

| Idea | Here |
|---|---|
| Consistent pipelines in | Connectors pull only what's new, stamp it with a date, upsert by `(source, source_ref)` — idempotent |
| Cheap model writes summaries | Each item gets a `summary`, `questions` (how you'd search for it) and `atoms` (exact errors, fixes, commands, decisions + why). Heuristic by default; optional LLM |
| Plain-text search, no vector DB | SQLite FTS5 with BM25, columns weighted toward the enriched fields |
| Recency weighting | Score × exponential decay (90-day half-life, floor 0.35). Pinned items are exempt |
| Keep it fresh | Worker records every run; `brain_status` / `/api/status` flag failing or silent connectors |
| Sweep stale stuff | Daily: archive (never delete) old, unpinned, never-read items from *automated* sources. Hand-written notes are never auto-archived |

## Start here (5 minutes, with Docker)

You need Docker and (optionally) Claude Code. Nothing here is tied to anyone's accounts or data.

```bash
git clone <this repo> brain && cd brain
cp .env.example .env
echo "BRAIN_TOKEN=$(openssl rand -hex 32)" >> .env     # or edit .env by hand
docker compose up -d --build
open http://localhost:8080                                # paste the token from .env when asked
```

**Load the demo notes** (7 example notes so you can try search right away):

```bash
docker compose cp examples/demo-notes server:/tmp/demo
docker compose exec server brain import-md /tmp/demo --source demo
```

Try it in the UI: search `docker permission`, `vlan`, `postgres`. Delete the demo notes when you start your own
(each has source `demo`).

**Connect Claude Code** (any machine that can reach the server):

```bash
export BRAIN_TOKEN=...   # the value in your .env
claude mcp add --transport http brain http://localhost:8080/mcp --header "Authorization: Bearer $BRAIN_TOKEN"
```

Then start a new Claude Code chat and say *"search my brain for docker permission"* or *"remember that my router is 192.168.10.1"*.
The server sends usage instructions to the agent on connect, so it knows when to search and when to skip the brain
(general questions). For extra reinforcement paste `examples/CLAUDE.md` into your global `~/.claude/CLAUDE.md`.

**claude.ai (web/mobile):** custom connectors are called from Anthropic's cloud, so the server needs a public HTTPS URL
and OAuth (a static token is not enough). Put the server behind HTTPS, then follow "Configuration" below
(`BRAIN_OAUTH_*`) with any OIDC provider (Pocket ID, Authentik, Keycloak, Auth0...). Optional; Claude Code works without it.

**Make it smarter later (optional):**
- `brain push` ingests git history and notes from your laptop (`examples/device-connectors.yaml`).
- `BRAIN_ENRICH=claude-cli` writes better summaries using your own Claude subscription (set `BRAIN_ENRICH_BIN`,
  default `claude`); `brain enrich-remote --limit 25` enriches pending items in small batches. These use your plan quota,
  so keep batches small. Default is free local heuristics.
- Back up the `brain-data` volume (it also keeps 7 daily copies under `/data/backups`).

**Safety:** never put passwords or tokens in notes. Do not expose the server without HTTPS. Do not mix work and
personal data in one instance.

---

## Quickstart (local, 1 minute)

```bash
pip install .                       # Python 3.11+
export BRAIN_DB=./brain.db BRAIN_TOKEN=$(openssl rand -hex 32)
brain add "Router VLANs" --body "vlan 20 = iot, vlan 30 = guests" --kind reference
brain search vlan iot
brain serve --port 8080             # UI at http://localhost:8080  (paste the token)
```

## Deploy to Kubernetes

```bash
# 1. build + push the image to a registry your cluster can pull from
docker build -t REGISTRY/brain:0.1.0 . && docker push REGISTRY/brain:0.1.0
# 2. set that image in k8s/kustomization.yaml (replace REGISTRY) and a storageClassName in k8s/brain.yaml
# 3. create the namespace and secret, then apply
kubectl apply -f k8s/namespace.yaml
kubectl -n brain create secret generic brain-secrets --from-literal=BRAIN_TOKEN="$(openssl rand -hex 32)"
kubectl apply -k k8s/
# 4. try it without any ingress
kubectl -n brain port-forward svc/brain 8080:80
curl localhost:8080/healthz
```

Design notes: one replica with `Recreate` (SQLite is single-host). The **server and
worker share the volume as two containers in one pod**, which is what makes SQLite
WAL safe. **Do not use an NFS/SMB-backed volume for the database.** Use local-path,
Longhorn, Ceph RBD or similar. Keep the Service private (tailnet / LAN / VPN):
the bearer token is the only gate. `k8s/ingress.example.yaml` is opt-in.

## Connect agents

* **Claude Code** (on a machine that can reach the server):
  `claude mcp add --transport http brain https://YOUR-HOST/mcp --header "Authorization: Bearer $BRAIN_TOKEN"`
  (check `claude mcp add --help` for the current flag names), then paste
  `examples/CLAUDE.md` into your global instructions so agents search first and
  write back durable facts.
* **Self-hosted / other agents:** any MCP client over streamable HTTP, or plain REST:
  `GET /api/search?q=…`, `POST /api/items`, `GET /api/items/{id}`, `PUT`, `DELETE` (archive),
  `POST /api/items/{id}/append`, `GET /api/status`. Header: `Authorization: Bearer <token>`.
* **claude.ai web / mobile app:** custom connectors are called from Anthropic's cloud, so
  they need a publicly reachable HTTPS URL and whatever auth scheme claude.ai supports for
  custom connectors (I have not verified that a static bearer token is accepted; OAuth may
  be required). Treat this as an open item. Until then, use Claude Code / Desktop on
  devices that can reach the server.

Tools exposed: `brain_search`, `brain_read`, `brain_write`, `brain_append`,
`brain_recent`, `brain_status`. The server also sends usage instructions to the client.

## Getting data in

* **From a device** (things that only exist on your laptop): run the same connectors there and push:
  ```bash
  export BRAIN_URL=https://YOUR-HOST BRAIN_TOKEN=...
  brain push --config examples/device-connectors.yaml      # Claude sessions, git repos, a markdown vault
  # every 15 min:   */15 * * * *  brain push --config ~/.brain/connectors.yaml
  ```
  Cursor state stays in `~/.brain/state.json`; failures keep the cursor so it retries.
* **From the cluster:** edit the ConfigMap's `connectors.yaml` (a drop-folder `markdown_dir` is preconfigured).
* **One-off import** (e.g. your old project notes): `brain import-md ./old-notes --source notion-export`.
* **Export anytime** as plain markdown: `brain export-md ./out` (portable; no lock-in).
* **Write your own connector:** subclass `Connector` in `src/brain/connectors.py`, yield dicts of item fields from
  `collect(cursor)`, set `self.cursor`. Gmail/Slack/calendar are natural next ones — use read-only credentials.

Connector types today: `markdown_dir`, `claude_sessions`, `git_log`.

## Enrichment and privacy

Default is **heuristic** (no model, nothing leaves your network). To use a model:

| `BRAIN_ENRICH` | needs | notes |
|---|---|---|
| `heuristic` (default) | — | lead text as summary; error/command lines as atoms |
| `openai` | `BRAIN_ENRICH_BASE_URL`, `BRAIN_ENRICH_MODEL` | any OpenAI-compatible server (Ollama, vLLM, llama.cpp) — stays on your network |
| `claude-cli` | the `claude` CLI logged in on this machine | uses your Claude subscription, no API key (run via `brain enrich-remote` from your laptop); `BRAIN_ENRICH_BIN`, `BRAIN_ENRICH_MODEL` (default `sonnet`) |
| `anthropic` | `ANTHROPIC_API_KEY` | sends item text to the API; model via `BRAIN_ENRICH_MODEL` (default `claude-haiku-5-5`) |

`BRAIN_ENRICH_SCOPES=personal` restricts the model to those scopes; other items use the heuristic.
If the model fails, enrichment falls back to the heuristic so ingestion never stalls.

**Work data:** don't ingest it here. If you ever do, run a **separate instance** (separate DB, token and namespace)
rather than relying on `scope` — scopes are a filter, not a security boundary. Don't store secrets in notes;
the agent instructions tell agents the same.

## Configuration

| env | default | |
|---|---|---|
| `BRAIN_TOKEN` | required | bearer token (server refuses to start without it unless `BRAIN_ALLOW_NO_AUTH=1`) |
| `BRAIN_DB` | `/data/brain.db` | SQLite path |
| `BRAIN_CONFIG` | — | connectors / maintenance YAML |
| `BRAIN_ALLOWED_HOSTS` | off | comma list; enables Host/Origin pinning |
| `BRAIN_OAUTH_ISSUER` | off | OIDC issuer (e.g. Pocket ID). Enables OAuth access tokens next to the static token; needs the next three |
| `BRAIN_PUBLIC_URL` | — | public base URL, e.g. `https://brain.example.com` (resource = `<url>/mcp`) |
| `BRAIN_OAUTH_CLIENT_IDS` | — | comma list of accepted `aud` values (the claude.ai client id) |
| `BRAIN_OAUTH_SUBJECTS` | — | comma list of accepted `sub` values (your user id; everyone else is rejected) |
| `BRAIN_BACKUP_DIR` | — | daily consistent backups, 7 kept |
| `BRAIN_ENRICH*` | heuristic | see above |

Config YAML keys: `connectors[]` (`name`, `type`, `interval_minutes`, `scope`, `options`),
`enrich_batch`, `poll_seconds`, `maintenance` (`prune_sources`, `archive_after_days`, `backup_dir`, `backup_keep`).

**Restore:** stop the pod, copy a `brain-*.db` from `/data/backups` over `/data/brain.db`, start.
**Pruning dry run:** `brain maintain --sources claude-sessions git` (add `--apply` to archive).

## Tests

```bash
pip install -e '.[dev]' && pytest         # 52 tests: store, search/ranking, connectors, enrichment,
                                          # auth, REST, MCP over HTTP, device push, 3-process concurrency, CLI
```

## Status / known gaps (alpha)

* Verified here: unit/integration tests, a two-process server+worker simulation, and the UI in headless Chrome.
* **Not verified here:** `docker build` (no registry/network in the build sandbox), applying the manifests to a real
  cluster (YAML validated only), real Anthropic/Ollama calls (tested against local fakes that check request shape),
  and the Claude session log parser against *your* real files (it is tolerant of unknown lines, but check the first run).
* Single user, single token, no OAuth. Search is keyword-only by design; add embeddings later only if BM25 misses things.
* No Gmail/Slack/calendar connectors yet.
