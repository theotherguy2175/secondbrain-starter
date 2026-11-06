"""SQLite + FTS5 store with BM25 search and recency weighting.

One table of `items` is the single source of truth. Everything an agent or
connector writes becomes an item. Search runs BM25 over the summary / questions /
atoms / title / tags / body columns (weighted toward the enriched fields), then
re-ranks with an exponential recency decay so stale facts lose to fresh ones.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'manual',
  source_ref TEXT,
  kind TEXT NOT NULL DEFAULT 'note',
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  questions TEXT NOT NULL DEFAULT '',
  atoms TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'personal',
  pinned INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  enriched INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  ingested_at REAL NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_ref
  ON items(source, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, summary, questions, atoms, tags, body,
  content='items', content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, summary, questions, atoms, tags, body)
  VALUES (new.rowid, new.title, new.summary, new.questions, new.atoms, new.tags, new.body);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary, questions, atoms, tags, body)
  VALUES ('delete', old.rowid, old.title, old.summary, old.questions, old.atoms, old.tags, old.body);
END;
CREATE TRIGGER IF NOT EXISTS items_au
  AFTER UPDATE OF title, summary, questions, atoms, tags, body ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, summary, questions, atoms, tags, body)
  VALUES ('delete', old.rowid, old.title, old.summary, old.questions, old.atoms, old.tags, old.body);
  INSERT INTO items_fts(rowid, title, summary, questions, atoms, tags, body)
  VALUES (new.rowid, new.title, new.summary, new.questions, new.atoms, new.tags, new.body);
END;

CREATE TABLE IF NOT EXISTS connector_state(
  name TEXT PRIMARY KEY,
  cursor TEXT NOT NULL DEFAULT '',
  last_run REAL,
  last_ok REAL,
  last_error TEXT,
  last_count INTEGER NOT NULL DEFAULT 0,
  total_count INTEGER NOT NULL DEFAULT 0
);
"""

# bm25 column weights: title, summary, questions, atoms, tags, body
BM25_WEIGHTS = (5.0, 4.0, 3.0, 4.0, 2.0, 1.0)

STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how i if in into is it its "
    "me my of on or our so that the their then there these they this to was we "
    "what when where which who why will with you your do does did can could should".split()
)

DAY = 86400.0

ITEM_COLUMNS = (
    "id, source, source_ref, kind, title, body, summary, questions, atoms, tags, "
    "scope, pinned, status, enriched, created_at, updated_at, ingested_at, "
    "access_count, last_accessed"
)


def _now() -> float:
    return time.time()


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def build_match(query: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression (OR of prefix terms).

    BM25 naturally scores documents that match more terms higher, so OR gives
    good recall without hard failures on long natural-language queries.
    """
    tokens = [t for t in re.findall(r"\w+", query.lower()) if t not in STOPWORDS]
    if not tokens:
        tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return None
    seen: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.append(t)
    parts = [f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in seen[:24]]
    return " OR ".join(parts)


def recency_weight(created_at: float, now: float, half_life_days: float, floor: float) -> float:
    """1.0 for brand new items, decaying toward `floor` with the given half-life."""
    age_days = max(0.0, (now - created_at) / DAY)
    if half_life_days <= 0:
        return 1.0
    return floor + (1.0 - floor) * math.pow(0.5, age_days / half_life_days)


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA busy_timeout=10000")
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------ write
    def put(
        self,
        *,
        title: str = "",
        body: str = "",
        id: str | None = None,
        source: str = "manual",
        source_ref: str | None = None,
        kind: str = "note",
        summary: str = "",
        questions: str = "",
        atoms: str = "",
        tags: str = "",
        scope: str = "personal",
        pinned: bool | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Insert or update an item. Returns {"id", "action"} where action is
        created / updated / unchanged. Identity is (source, source_ref) when a
        source_ref is given, otherwise `id`, otherwise a new item."""
        now = _now()
        if isinstance(tags, (list, tuple)):
            tags = ", ".join(tags)
        chash = _hash(title, body, kind, tags, scope)
        with self._lock:
            row = None
            if source_ref is not None:
                row = self.db.execute(
                    "SELECT * FROM items WHERE source=? AND source_ref=?", (source, source_ref)
                ).fetchone()
            if row is None and id:
                row = self.db.execute("SELECT * FROM items WHERE id=?", (id,)).fetchone()
            if row is None:
                new_id = id or uuid.uuid4().hex[:12]
                self.db.execute(
                    "INSERT INTO items(id, source, source_ref, kind, title, body, summary, questions,"
                    " atoms, tags, scope, pinned, enriched, content_hash, created_at, updated_at,"
                    " ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id, source, source_ref, kind, title, body, summary, questions, atoms,
                        tags, scope, 1 if pinned else 0, 1 if summary else 0, chash,
                        created_at or now, now, now,
                    ),
                )
                self.db.commit()
                return {"id": new_id, "action": "created"}
            if row["content_hash"] == chash and not (summary or questions or atoms) and pinned is None:
                return {"id": row["id"], "action": "unchanged"}
            # content changed -> update; keep enrichment unless caller supplied new,
            # but a body change invalidates old enrichment so the worker redoes it.
            body_changed = row["content_hash"] != chash
            new_summary = summary or ("" if body_changed else row["summary"])
            new_questions = questions or ("" if body_changed else row["questions"])
            new_atoms = atoms or ("" if body_changed else row["atoms"])
            enriched = 1 if new_summary else 0
            self.db.execute(
                "UPDATE items SET kind=?, title=?, body=?, summary=?, questions=?, atoms=?, tags=?,"
                " scope=?, pinned=?, enriched=?, content_hash=?, updated_at=?,"
                " status='active' WHERE id=?",
                (
                    kind, title, body, new_summary, new_questions, new_atoms, tags, scope,
                    row["pinned"] if pinned is None else (1 if pinned else 0),
                    enriched, chash, now, row["id"],
                ),
            )
            if created_at is not None and source_ref is not None:
                self.db.execute("UPDATE items SET created_at=? WHERE id=?", (created_at, row["id"]))
            self.db.commit()
            return {"id": row["id"], "action": "updated"}

    def append(self, id: str, text: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM items WHERE id=?", (id,)).fetchone()
            if row is None:
                return None
            body = (row["body"].rstrip() + "\n\n" + text.strip()).strip() + "\n"
            chash = _hash(row["title"], body, row["kind"], row["tags"], row["scope"])
            self.db.execute(
                "UPDATE items SET body=?, content_hash=?, updated_at=?, enriched=0 WHERE id=?",
                (body, chash, _now(), id),
            )
            self.db.commit()
            return {"id": id, "action": "appended"}

    def set_enrichment(self, id: str, summary: str, questions: str, atoms: str, level: int = 1) -> None:
        """level 1 = rule-based (heuristic), 2 = written by an LLM (never downgraded)."""
        with self._lock:
            self.db.execute(
                "UPDATE items SET summary=?, questions=?, atoms=?, enriched=? WHERE id=? AND enriched<=?",
                (summary, questions, atoms, level, id, level),
            )
            self.db.commit()

    def archive(self, id: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE items SET status='archived' WHERE id=?", (id,))
            self.db.commit()
            return cur.rowcount > 0

    def delete(self, id: str) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM items WHERE id=?", (id,))
            self.db.commit()
            return cur.rowcount > 0

    def set_pinned(self, id: str, pinned: bool) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE items SET pinned=? WHERE id=?", (1 if pinned else 0, id))
            self.db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------- read
    def get(self, id: str, touch: bool = True) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(f"SELECT {ITEM_COLUMNS} FROM items WHERE id=?", (id,)).fetchone()
            if row is None:
                return None
            if touch:
                self.db.execute(
                    "UPDATE items SET access_count=access_count+1, last_accessed=? WHERE id=?",
                    (_now(), id),
                )
                self.db.commit()
            return dict(row)

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        scope: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        since_days: float | None = None,
        half_life_days: float = 90.0,
        floor: float = 0.35,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        match = build_match(query)
        if match is None:
            return []
        where = ["items_fts MATCH ?"]
        params: list[Any] = [match]
        if not include_archived:
            where.append("i.status='active'")
        if scope:
            where.append("i.scope=?")
            params.append(scope)
        if source:
            where.append("i.source=?")
            params.append(source)
        if kind:
            where.append("i.kind=?")
            params.append(kind)
        if since_days is not None:
            where.append("i.created_at>=?")
            params.append(_now() - since_days * DAY)
        w = ", ".join(str(x) for x in BM25_WEIGHTS)
        sql = (
            f"SELECT i.id, i.source, i.kind, i.title, i.summary, i.atoms, i.tags, i.scope, i.pinned,"
            f" i.created_at, i.updated_at, bm25(items_fts, {w}) AS rank,"
            f" snippet(items_fts, 5, '[', ']', ' … ', 20) AS snippet"
            f" FROM items_fts JOIN items i ON i.rowid = items_fts.rowid"
            f" WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?"
        )
        params.append(max(limit * 6, 30))
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        now = _now()
        hits = []
        for r in rows:
            weight = 1.0 if r["pinned"] else recency_weight(r["created_at"], now, half_life_days, floor)
            score = (-r["rank"]) * weight
            hits.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "summary": r["summary"],
                    "snippet": r["snippet"],
                    "source": r["source"],
                    "kind": r["kind"],
                    "scope": r["scope"],
                    "tags": r["tags"],
                    "pinned": bool(r["pinned"]),
                    "created_at": r["created_at"],
                    "age_days": round((now - r["created_at"]) / DAY, 1),
                    "score": float(f"{score:.6g}"),
                }
            )
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:limit]

    def recent(
        self, limit: int = 10, kind: str | None = None, source: str | None = None, scope: str | None = None
    ) -> list[dict[str, Any]]:
        where, params = ["status='active'"], []
        for col, val in (("kind", kind), ("source", source), ("scope", scope)):
            if val:
                where.append(f"{col}=?")
                params.append(val)
        params.append(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT id, source, kind, title, summary, tags, scope, pinned, created_at, updated_at"
                f" FROM items WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def list_items(
        self, sources: list[str] | None = None, include_archived: bool = False,
        limit: int = 200, offset: int = 0,
    ) -> dict[str, Any]:
        """Full rows (incl. body and status) in a stable id order, for sync clients.

        Unlike search/recent this does not touch access counters, does not cap at 100,
        and can include archived items so a client can tell "archived" from "never existed".
        """
        where, params = [], []
        if not include_archived:
            where.append("status='active'")
        if sources:
            where.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self.db.execute(
                f"SELECT {ITEM_COLUMNS} FROM items {clause} ORDER BY id LIMIT ? OFFSET ?",
                [*params, limit + 1, offset],
            ).fetchall()
        items = [dict(r) for r in rows[:limit]]
        return {"items": items, "next": offset + limit if len(rows) > limit else None}
    def needing_llm(self, limit: int = 20) -> list[dict[str, Any]]:
        """Active items that only have rule-based (or no) enrichment, oldest first."""
        with self._lock:
            rows = self.db.execute(
                f"SELECT {ITEM_COLUMNS} FROM items WHERE enriched<2 AND status='active'"
                " ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def needing_enrichment(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                f"SELECT {ITEM_COLUMNS} FROM items WHERE enriched=0 AND status='active'"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
            active = self.db.execute("SELECT COUNT(*) c FROM items WHERE status='active'").fetchone()["c"]
            unenriched = self.db.execute(
                "SELECT COUNT(*) c FROM items WHERE enriched=0 AND status='active'"
            ).fetchone()["c"]
            by_source = {
                r["source"]: r["c"]
                for r in self.db.execute(
                    "SELECT source, COUNT(*) c FROM items WHERE status='active' GROUP BY source"
                )
            }
            by_scope = {
                r["scope"]: r["c"]
                for r in self.db.execute(
                    "SELECT scope, COUNT(*) c FROM items WHERE status='active' GROUP BY scope"
                )
            }
        return {
            "total": total,
            "active": active,
            "archived": total - active,
            "unenriched": unenriched,
            "by_source": by_source,
            "by_scope": by_scope,
        }

    # ------------------------------------------------------- connector state
    def get_state(self, name: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM connector_state WHERE name=?", (name,)).fetchone()
        if row is None:
            return {"name": name, "cursor": "", "last_run": None, "last_ok": None,
                    "last_error": None, "last_count": 0, "total_count": 0}
        return dict(row)

    def record_run(self, name: str, cursor: str, count: int, error: str | None) -> None:
        """Record a connector run. On success the cursor advances; on error it is kept."""
        now = _now()
        with self._lock:
            prev = self.get_state(name)
            self.db.execute(
                "INSERT OR REPLACE INTO connector_state(name, cursor, last_run, last_ok, last_error,"
                " last_count, total_count) VALUES (?,?,?,?,?,?,?)",
                (
                    name,
                    cursor if error is None else prev["cursor"],
                    now,
                    now if error is None else prev["last_ok"],
                    error,
                    count,
                    prev["total_count"] + count,
                ),
            )
            self.db.commit()

    def all_states(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM connector_state ORDER BY name")]

    # ------------------------------------------------------------------ misc
    def dump_json(self) -> str:
        with self._lock:
            rows = self.db.execute(f"SELECT {ITEM_COLUMNS} FROM items").fetchall()
        return json.dumps([dict(r) for r in rows], indent=2)

    def backup_to(self, dest: str | Path) -> None:
        """Consistent online backup using SQLite's backup API."""
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            out = sqlite3.connect(str(dest))
            try:
                self.db.backup(out)
            finally:
                out.close()
