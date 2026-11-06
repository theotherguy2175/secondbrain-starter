"""`brain` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import maintenance, notes_io
from .connectors import run_specs
from .enrich import enrich_pending, get_enricher
from .sink import LocalSink, RemoteSink
from .store import Store
from .worker import load_config, loop, run_once


def _store(args) -> Store:
    return Store(args.db or os.environ.get("BRAIN_DB", "brain.db"))


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app, oauth_from_env

    store = _store(args)
    cfg = load_config(args.config or os.environ.get("BRAIN_CONFIG"))
    hosts = [h.strip() for h in os.environ.get("BRAIN_ALLOWED_HOSTS", "").split(",") if h.strip()]
    app = create_app(store, cfg.get("connectors", []), token=os.environ.get("BRAIN_TOKEN"),
                     allowed_hosts=hosts or None,
                     allow_no_auth=os.environ.get("BRAIN_ALLOW_NO_AUTH") == "1",
                     oauth=oauth_from_env(), ui_client_id=os.environ.get("BRAIN_UI_CLIENT_ID"))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(args) -> int:
    store = _store(args)
    cfg = load_config(args.config or os.environ.get("BRAIN_CONFIG"))
    if args.once:
        print(json.dumps(run_once(store, cfg), indent=2))
        return 0
    loop(store, cfg)
    return 0


def cmd_push(args) -> int:
    """Run connectors on this device and push results to a remote brain server."""
    url = args.url or os.environ.get("BRAIN_URL")
    token = args.token or os.environ.get("BRAIN_TOKEN")
    if not url or not token:
        print("need --url/BRAIN_URL and --token/BRAIN_TOKEN", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    sink = RemoteSink(url, token, state_path=args.state)
    results = run_specs(cfg.get("connectors", []), sink, only=args.only)
    print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results.values()) else 1


def cmd_enrich_remote(args) -> int:
    """Upgrade rule-based summaries on a remote brain using an LLM that runs on this device."""
    import urllib.request
    url = (args.url or os.environ.get("BRAIN_URL", "")).rstrip("/")
    token = args.token or os.environ.get("BRAIN_TOKEN")
    if not url or not token:
        print("need --url/BRAIN_URL and --token/BRAIN_TOKEN", file=sys.stderr)
        return 2
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "brain-cli/1.0"}

    def call(path, data=None):
        req = urllib.request.Request(url + path, headers=hdr, data=json.dumps(data).encode() if data else None,
                                     method="POST" if data else "GET")
        return json.load(urllib.request.urlopen(req, timeout=60))

    enricher = get_enricher({**os.environ, "BRAIN_ENRICH": args.mode, "BRAIN_ENRICH_MODEL": args.model})
    done = failed = 0
    for item in call(f"/api/pending-enrichment?limit={args.limit}"):
        try:
            out = enricher.enrich(item)
            call(f"/api/items/{item['id']}/enrichment", out)
            done += 1
            print(f"ok   {item['id']}  {item['title'][:70]}")
        except Exception as e:  # keep going; one bad item must not stop the batch
            failed += 1
            print(f"FAIL {item['id']}  {item['title'][:50]}: {e}", file=sys.stderr)
    print(json.dumps({"enriched": done, "failed": failed}))
    return 0 if not failed else 1


def cmd_search(args) -> int:
    store = _store(args)
    for h in store.search(" ".join(args.query), limit=args.limit, scope=args.scope):
        print(f"{h['score']:8.3f}  {h['id']}  [{h['kind']}/{h['source']}] {h['title']}  ({h['age_days']}d)")
        if h["snippet"]:
            print(f"          {h['snippet']}")
    return 0


def cmd_add(args) -> int:
    store = _store(args)
    body = args.body if args.body is not None else sys.stdin.read()
    print(json.dumps(store.put(title=args.title, body=body, kind=args.kind, tags=args.tags,
                               scope=args.scope, source="cli")))
    return 0


def cmd_import_md(args) -> int:
    store = _store(args)
    sink = LocalSink(store, default_scope=args.scope)
    root = Path(args.path).expanduser()
    n = 0
    for path in sorted(root.rglob("*.md")):
        fields = notes_io.file_to_fields(path, root)
        fields["source"] = args.source
        sink.put(**fields)
        n += 1
    enrich_pending(store, get_enricher(), limit=100000)
    print(f"imported {n} files")
    return 0


def cmd_export_md(args) -> int:
    n = notes_io.export_all(_store(args), args.out, include_archived=args.archived)
    print(f"exported {n} items to {args.out}")
    return 0


def cmd_maintain(args) -> int:
    store = _store(args)
    cfg = load_config(args.config or os.environ.get("BRAIN_CONFIG"))
    print(json.dumps(maintenance.health(store, cfg.get("connectors", [])), indent=2))
    print(json.dumps(maintenance.prune(store, args.archive_after_days, args.sources or None,
                                       dry_run=not args.apply)))
    if args.backup_dir:
        print("backup:", maintenance.backup(store, args.backup_dir))
    return 0


def cmd_stats(args) -> int:
    print(json.dumps(_store(args).stats(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain", description="Self-hosted second brain")
    p.add_argument("--db", help="SQLite path (default $BRAIN_DB or ./brain.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP/MCP server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--config")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("worker", help="run connectors/enrichment/maintenance on a schedule")
    s.add_argument("--config")
    s.add_argument("--once", action="store_true", help="single cycle then exit")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("push", help="run connectors on this device and push to a remote server")
    s.add_argument("--config", required=True)
    s.add_argument("--url")
    s.add_argument("--token")
    s.add_argument("--state", default="~/.brain/state.json")
    s.add_argument("--only")
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser("enrich-remote", help="LLM-enrich a remote brain's rule-based items from this device")
    s.add_argument("--url")
    s.add_argument("--token")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--mode", default="claude-cli")
    s.add_argument("--model", default="sonnet")
    s.set_defaults(fn=cmd_enrich_remote)

    s = sub.add_parser("search", help="search from the terminal")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--scope")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("add", help="add a note (body from --body or stdin)")
    s.add_argument("title")
    s.add_argument("--body")
    s.add_argument("--kind", default="note")
    s.add_argument("--tags", default="")
    s.add_argument("--scope", default="personal")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("import-md", help="import a folder of markdown notes")
    s.add_argument("path")
    s.add_argument("--source", default="import")
    s.add_argument("--scope", default="personal")
    s.set_defaults(fn=cmd_import_md)

    s = sub.add_parser("export-md", help="export everything as markdown files")
    s.add_argument("out")
    s.add_argument("--archived", action="store_true")
    s.set_defaults(fn=cmd_export_md)

    s = sub.add_parser("maintain", help="health report, prune (dry run unless --apply), backup")
    s.add_argument("--config")
    s.add_argument("--archive-after-days", type=float, default=365)
    s.add_argument("--sources", nargs="*")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--backup-dir")
    s.set_defaults(fn=cmd_maintain)

    s = sub.add_parser("stats", help="item counts")
    s.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
