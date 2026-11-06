"""Connectors: pull only what is new since the last run and write it via a sink.

Every connector is idempotent: items are keyed by (source, source_ref), so
re-reading a file or session updates it in place instead of duplicating it.
Credentials, where a connector needs them, should be read-only.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterator

from . import notes_io
from .redact import redact
from .sink import Sink

MAX_MSG_CHARS = 4000
MAX_TRANSCRIPT_CHARS = 150_000


class Connector:
    """Subclass and implement `collect`, yielding kwargs for Sink.put and
    finally returning the new cursor via `self.cursor`."""

    type = "base"

    def __init__(self, name: str, sink: Sink, scope: str | None = None, **options: Any):
        self.name = name
        self.sink = sink
        self.scope = scope
        self.options = options
        self.cursor = ""

    def collect(self, cursor: str) -> Iterator[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def run(self) -> int:
        cursor = self.sink.get_cursor(self.name)
        self.cursor = cursor
        count = 0
        try:
            for fields in self.collect(cursor):
                fields.setdefault("source", self.name)
                if self.scope:
                    fields["scope"] = self.scope
                result = self.sink.put(**fields)
                if result.get("action") in ("created", "updated"):
                    count += 1
        except Exception as e:
            self.sink.record_run(self.name, cursor, count, f"{type(e).__name__}: {e}")
            raise
        self.sink.record_run(self.name, self.cursor, count, None)
        return count


class MarkdownDir(Connector):
    """Ingest a directory tree of markdown files (an inbox, an Obsidian vault...)."""

    type = "markdown_dir"

    def collect(self, cursor: str) -> Iterator[dict[str, Any]]:
        root = Path(self.options["path"]).expanduser()
        if not root.is_dir():
            raise FileNotFoundError(f"markdown_dir path not found: {root}")
        last = float(cursor) if cursor else 0.0
        newest = last
        for path in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            mtime = path.stat().st_mtime
            if mtime <= last:
                continue
            newest = max(newest, mtime)
            yield notes_io.file_to_fields(path, root)
        self.cursor = repr(newest)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return ""


def parse_session(path: Path) -> dict[str, Any] | None:
    """Parse one Claude Code style JSONL session into item fields (None if empty)."""
    turns: list[str] = []
    first_ts: float | None = None
    first_user = ""
    total = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            msg = rec.get("message")
            role = (msg or {}).get("role") if isinstance(msg, dict) else rec.get("type")
            if role not in ("user", "assistant"):
                continue
            text = _text_of((msg or {}).get("content") if isinstance(msg, dict) else rec.get("content"))
            text = text.strip()
            if not text or text.startswith("<system-reminder>"):
                continue
            text = redact(text)
            ts = notes_io.to_timestamp(rec.get("timestamp"))
            if ts and first_ts is None:
                first_ts = ts
            if role == "user" and not first_user:
                first_user = text
            snippet = text[:MAX_MSG_CHARS] + (" …[truncated]" if len(text) > MAX_MSG_CHARS else "")
            turns.append(f"{role}: {snippet}")
            total += len(snippet)
            if total > MAX_TRANSCRIPT_CHARS:
                turns.append("…[transcript truncated]")
                break
    if not turns:
        return None
    return {
        "title": (first_user.splitlines()[0][:100] if first_user else path.stem),
        "body": "\n\n".join(turns),
        "kind": "session",
        "created_at": first_ts or path.stat().st_mtime,
        "source_ref": f"{path.parent.name}/{path.stem}",
        "tags": f"claude-session, {path.parent.name}",
    }


# Project dirs whose sessions are our own tooling (enrichment runs), never ingested.
SESSION_EXCLUDE = ("brain-enrich-cwd",)


class ClaudeSessions(Connector):
    """Index local Claude Code session logs (~/.claude/projects/**/*.jsonl).

    These are plain text on disk but are cleaned up after a retention period;
    indexing them keeps old conversations searchable. Run this on the device
    where the sessions live and push with a RemoteSink.
    """

    type = "claude_sessions"

    def collect(self, cursor: str) -> Iterator[dict[str, Any]]:
        root = Path(self.options.get("path", "~/.claude/projects")).expanduser()
        if not root.is_dir():
            raise FileNotFoundError(f"sessions path not found: {root}")
        last = float(cursor) if cursor else 0.0
        newest = last
        for path in sorted(root.rglob("*.jsonl")):
            if any(x in str(path.parent) for x in SESSION_EXCLUDE):
                continue
            mtime = path.stat().st_mtime
            if mtime <= last:
                continue
            newest = max(newest, mtime)
            fields = parse_session(path)
            if fields:
                yield fields
        self.cursor = repr(newest)


class GitLog(Connector):
    """One item per repo per day listing commits. Re-reads the last few days each
    run so same-day commits update the existing item."""

    type = "git_log"

    def collect(self, cursor: str) -> Iterator[dict[str, Any]]:
        repos = [Path(p).expanduser() for p in self.options.get("repos", [])]
        lookback = int(self.options.get("lookback_days", 3))
        since = (
            dt.datetime.fromtimestamp(float(cursor), dt.timezone.utc) - dt.timedelta(days=lookback)
            if cursor
            else dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(self.options.get("initial_days", 90)))
        )
        for repo in repos:
            if not (repo / ".git").exists():
                continue
            proc = subprocess.run(
                ["git", "-C", str(repo), "log", f"--since={since.isoformat()}",
                 "--pretty=format:%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1e"],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:  # e.g. repo with no commits yet; don't fail the rest
                continue
            out = proc.stdout
            by_day: dict[str, list[tuple[str, str, str, str]]] = {}
            for rec in out.split("\x1e"):
                rec = rec.strip("\n")
                if not rec:
                    continue
                sha, when, author, subject, body = (rec.split("\x1f") + [""] * 5)[:5]
                by_day.setdefault(when[:10], []).append((sha[:8], author, subject, body.strip()))
            for day, commits in sorted(by_day.items()):
                lines = []
                for sha, author, subject, body in commits:
                    lines.append(redact(f"- {sha} {subject} ({author})"))
                    if body:
                        lines.append("  " + redact(body).replace("\n", "\n  "))
                yield {
                    "title": f"{repo.name} commits {day}",
                    "body": "\n".join(lines),
                    "kind": "commits",
                    "created_at": notes_io.to_timestamp(day),
                    "source_ref": f"{repo.name}:{day}",
                    "tags": f"git, {repo.name}",
                }
        import time as _t
        self.cursor = repr(_t.time())


REGISTRY: dict[str, type[Connector]] = {
    c.type: c for c in (MarkdownDir, ClaudeSessions, GitLog)
}


def build_connector(spec: dict[str, Any], sink: Sink) -> Connector:
    kind = spec["type"]
    if kind not in REGISTRY:
        raise ValueError(f"unknown connector type {kind!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[kind](
        name=spec["name"], sink=sink, scope=spec.get("scope"), **(spec.get("options") or {})
    )


def run_specs(specs: list[dict[str, Any]], sink: Sink, only: str | None = None,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """Run each configured connector once. Failures are isolated per connector."""
    results: dict[str, Any] = {}
    for spec in specs:
        if only and spec["name"] != only:
            continue
        try:
            n = build_connector(spec, sink).run()
            results[spec["name"]] = {"ok": True, "ingested": n}
        except Exception as e:
            results[spec["name"]] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            log(f"connector {spec['name']} failed: {e}")
    return results
