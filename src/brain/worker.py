"""Background worker: scheduled connectors, enrichment, pruning and backups.

Runs as a sidecar container next to the server (they share the data volume),
or locally via `brain worker`. State lives in the database, so restarts are safe.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

from . import maintenance
from .connectors import run_specs
from .enrich import enrich_pending, get_enricher
from .sink import LocalSink
from .store import Store


def load_config(path: str | None) -> dict[str, Any]:
    if not path or not Path(path).exists():
        return {"connectors": []}
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    cfg.setdefault("connectors", [])
    return cfg


def run_once(store: Store, cfg: dict[str, Any], enricher=None, log=print) -> dict[str, Any]:
    """One full cycle: all connectors, then enrichment of anything new."""
    sink = LocalSink(store)
    results = run_specs(cfg.get("connectors", []), sink, log=log)
    enricher = enricher or get_enricher()
    enriched = enrich_pending(store, enricher, limit=int(cfg.get("enrich_batch", 50)))
    return {"connectors": results, "enriched": enriched}


def loop(store: Store, cfg: dict[str, Any], log=print, sleep=time.sleep, max_cycles: int | None = None) -> None:
    specs = cfg.get("connectors", [])
    last_run: dict[str, float] = {}
    last_daily = 0.0
    enricher = get_enricher()
    sink = LocalSink(store)
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        now = time.time()
        due = [s for s in specs if now - last_run.get(s["name"], 0) >= float(s.get("interval_minutes", 15)) * 60]
        for spec in due:
            last_run[spec["name"]] = now
            res = run_specs([spec], sink, log=log)
            log(f"[worker] {spec['name']}: {res[spec['name']]}")
        n = enrich_pending(store, enricher, limit=int(cfg.get("enrich_batch", 50)))
        if n:
            log(f"[worker] enriched {n} items")
        if now - last_daily >= 86400:
            last_daily = now
            m = cfg.get("maintenance", {})
            if m.get("prune_sources") or m.get("archive_after_days"):
                log(f"[worker] prune: {maintenance.prune(store, float(m.get('archive_after_days', 365)), m.get('prune_sources'))}")
            backup_dir = os.environ.get("BRAIN_BACKUP_DIR", m.get("backup_dir", ""))
            if backup_dir:
                log(f"[worker] backup: {maintenance.backup(store, backup_dir, int(m.get('backup_keep', 7)))}")
        cycles += 1
        sleep(float(cfg.get("poll_seconds", 30)))
