"""Keeping the brain fresh: connector health, pruning, backups."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .store import DAY, Store


def health(store: Store, specs: list[dict[str, Any]] | None = None,
           stale_factor: float = 3.0) -> dict[str, Any]:
    """Report connectors that are failing, never ran, or have gone quiet.

    A connector is stale when it has not run for `stale_factor` x its interval.
    Silent connector death (rotated key, changed permission) is the main way a
    second brain rots, so this is surfaced through the status tool and /api/health.
    """
    now = time.time()
    states = {s["name"]: s for s in store.all_states()}
    problems: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for spec in specs or []:
        st = states.get(spec["name"])
        interval = float(spec.get("interval_minutes", 60)) * 60
        if st is None or st["last_run"] is None:
            problems.append({"connector": spec["name"], "problem": "never ran"})
            continue
        age = now - st["last_run"]
        row = {"connector": spec["name"], "last_run_age_min": round(age / 60, 1),
               "last_error": st["last_error"], "last_count": st["last_count"]}
        rows.append(row)
        if st["last_error"]:
            problems.append({"connector": spec["name"], "problem": f"last run failed: {st['last_error']}"})
        elif age > interval * stale_factor:
            problems.append({"connector": spec["name"],
                             "problem": f"stale: no run for {round(age / 60)} min (interval {round(interval / 60)} min)"})
    # connectors known only from pushed state (remote devices) are listed but not judged
    for name, st in states.items():
        if not any(r["connector"] == name for r in rows) and not any(
            p["connector"] == name for p in problems
        ) and st["last_run"]:
            rows.append({"connector": name, "last_run_age_min": round((now - st["last_run"]) / 60, 1),
                         "last_error": st["last_error"], "last_count": st["last_count"]})
    return {"ok": not problems, "problems": problems, "connectors": rows}


def prune(store: Store, archive_after_days: float = 365.0,
          sources: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Archive (never delete) stale, unpinned, never-read items.

    Archived items drop out of search but remain in the database and can be
    restored by rewriting them. `sources` limits pruning to noisy automated
    sources (e.g. sessions, commits); by default only non-manual sources are
    considered so hand-written notes (including those mirrored from agent-docs, where an archive would
    propagate back as a file deletion) are never auto-archived.
    """
    cutoff = time.time() - archive_after_days * DAY
    where = ["status='active'", "pinned=0", "access_count=0", "created_at<?", "updated_at<?"]
    params: list[Any] = [cutoff, cutoff]
    if sources:
        where.append(f"source IN ({','.join('?' * len(sources))})")
        params.extend(sources)
    else:
        where.append("source NOT IN ('manual','agent','agent-docs')")
    with store._lock:
        ids = [r["id"] for r in store.db.execute(f"SELECT id FROM items WHERE {' AND '.join(where)}", params)]
    if not dry_run:
        for i in ids:
            store.archive(i)
    return {"candidates": len(ids), "archived": 0 if dry_run else len(ids), "dry_run": dry_run}


def purge_archived(store: Store, older_than_days: float = 365.0) -> int:
    cutoff = time.time() - older_than_days * DAY
    with store._lock:
        cur = store.db.execute("DELETE FROM items WHERE status='archived' AND updated_at<?", (cutoff,))
        store.db.commit()
        return cur.rowcount


def backup(store: Store, backup_dir: str | Path, keep: int = 7) -> str:
    """Write a timestamped consistent copy of the database and rotate old ones."""
    d = Path(backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"brain-{time.strftime('%Y%m%d-%H%M%S')}.db"
    store.backup_to(dest)
    for old in sorted(d.glob("brain-*.db"))[:-keep]:
        old.unlink()
    return str(dest)
