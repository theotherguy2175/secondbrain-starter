import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from brain import maintenance, notes_io
from brain.connectors import ClaudeSessions, GitLog, MarkdownDir, parse_session, run_specs
from brain.enrich import (AnthropicEnricher, HeuristicEnricher, OpenAICompatEnricher, ScopedEnricher,
                          enrich_pending, get_enricher, parse_llm_json)
from brain.sink import LocalSink
from brain.worker import load_config, loop, run_once


# ------------------------------------------------------------------ markdown
def test_frontmatter_parse_and_bad_yaml():
    meta, body = notes_io.parse_markdown("---\ntitle: X\ntags: [a, b]\n---\nhello")
    assert meta["title"] == "X" and body == "hello"
    meta, body = notes_io.parse_markdown("---\n: : bad: [\n---\nhello")
    assert meta == {} and body == "hello"
    assert notes_io.parse_markdown("no front matter") == ({}, "no front matter")


def test_markdown_export_import_roundtrip(tmp_path, corpus):
    r = corpus.put(title="Roundtrip Note", body="# H\ncontent about walrus", kind="howto",
                   tags="x, y", pinned=True)
    out = tmp_path / "out"
    n = notes_io.export_all(corpus, out)
    assert n == corpus.stats()["active"]
    files = list(out.rglob("*roundtrip-note*.md"))
    assert len(files) == 1
    meta, body = notes_io.parse_markdown(files[0].read_text())
    assert meta["id"] == r["id"] and meta["pinned"] is True and meta["tags"] == ["x", "y"]
    assert "walrus" in body


# ---------------------------------------------------------------- connectors
def test_markdown_dir_connector_incremental(tmp_path, store):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / ".hidden").mkdir()
    (inbox / ".hidden" / "skip.md").write_text("secret")
    (inbox / "a.md").write_text("---\ntitle: Alpha\nkind: decision\ncreated: 2026-01-02\n---\nchose sqlite because simple")
    (inbox / "sub" / "b.md").write_text("# Beta heading\nbody text")
    specs = [{"name": "inbox", "type": "markdown_dir", "scope": "homelab", "options": {"path": str(inbox)}}]
    sink = LocalSink(store)

    res = run_specs(specs, sink)
    assert res["inbox"] == {"ok": True, "ingested": 2}
    a = store.search("sqlite simple")[0]
    assert a["title"] == "Alpha" and a["kind"] == "decision" and a["scope"] == "homelab"
    assert a["source"] == "inbox"
    assert not store.search("secret")

    # nothing changed -> nothing ingested
    assert run_specs(specs, sink)["inbox"]["ingested"] == 0

    # modify one file (bump mtime) -> only it is re-ingested, no duplicate
    time.sleep(0.02)
    p = inbox / "a.md"
    p.write_text(p.read_text() + "\nupdated with kestrel")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert run_specs(specs, sink)["inbox"]["ingested"] == 1
    assert store.stats()["by_source"]["inbox"] == 2
    assert store.search("kestrel")


def test_connector_failure_is_isolated_and_recorded(tmp_path, store):
    good = tmp_path / "g"
    good.mkdir()
    (good / "x.md").write_text("fine content")
    specs = [
        {"name": "broken", "type": "markdown_dir", "options": {"path": str(tmp_path / "nope")}},
        {"name": "ok", "type": "markdown_dir", "options": {"path": str(good)}},
    ]
    logs = []
    res = run_specs(specs, LocalSink(store), log=logs.append)
    assert res["broken"]["ok"] is False and "not found" in res["broken"]["error"]
    assert res["ok"]["ok"] is True
    assert store.get_state("broken")["last_error"]
    assert store.get_state("ok")["last_error"] is None


def test_unknown_connector_type(store):
    res = run_specs([{"name": "x", "type": "nope"}], LocalSink(store), log=lambda m: None)
    assert res["x"]["ok"] is False


def _write_session(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines))


def test_claude_sessions_connector(tmp_path, store):
    root = tmp_path / "projects"
    _write_session(root / "-home-me-proj" / "sess1.jsonl", [
        {"type": "summary", "summary": "ignored"},
        {"type": "user", "timestamp": "2026-10-01T10:00:00Z",
         "message": {"role": "user", "content": "How do I fix the ingress 502 on traefik?"}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Check the service port name; traefik needs matching targetPort."},
             {"type": "tool_use", "name": "Bash", "input": {"command": "secret-tool-input"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "huge output"}]}},
        "not json",
    ])
    (root / "-home-me-proj" / "sess2.jsonl").write_text("garbage\n\n{}\n")
    sink = LocalSink(store)
    spec = [{"name": "sessions", "type": "claude_sessions", "options": {"path": str(root)}}]
    assert run_specs(spec, sink)["sessions"]["ingested"] == 1
    hit = store.search("traefik ingress 502")[0]
    assert hit["kind"] == "session" and "claude-session" in hit["tags"]
    full = store.get(hit["id"])
    assert "targetPort" in full["body"] and "secret-tool-input" not in full["body"]
    assert "huge output" not in full["body"]
    assert abs(full["created_at"] - 1_790_848_800) < 1e6  # parsed from timestamp (Oct 2026)


def test_parse_session_truncates_long_messages(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_session(p, [{"type": "user", "message": {"role": "user", "content": "x" * 20000}}])
    out = parse_session(p)
    assert len(out["body"]) < 5000 and "truncated" in out["body"]


@pytest.mark.skipif(subprocess.run(["which", "git"], capture_output=True).returncode != 0, reason="git missing")
def test_git_log_connector(tmp_path, store):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T",
           "GIT_COMMITTER_EMAIL": "t@x"}
    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=env)
    git("init", "-q")
    (repo / "f").write_text("1")
    git("add", ".")
    git("commit", "-qm", "add flux bootstrap", "-m", "because gitops")
    spec = [{"name": "git", "type": "git_log", "options": {"repos": [str(repo)]}}]
    assert run_specs(spec, LocalSink(store))["git"]["ingested"] == 1
    hit = store.search("flux bootstrap")[0]
    assert hit["kind"] == "commits"
    assert "gitops" in store.get(hit["id"])["body"]
    # same-day new commit updates the existing item, no duplicate
    (repo / "f").write_text("2")
    git("commit", "-aqm", "second change velociraptor")
    assert run_specs(spec, LocalSink(store))["git"]["ingested"] == 1
    assert store.stats()["by_source"]["git"] == 1
    assert store.search("velociraptor")


def test_scope_override_by_connector(tmp_path, store):
    d = tmp_path / "d"
    d.mkdir()
    (d / "n.md").write_text("---\nscope: work\n---\nbody")
    run_specs([{"name": "n", "type": "markdown_dir", "scope": "personal", "options": {"path": str(d)}}],
              LocalSink(store))
    assert store.recent()[0]["scope"] == "personal"  # connector scope wins; no accidental leakage


# ---------------------------------------------------------------- enrichment
def test_heuristic_enricher_extracts_atoms():
    item = {"title": "Fix NFS", "kind": "fix", "body": (
        "# Fix NFS\nBackups failed overnight.\n\n```\nmount -a\n```\n"
        "Error: storage 'nas' is not online\nWe decided to use NFSv4 because of locking.\n")}
    out = HeuristicEnricher().enrich(item)
    assert out["summary"].startswith("Fix NFS")
    assert "mount -a" in out["atoms"] and "Error: storage" in out["atoms"] and "decided" in out["atoms"]


def test_enrich_pending_end_to_end(corpus):
    r = corpus.put(title="Kernel panic", body="Error: kernel panic not syncing\nFixed by pinning kernel 6.1")
    n = enrich_pending(corpus, HeuristicEnricher())
    assert n >= 1
    item = corpus.get(r["id"], touch=False)
    assert item["enriched"] == 1 and "kernel panic" in item["atoms"].lower()
    assert corpus.search("pinning kernel")[0]["id"] == r["id"]


def test_parse_llm_json_tolerates_fences_and_prose():
    out = parse_llm_json('Sure!\n```json\n{"summary":"s","questions":["q1","q2"],"atoms":["a"]}\n```')
    assert out == {"summary": "s", "questions": "q1\nq2", "atoms": "a"}
    with pytest.raises(ValueError):
        parse_llm_json("no json here")


class _FakeLLM(BaseHTTPRequestHandler):
    calls = []
    fail = False

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        _FakeLLM.calls.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if _FakeLLM.fail:
            self.send_response(500)
            self.end_headers()
            return
        payload = '{"summary":"Fake summary","questions":["why?"],"atoms":["do the thing"]}'
        if self.path.endswith("/messages"):
            out = {"content": [{"type": "text", "text": payload}]}
        else:
            out = {"choices": [{"message": {"content": payload}}]}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake_llm():
    _FakeLLM.calls, _FakeLLM.fail = [], False
    srv = HTTPServer(("127.0.0.1", 0), _FakeLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


ITEM = {"title": "T", "kind": "note", "body": "some body", "scope": "personal"}


def test_anthropic_enricher_request_shape(fake_llm):
    out = AnthropicEnricher("k", "claude-haiku-5-5", fake_llm).enrich(ITEM)
    assert out["summary"] == "Fake summary"
    path, headers, body = _FakeLLM.calls[0]
    assert path == "/v1/messages" and headers["x-api-key"] == "k"
    assert body["model"] == "claude-haiku-5-5" and "some body" in body["messages"][0]["content"]


def test_openai_compat_enricher_request_shape(fake_llm):
    out = OpenAICompatEnricher(fake_llm + "/v1", "llama3", "key").enrich(ITEM)
    assert out["atoms"] == "do the thing"
    path, headers, body = _FakeLLM.calls[0]
    assert path == "/v1/chat/completions" and body["model"] == "llama3"
    assert headers["authorization"] == "Bearer key"


def test_scoped_enricher_limits_what_leaves_and_falls_back(fake_llm):
    primary = OpenAICompatEnricher(fake_llm + "/v1", "m")
    scoped = ScopedEnricher(primary, {"personal"})
    scoped.enrich({**ITEM, "scope": "homelab"})
    assert _FakeLLM.calls == []  # out-of-scope item never sent to the model
    scoped.enrich(ITEM)
    assert len(_FakeLLM.calls) == 1
    _FakeLLM.fail = True
    out = scoped.enrich(ITEM)  # provider down -> heuristic fallback, no exception
    assert out["summary"] and scoped.last_error


def test_get_enricher_env():
    assert isinstance(get_enricher({}), HeuristicEnricher)
    with pytest.raises(RuntimeError):
        get_enricher({"BRAIN_ENRICH": "anthropic"})
    with pytest.raises(RuntimeError):
        get_enricher({"BRAIN_ENRICH": "openai"})
    e = get_enricher({"BRAIN_ENRICH": "openai", "BRAIN_ENRICH_BASE_URL": "http://x", "BRAIN_ENRICH_MODEL": "m",
                      "BRAIN_ENRICH_SCOPES": "personal, homelab"})
    assert isinstance(e, ScopedEnricher) and e.allowed == {"personal", "homelab"}


# --------------------------------------------------------------- maintenance
def test_health_flags_never_ran_failing_and_stale(store):
    specs = [{"name": "a", "interval_minutes": 10}, {"name": "b", "interval_minutes": 10},
             {"name": "c", "interval_minutes": 10}, {"name": "d", "interval_minutes": 10}]
    store.record_run("b", "1", 0, "401 unauthorized")
    store.record_run("c", "1", 3, None)
    store.db.execute("UPDATE connector_state SET last_run=? WHERE name='c'", (time.time() - 3 * 3600,))
    store.record_run("d", "1", 3, None)
    h = maintenance.health(store, specs)
    probs = {p["connector"]: p["problem"] for p in h["problems"]}
    assert probs["a"] == "never ran"
    assert "401" in probs["b"] and "stale" in probs["c"] and "d" not in probs
    assert h["ok"] is False


def test_prune_archives_only_stale_unpinned_unread_automated(store):
    old = time.time() - 800 * 86400
    store.db.execute("PRAGMA foreign_keys=OFF")
    mk = lambda **k: store.put(created_at=old, **k)["id"]
    auto = mk(title="auto", body="x", source="sessions", source_ref="1")
    pinned = mk(title="pin", body="x", source="sessions", source_ref="2", pinned=True)
    manual = mk(title="manual", body="x", source="manual")
    agent = mk(title="agent", body="x", source="agent")
    read = mk(title="read", body="x", source="sessions", source_ref="3")
    fresh = store.put(title="fresh", body="x", source="sessions", source_ref="4")["id"]
    for i in (auto, pinned, manual, agent, read):
        store.db.execute("UPDATE items SET updated_at=? WHERE id=?", (old, i))
    store.get(read)
    store.db.commit()
    dry = maintenance.prune(store, 365, dry_run=True)
    assert dry["candidates"] == 1 and store.get(auto, touch=False)["status"] == "active"
    res = maintenance.prune(store, 365)
    assert res["archived"] == 1
    status = {i: store.get(i, touch=False)["status"] for i in (auto, pinned, manual, agent, read, fresh)}
    assert status[auto] == "archived"
    assert all(v == "active" for k, v in status.items() if k != auto)


def test_backup_rotation(tmp_path, corpus):
    for _ in range(4):
        maintenance.backup(corpus, tmp_path / "b", keep=2)
        time.sleep(1.05)
    assert len(list((tmp_path / "b").glob("brain-*.db"))) == 2


# -------------------------------------------------------------------- worker
def test_worker_run_once_and_loop(tmp_path, store):
    d = tmp_path / "in"
    d.mkdir()
    (d / "n.md").write_text("# Note\nError: widget exploded\n")
    cfgfile = tmp_path / "c.yaml"
    cfgfile.write_text(f"connectors:\n  - name: inbox\n    type: markdown_dir\n    interval_minutes: 1\n"
                       f"    options: {{path: {d}}}\n")
    cfg = load_config(str(cfgfile))
    out = run_once(store, cfg)
    assert out["connectors"]["inbox"]["ingested"] == 1 and out["enriched"] == 1
    assert store.stats()["unenriched"] == 0
    (d / "m.md").write_text("another")
    logs = []
    loop(store, cfg, log=logs.append, sleep=lambda s: None, max_cycles=2)
    assert store.stats()["by_source"]["inbox"] == 2
    assert load_config(None) == {"connectors": []}


def test_prune_never_archives_agent_docs_mirror(store):
    old = time.time() - 800 * 86400
    for src in ("agent-docs", "session"):
        store.put(title=src, body="old", source=src, source_ref=src, created_at=old)
    store.db.execute("UPDATE items SET updated_at=?", (old,))
    store.db.commit()
    res = maintenance.prune(store, archive_after_days=365)
    assert res["archived"] == 1
    status = {r["source"]: r["status"] for r in store.db.execute("SELECT source, status FROM items")}
    assert status == {"agent-docs": "active", "session": "archived"}
