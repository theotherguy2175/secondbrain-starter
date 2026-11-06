import json
import urllib.error
import urllib.request

import pytest

from brain.connectors import run_specs
from brain.server import create_app
from brain.sink import RemoteSink

from conftest import TOKEN


def rpc(server, method, params=None, id=1, token=TOKEN):
    body = {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server.url + "/mcp", data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def call(server, tool, **args):
    status, out = rpc(server, "tools/call", {"name": tool, "arguments": args})
    assert status == 200, out
    return out["result"]


# ------------------------------------------------------------------- auth
def test_refuses_to_start_without_token(store):
    with pytest.raises(RuntimeError):
        create_app(store, token=None)
    create_app(store, token=None, allow_no_auth=True)


def test_auth_required_everywhere_except_health_and_ui(server):
    assert server.request("GET", "/healthz", token=None)[0] == 200
    assert server.request("GET", "/", token=None)[0] == 200
    for method, path in [("GET", "/api/status"), ("GET", "/api/search?q=x"), ("GET", "/api/recent"),
                         ("POST", "/api/items"), ("GET", "/api/items/abc")]:
        assert server.request(method, path, body={} if method == "POST" else None, token=None)[0] == 401
    assert server.request("GET", "/api/status", token="wrong")[0] == 401
    assert server.request("GET", "/api/status", token=TOKEN[:-1])[0] == 401
    assert rpc(server, "tools/list", token=None)[0] == 401
    assert rpc(server, "tools/list", token="nope")[0] == 401
    # alternate header
    code, _ = server.request("GET", "/api/status", token=None, headers={"X-Brain-Token": TOKEN})
    assert code == 200


# -------------------------------------------------------------------- REST
def test_rest_crud_and_search(server):
    code, r = server.request("POST", "/api/items", {"title": "Swordfish", "body": "the pelican wrote it", "tags": "x"})
    assert code == 200 and r["action"] == "created"
    iid = r["id"]
    code, hits = server.request("GET", "/api/search?q=pelican")
    assert code == 200 and hits[0]["id"] == iid
    code, item = server.request("GET", f"/api/items/{iid}")
    assert item["title"] == "Swordfish" and item["source"] == "api"

    code, r = server.request("PUT", f"/api/items/{iid}", {"body": "now about a heron", "pinned": True})
    assert code == 200 and r["action"] == "updated"
    assert server.request("GET", "/api/search?q=heron")[1][0]["pinned"] is True
    assert server.request("GET", "/api/search?q=pelican")[1] == []

    code, r = server.request("POST", f"/api/items/{iid}/append", {"text": "appended egret"})
    assert code == 200
    assert server.request("GET", "/api/search?q=egret")[1]

    assert server.request("GET", "/api/recent?limit=5")[1][0]["id"] == iid
    assert server.request("DELETE", f"/api/items/{iid}")[0] == 200
    assert server.request("GET", "/api/search?q=heron")[1] == []
    assert server.request("DELETE", f"/api/items/{iid}?hard=1")[0] == 200
    assert server.request("GET", f"/api/items/{iid}")[0] == 404


def test_rest_validation(server):
    assert server.request("GET", "/api/search")[0] == 400
    assert server.request("GET", "/api/search?q=x&limit=abc")[0] == 400
    assert server.request("POST", "/api/items", {})[0] == 400
    assert server.request("POST", "/api/items", [1, 2])[0] == 400
    assert server.request("POST", "/api/items", {"title": "x", "body": "y" * 2_000_001})[0] == 413
    assert server.request("PUT", "/api/items/nope", {"title": "x"})[0] == 404
    assert server.request("POST", "/api/items/nope/append", {"text": "x"})[0] == 404
    assert server.request("POST", "/api/items/nope/append", {"text": " "})[0] == 400
    # unknown fields are ignored, not injected
    code, r = server.request("POST", "/api/items", {"title": "t", "body": "b", "status": "archived", "id": "fixedid"})
    assert code == 200 and r["id"] == "fixedid"
    assert server.request("GET", "/api/items/fixedid")[1]["status"] == "active"


def test_rest_upsert_by_source_ref_is_idempotent(server):
    p = {"title": "T", "body": "b", "source": "laptop-sessions", "source_ref": "proj/s1"}
    assert server.request("POST", "/api/items", p)[1]["action"] == "created"
    assert server.request("POST", "/api/items", p)[1]["action"] == "unchanged"
    p["body"] = "b2"
    assert server.request("POST", "/api/items", p)[1]["action"] == "updated"
    assert server.request("GET", "/api/status")[1]["stats"]["total"] == 1


# --------------------------------------------------------------------- MCP
def test_mcp_lists_tools_and_instructions(server):
    code, out = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}})
    assert code == 200 and "brain_search" in out["result"]["instructions"]
    code, out = rpc(server, "tools/list", id=2)
    names = {t["name"] for t in out["result"]["tools"]}
    assert names == {"brain_search", "brain_read", "brain_write", "brain_append", "brain_recent", "brain_status"}


def test_mcp_write_search_read_append_roundtrip(server):
    w = call(server, "brain_write", title="Decision: use SQLite", body="Chose SQLite FTS5 because single node.",
             kind="decision", tags="brain", scope="homelab")
    iid = w["structuredContent"]["id"]
    s = call(server, "brain_search", query="sqlite fts5", scope="homelab")
    hit = s["structuredContent"]["result"][0]
    assert hit["id"] == iid and hit["source"] == "agent" and hit["kind"] == "decision"
    r = call(server, "brain_read", id=iid)["structuredContent"]
    assert "single node" in r["body"]
    call(server, "brain_append", id=iid, text="Follow-up: add backups to NAS")
    assert call(server, "brain_search", query="backups nas")["structuredContent"]["result"]
    rec = call(server, "brain_recent", limit=3)["structuredContent"]["result"]
    assert rec[0]["id"] == iid
    assert call(server, "brain_read", id="missing")["structuredContent"]["error"]
    assert call(server, "brain_append", id="missing", text="x")["structuredContent"]["error"]
    st = call(server, "brain_status")["structuredContent"]
    assert st["stats"]["by_source"]["agent"] == 1


def test_mcp_scope_filter_isolates_data(server):
    call(server, "brain_write", title="Personal", body="zeppelin trip", scope="personal")
    call(server, "brain_write", title="Homelab", body="zeppelin cluster", scope="homelab")
    only = call(server, "brain_search", query="zeppelin", scope="homelab")["structuredContent"]["result"]
    assert [h["title"] for h in only] == ["Homelab"]


# ------------------------------------------------------ device push (remote)
def test_remote_sink_pushes_connector_output(server, tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    (d / "n.md").write_text("---\ntitle: From laptop\n---\nmarmot hibernation schedule")
    state = tmp_path / "state.json"
    sink = RemoteSink(server.url, TOKEN, state_path=state, default_scope="personal")
    specs = [{"name": "laptop-notes", "type": "markdown_dir", "options": {"path": str(d)}}]
    assert run_specs(specs, sink)["laptop-notes"] == {"ok": True, "ingested": 1}
    hit = server.store.search("marmot")[0]
    assert hit["title"] == "From laptop" and hit["source"] == "laptop-notes" and hit["scope"] == "personal"
    # second run: cursor persisted locally -> nothing re-sent
    assert run_specs(specs, sink)["laptop-notes"]["ingested"] == 0
    assert json.loads(state.read_text())["laptop-notes"]["cursor"]


def test_remote_sink_wrong_token_fails_and_keeps_cursor(server, tmp_path):
    d = tmp_path / "n"
    d.mkdir()
    (d / "a.md").write_text("x")
    state = tmp_path / "s.json"
    sink = RemoteSink(server.url, "bad", state_path=state)
    res = run_specs([{"name": "c", "type": "markdown_dir", "options": {"path": str(d)}}], sink, log=lambda m: None)
    assert res["c"]["ok"] is False and "401" in res["c"]["error"]
    assert json.loads(state.read_text())["c"]["cursor"] == ""  # not advanced -> will retry later
    assert server.store.stats()["total"] == 0


def test_list_items_pagination_sources_and_archived(server):
    ids = {}
    for name, src in (("a", "agent-docs"), ("b", "agent-docs"), ("c", "agent"), ("d", "session")):
        code, r = server.request("POST", "/api/items", {"title": name, "body": f"body {name}", "source": src,
                                                       "source_ref": f"ref/{name}"})
        ids[name] = r["id"]
    code, page = server.request("GET", "/api/items?source=agent-docs,agent&limit=2")
    assert code == 200 and len(page["items"]) == 2 and page["next"] == 2
    assert {"body", "status", "updated_at", "source_ref"} <= set(page["items"][0])
    code, page2 = server.request("GET", f"/api/items?source=agent-docs,agent&limit=2&offset={page['next']}")
    assert len(page2["items"]) == 1 and page2["next"] is None
    got = {i["title"] for i in page["items"] + page2["items"]}
    assert got == {"a", "b", "c"}  # 'd' (other source) excluded

    assert server.request("DELETE", f"/api/items/{ids['a']}")[0] == 200  # archive
    code, active = server.request("GET", "/api/items?source=agent-docs")
    assert [i["title"] for i in active["items"]] == ["b"]
    code, allx = server.request("GET", "/api/items?source=agent-docs&include_archived=1")
    assert {i["title"]: i["status"] for i in allx["items"]} == {"a": "archived", "b": "active"}
    assert server.request("GET", "/api/items?limit=abc")[0] == 400


def test_list_items_does_not_touch_access_counters(server):
    code, r = server.request("POST", "/api/items", {"title": "t", "body": "b"})
    server.request("GET", "/api/items")
    code, item = server.request("GET", f"/api/items/{r['id']}")
    assert item["access_count"] == 0  # first real read still sees 0 (get() bumps after read)


def test_mcp_titles_only_and_read_truncation(server):
    w = call(server, "brain_write", title="Long note", body="zebra " + "x" * 3000, scope="homelab")
    iid = w["structuredContent"]["id"]
    hit = call(server, "brain_search", query="zebra", titles_only=True)["structuredContent"]["result"][0]
    assert hit["id"] == iid and "snippet" not in hit and "summary" not in hit and "title" in hit
    r = call(server, "brain_read", id=iid, max_chars=500)["structuredContent"]
    assert r["truncated"] is True and r["body_chars"] > 3000 and len(r["body"]) < 600
    assert "truncated" not in call(server, "brain_read", id=iid)["structuredContent"]
