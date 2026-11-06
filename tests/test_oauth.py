"""OAuth resource-server behaviour: Pocket-ID-style JWTs accepted, everything else rejected."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from brain.server import OAuthConfig, create_app

ISSUER = "https://id.example.test"
CLIENT = "claude-client-id"
OWNER = "user-owner-1"
RESOURCE = "https://brain.example.test"
STATIC = "static-token-xyz"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwkClient:
    class _K:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._K()


def mint(sub=OWNER, aud=CLIENT, iss=ISSUER, exp_in=600, key=KEY, alg="RS256"):
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in},
                      key, algorithm=alg, headers={"typ": "at+jwt"})


@pytest.fixture
def client(store):
    oauth = OAuthConfig(ISSUER, [CLIENT], [OWNER], RESOURCE + "/mcp", jwk_client=FakeJwkClient())
    app = create_app(store, token=STATIC, oauth=oauth)
    with TestClient(app, base_url=RESOURCE) as c:
        yield c


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HDR = {"Accept": "application/json, text/event-stream"}


def post_mcp(c, token=None):
    h = dict(HDR)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", json=INIT, headers=h)


def test_valid_oauth_token_accepted_on_mcp_and_rest(client):
    t = mint()
    assert post_mcp(client, t).status_code == 200
    assert client.get("/api/status", headers={"Authorization": f"Bearer {t}"}).status_code == 200


def test_static_token_still_works(client):
    assert post_mcp(client, STATIC).status_code == 200
    assert client.get("/api/status", headers={"Authorization": f"Bearer {STATIC}"}).status_code == 200


@pytest.mark.parametrize("bad", [
    lambda: mint(sub="someone-else"),            # other Pocket ID user
    lambda: mint(sub="client-" + CLIENT),        # machine token
    lambda: mint(aud="another-client"),          # token for a different app
    lambda: mint(iss="https://evil.test"),       # wrong issuer
    lambda: mint(exp_in=-10),                    # expired
    lambda: mint(key=OTHER_KEY),                 # bad signature
    lambda: "not.a.jwt",
])
def test_bad_tokens_rejected(client, bad):
    t = bad()
    assert post_mcp(client, t).status_code == 401
    assert client.get("/api/status", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_alg_none_rejected(client):
    t = jwt.encode({"sub": OWNER, "aud": [CLIENT], "iss": ISSUER, "exp": int(time.time()) + 600}, key=None,
                   algorithm="none")
    assert post_mcp(client, t).status_code == 401


def test_challenge_points_at_metadata(client):
    r = post_mcp(client)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert "resource_metadata=" in www and "/.well-known/oauth-protected-resource" in www


def test_protected_resource_metadata_is_public_and_correct(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["resource"].rstrip("/") == RESOURCE + "/mcp"
    assert d["authorization_servers"][0].rstrip("/") == ISSUER


def test_healthz_and_ui_stay_open(client):
    assert client.get("/healthz").status_code == 200


def test_ui_config_public_and_ui_client_accepted(store):
    ui_client = "gui-client-id"
    oauth = OAuthConfig(ISSUER, [CLIENT, ui_client], [OWNER], RESOURCE + "/mcp", jwk_client=FakeJwkClient())
    app = create_app(store, token=STATIC, oauth=oauth, ui_client_id=ui_client)
    with TestClient(app, base_url=RESOURCE) as c:
        r = c.get("/ui-config")
        assert r.status_code == 200
        assert r.json() == {"oidc": {"issuer": ISSUER, "client_id": ui_client}}
        t = mint(aud=ui_client)
        assert c.get("/api/status", headers={"Authorization": f"Bearer {t}"}).status_code == 200


def test_ui_config_empty_without_ui_client(client):
    r = client.get("/ui-config")
    assert r.status_code == 200 and r.json() == {}


def test_pending_enrichment_roundtrip(client):
    h = {"Authorization": f"Bearer {STATIC}"}
    iid = client.post("/api/items", json={"title": "t", "body": "some body text"}, headers=h).json()["id"]
    pend = client.get("/api/pending-enrichment", headers=h).json()
    assert any(p["id"] == iid for p in pend)
    r = client.post(f"/api/items/{iid}/enrichment", json={"summary": "S", "questions": "Q?", "atoms": "A"}, headers=h)
    assert r.status_code == 200
    assert not any(p["id"] == iid for p in client.get("/api/pending-enrichment", headers=h).json())
    assert client.get("/api/pending-enrichment").status_code == 401
