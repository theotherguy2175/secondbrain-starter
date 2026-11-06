import json
import multiprocessing as mp
import os
import subprocess
import sys

from brain.store import Store


def _writer(path, tag, n, q):
    try:
        s = Store(path)
        for i in range(n):
            s.put(title=f"{tag} {i}", body=f"concurrent {tag} item {i} lorem ipsum", source=tag, source_ref=str(i))
            s.search("concurrent lorem")
            s.record_run(tag, str(i), 1, None)
        q.put(None)
    except Exception as e:  # pragma: no cover
        q.put(repr(e))


def test_two_processes_share_one_database(tmp_path):
    """Server + worker sidecar share the DB file: no 'database is locked', no lost writes."""
    path = str(tmp_path / "shared.db")
    Store(path).close()
    q = mp.Queue()
    procs = [mp.Process(target=_writer, args=(path, t, 150, q)) for t in ("server", "worker", "device")]
    [p.start() for p in procs]
    errs = [q.get(timeout=60) for _ in procs]
    [p.join(timeout=10) for p in procs]
    assert errs == [None, None, None], errs
    s = Store(path)
    assert s.stats()["total"] == 450
    assert len(s.search("concurrent", limit=25)) == 25
    assert s.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    s.db.execute("INSERT INTO items_fts(items_fts) VALUES('integrity-check')")  # raises if index is inconsistent


def run_cli(*args, env=None, stdin=None):
    e = {**os.environ, "PYTHONPATH": "src", **(env or {})}
    return subprocess.run([sys.executable, "-m", "brain.cli", *args], capture_output=True, text=True, env=e,
                          input=stdin, timeout=60)


def test_cli_add_search_import_export_maintain(tmp_path):
    db = str(tmp_path / "c.db")
    r = run_cli("--db", db, "add", "Traefik fix", "--body", "Error 502 fixed by matching targetPort", "--kind", "fix")
    assert r.returncode == 0 and "created" in r.stdout
    r = run_cli("--db", db, "add", "From stdin", stdin="body via stdin heron")
    assert r.returncode == 0
    r = run_cli("--db", db, "search", "traefik", "502")
    assert r.returncode == 0 and "Traefik fix" in r.stdout

    notes = tmp_path / "vault"
    notes.mkdir()
    (notes / "x.md").write_text("---\ntitle: Vault note\ntags: [v]\n---\nimported falcon")
    r = run_cli("--db", db, "import-md", str(notes), "--source", "vault")
    assert r.returncode == 0 and "imported 1" in r.stdout
    assert "Vault note" in run_cli("--db", db, "search", "falcon").stdout
    assert json.loads(run_cli("--db", db, "stats").stdout)["unenriched"] == 0  # import enriches

    out = tmp_path / "export"
    r = run_cli("--db", db, "export-md", str(out))
    assert r.returncode == 0 and "exported 3" in r.stdout
    assert len(list(out.rglob("*.md"))) == 3

    r = run_cli("--db", db, "maintain", "--backup-dir", str(tmp_path / "bk"))
    assert r.returncode == 0 and "backup:" in r.stdout
    assert list((tmp_path / "bk").glob("brain-*.db"))


def test_cli_worker_once_and_push_requires_creds(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    (d / "a.md").write_text("worker note")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"connectors:\n  - name: inbox\n    type: markdown_dir\n    options: {{path: {d}}}\n")
    db = str(tmp_path / "w.db")
    r = run_cli("--db", db, "worker", "--once", "--config", str(cfg))
    assert r.returncode == 0 and '"ingested": 1' in r.stdout
    r = run_cli("push", "--config", str(cfg), env={"BRAIN_URL": "", "BRAIN_TOKEN": ""})
    assert r.returncode == 2 and "BRAIN_URL" in r.stderr
