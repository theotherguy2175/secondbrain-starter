"""Markdown <-> item conversion, so notes stay portable.

Files are plain markdown with optional YAML frontmatter:

    ---
    id: abc123
    title: Proxmox backup schedule
    kind: note
    tags: [homelab, backup]
    scope: personal
    created: 2026-11-05
    pinned: true
    ---
    body...
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_markdown(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into (metadata, body). Never raises on bad YAML."""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, text[m.end():]


def to_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.timestamp()
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return dt.datetime.fromisoformat(str(value)).replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def title_from(meta: dict[str, Any], body: str, fallback: str) -> str:
    if meta.get("title"):
        return str(meta["title"])
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        if s:
            return s[:80]
    return fallback


def tags_from(meta: dict[str, Any]) -> str:
    tags = meta.get("tags", "")
    if isinstance(tags, (list, tuple)):
        return ", ".join(str(t) for t in tags)
    return str(tags or "")


def slugify(text: str, maxlen: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen] or "untitled"


def item_to_markdown(item: dict[str, Any]) -> str:
    meta: dict[str, Any] = {
        "id": item["id"],
        "title": item["title"],
        "kind": item["kind"],
        "scope": item["scope"],
        "source": item["source"],
        "created": dt.datetime.fromtimestamp(item["created_at"], dt.timezone.utc).strftime("%Y-%m-%d"),
    }
    if item.get("tags"):
        meta["tags"] = [t.strip() for t in item["tags"].split(",") if t.strip()]
    if item.get("pinned"):
        meta["pinned"] = True
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n{item['body'].strip()}\n"


def export_all(store, out_dir: str | Path, include_archived: bool = False) -> int:
    """Write every item to <out_dir>/<kind>/<slug>-<id>.md. Returns file count."""
    out = Path(out_dir)
    n = 0
    status_clause = "" if include_archived else "WHERE status='active'"
    with store._lock:
        ids = [r["id"] for r in store.db.execute(f"SELECT id FROM items {status_clause}")]
    for item_id in ids:
        item = store.get(item_id, touch=False)
        if item is None:
            continue
        d = out / slugify(item["kind"])
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slugify(item['title'])}-{item['id']}.md").write_text(
            item_to_markdown(item), encoding="utf-8"
        )
        n += 1
    return n


def file_to_fields(path: Path, root: Path) -> dict[str, Any]:
    """Read one markdown file into kwargs suitable for Store.put / Sink.put."""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_markdown(text)
    rel = path.relative_to(root).as_posix()
    created = to_timestamp(meta.get("created")) or path.stat().st_mtime
    return {
        "title": title_from(meta, body, path.stem),
        "body": body.strip(),
        "kind": str(meta.get("kind", "note")),
        "tags": tags_from(meta),
        "scope": str(meta.get("scope", "")) or None,
        "pinned": bool(meta["pinned"]) if "pinned" in meta else None,
        "created_at": created,
        "source_ref": rel,
    }
