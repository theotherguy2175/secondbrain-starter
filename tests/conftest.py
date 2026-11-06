import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from brain.server import create_app
from brain.store import Store

TOKEN = "test-token-123"


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def corpus(store):
    """A small but non-trivial corpus so BM25 has real IDF signal."""
    fillers = [
        ("Grocery list", "milk eggs bread apples", "note"),
        ("Gym plan", "squat bench deadlift three times weekly", "note"),
        ("Book ideas", "science fiction novel about a lighthouse", "note"),
        ("Trip to Maine", "booked cabin for october, bring boots", "note"),
        ("Router config", "vlan 20 for iot devices, dhcp range 10.0.20.0/24", "reference"),
        ("Printer setup", "cups driver installed on the server", "howto"),
    ]
    for title, body, kind in fillers:
        store.put(title=title, body=body, kind=kind)
    return store


class Server:
    def __init__(self, store):
        self.store = store
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        app = create_app(store, specs=[], token=TOKEN)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def request(self, method, path, body=None, token=TOKEN, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def server(store):
    srv = Server(store)
    yield srv
    srv.stop()
