"""Sinks: where connectors write.

Connectors never touch the database directly. They write through a sink, so the
same connector code runs in two places:

* LocalSink  - inside the cluster, writing straight to the SQLite store.
* RemoteSink - on a laptop / other machine, pushing over HTTPS to the server
  (used for things that only exist on a device: Claude session logs, local git
  repos). Cursor state is kept in a small local JSON file.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .store import Store


class Sink(Protocol):
    def put(self, **fields: Any) -> dict[str, Any]: ...
    def get_cursor(self, name: str) -> str: ...
    def record_run(self, name: str, cursor: str, count: int, error: str | None) -> None: ...


class LocalSink:
    def __init__(self, store: Store, default_scope: str = "personal"):
        self.store = store
        self.default_scope = default_scope

    def put(self, **fields: Any) -> dict[str, Any]:
        if not fields.get("scope"):
            fields["scope"] = self.default_scope
        return self.store.put(**fields)

    def get_cursor(self, name: str) -> str:
        return self.store.get_state(name)["cursor"]

    def record_run(self, name: str, cursor: str, count: int, error: str | None) -> None:
        self.store.record_run(name, cursor, count, error)


class RemoteSink:
    """Pushes items to a brain server's REST API with a bearer token."""

    def __init__(self, url: str, token: str, state_path: str | Path = "~/.brain/state.json",
                 default_scope: str = "personal", timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.token = token
        self.state_path = Path(state_path).expanduser()
        self.default_scope = default_scope
        self.timeout = timeout

    # -- state is local to the device
    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2))

    def get_cursor(self, name: str) -> str:
        return self._load().get(name, {}).get("cursor", "")

    def record_run(self, name: str, cursor: str, count: int, error: str | None) -> None:
        state = self._load()
        prev = state.get(name, {})
        state[name] = {
            "cursor": cursor if error is None else prev.get("cursor", ""),
            "last_run": time.time(),
            "last_error": error,
            "last_count": count,
        }
        self._save(state)

    # -- items go to the server
    def put(self, **fields: Any) -> dict[str, Any]:
        if not fields.get("scope"):
            fields["scope"] = self.default_scope
        payload = {k: v for k, v in fields.items() if v is not None}
        req = urllib.request.Request(
            f"{self.url}/api/items",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "User-Agent": "brain-cli/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"server rejected item ({e.code}): {e.read()[:200]!r}") from e
