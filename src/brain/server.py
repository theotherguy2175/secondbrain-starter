"""HTTP server: MCP (streamable HTTP at /mcp) + REST API (/api/*) + web UI (/).

All of it sits behind one bearer token except /healthz and the static UI shell.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__, maintenance
from .store import Store

MAX_BODY_CHARS = 2_000_000
log = logging.getLogger("brain.auth")

MCP_INSTRUCTIONS = """\
This is the user's personal second brain: notes, decisions, home lab and project
knowledge, plus ingested git history. Use it economically; it is for user-specific
facts, not general knowledge.

When to use it:
- Skip it for general questions, generic coding help and small talk.
- For anything about the user's home lab, projects, past decisions or preferences, run 1-2
  narrow brain_search calls with specific keywords (hostnames, tool names, error text),
  not sentences. A third query only if both were thin.
- Start with titles_only=true when scanning; read only the 1-2 best hits with
  brain_read(id). Do not read many items "just in case".
- Results are recency-weighted; check `age_days` and prefer newer items when they conflict.
- When you rely on an item, cite its title (and id) in your answer.

Writing:
- When you learn something durable (a decision and its reason, a fix that worked, a
  config change, a preference), brain_write it: clear title, the exact command/error/
  value, and WHY. Use brain_append to extend an existing note instead of duplicating.
- Never store secrets (passwords, tokens, private keys).
- Tip: brain_search "orientation map" returns a short overview of what the brain holds.
"""


# ---------------------------------------------------------------- MCP tools
def build_mcp(store: Store, specs: list[dict[str, Any]] | None = None, *, auth: AuthSettings | None = None,
              token_verifier: Any = None) -> MCPServer:
    mcp = MCPServer("brain", instructions=MCP_INSTRUCTIONS, version=__version__, auth=auth,
                    token_verifier=token_verifier)

    @mcp.tool()
    def brain_search(query: str, limit: int = 8, scope: str | None = None, source: str | None = None,
                     kind: str | None = None, since_days: float | None = None,
                     titles_only: bool = False) -> list[dict[str, Any]]:
        """Search the second brain (BM25 keyword search with recency weighting).

        Use specific keywords. Returns the best matches with id, title, summary,
        a highlighted snippet, source, kind, age_days and score. Filter by scope
        (e.g. 'personal', 'homelab'), source, kind, or since_days. With
        titles_only=true, returns just id/title/kind/scope/age_days (cheap scanning)."""
        hits = store.search(query, limit=max(1, min(limit, 25)), scope=scope, source=source,
                            kind=kind, since_days=since_days)
        if titles_only:
            keep = ("id", "title", "kind", "scope", "age_days")
            hits = [{k: h[k] for k in keep if k in h} for h in hits]
        return hits

    @mcp.tool()
    def brain_read(id: str, max_chars: int = 8000) -> dict[str, Any]:
        """Read one item by id (as returned by brain_search / brain_recent). The body is
        truncated to max_chars (default 8000); `truncated` and `body_chars` say if it was cut."""
        item = store.get(id)
        if not item:
            return {"error": f"no item with id {id}"}
        body = item.get("body") or ""
        cap = max(500, min(max_chars, 40000))
        if len(body) > cap:
            item = {**item, "body": body[:cap] + "\n…[truncated]", "truncated": True, "body_chars": len(body)}
        return item

    @mcp.tool()
    def brain_write(title: str, body: str, kind: str = "note", tags: str = "",
                    scope: str = "personal", id: str | None = None, pinned: bool = False) -> dict[str, Any]:
        """Create a note, or update one if `id` is given.

        kinds: note, decision, fix, howto, reference, preference, project. Pin
        evergreen facts (pinned items are exempt from recency decay and pruning).
        Never store secrets."""
        if len(body) > MAX_BODY_CHARS:
            return {"error": "body too large"}
        return store.put(title=title, body=body, kind=kind, tags=tags, scope=scope, id=id,
                         source="agent", pinned=pinned or None)

    @mcp.tool()
    def brain_append(id: str, text: str) -> dict[str, Any]:
        """Append text to the end of an existing item (a log entry, a follow-up, an update)."""
        res = store.append(id, text)
        return res if res else {"error": f"no item with id {id}"}

    @mcp.tool()
    def brain_recent(limit: int = 10, kind: str | None = None, source: str | None = None,
                     scope: str | None = None) -> list[dict[str, Any]]:
        """List the most recently updated items (good for 'what was I working on?')."""
        return store.recent(max(1, min(limit, 50)), kind=kind, source=source, scope=scope)

    @mcp.tool()
    def brain_status() -> dict[str, Any]:
        """Item counts and connector health. Check this if results look stale."""
        return {"stats": store.stats(), "health": maintenance.health(store, specs)}

    return mcp


# --------------------------------------------------------------------- REST
def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return _json({"error": msg}, status)


def build_routes(store: Store, specs: list[dict[str, Any]], ui_oidc: dict[str, str] | None = None) -> list[Route]:
    ui_path = Path(__file__).parent / "ui" / "index.html"

    def healthz(request: Request) -> Response:
        return _json({"ok": True, "version": __version__})

    def ui_config(request: Request) -> Response:
        # Public: tells the web UI how to run the browser sign-in. Contains no secrets.
        return _json({"oidc": ui_oidc} if ui_oidc else {})

    def ui(request: Request) -> Response:
        try:
            return HTMLResponse(ui_path.read_text(encoding="utf-8"))
        except OSError:
            return HTMLResponse("<h1>brain</h1><p>UI not installed.</p>", status_code=404)

    def search(request: Request) -> Response:
        q = request.query_params.get("q", "").strip()
        if not q:
            return _bad("missing q")
        p = request.query_params
        try:
            limit = max(1, min(int(p.get("limit", 10)), 50))
            since = float(p["since_days"]) if p.get("since_days") else None
        except ValueError:
            return _bad("limit/since_days must be numbers")
        return _json(store.search(q, limit=limit, scope=p.get("scope"), source=p.get("source"),
                                  kind=p.get("kind"), since_days=since))

    def recent(request: Request) -> Response:
        p = request.query_params
        try:
            limit = max(1, min(int(p.get("limit", 20)), 100))
        except ValueError:
            return _bad("limit must be a number")
        return _json(store.recent(limit, kind=p.get("kind"), source=p.get("source"), scope=p.get("scope")))

    def list_items(request: Request) -> Response:
        p = request.query_params
        try:
            limit = max(1, min(int(p.get("limit", 200)), 500))
            offset = max(0, int(p.get("offset", 0)))
        except ValueError:
            return _bad("limit/offset must be numbers")
        sources = [x for x in p.get("source", "").split(",") if x] or None
        include_archived = p.get("include_archived", "") in ("1", "true", "yes")
        return _json(store.list_items(sources, include_archived, limit, offset))

    def get_item(request: Request) -> Response:
        item = store.get(request.path_params["id"])
        return _json(item) if item else _bad("not found", 404)

    async def create_item(request: Request) -> Response:
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _bad("invalid JSON")
        if not isinstance(data, dict):
            return _bad("expected a JSON object")
        title, body = str(data.get("title", "")), str(data.get("body", ""))
        if not (title or body):
            return _bad("title or body required")
        if len(body) > MAX_BODY_CHARS:
            return _bad("body too large", 413)
        allowed = {"id", "source", "source_ref", "kind", "summary", "questions", "atoms", "tags",
                   "scope", "pinned", "created_at"}
        extra = {k: v for k, v in data.items() if k in allowed and v is not None}
        extra.setdefault("source", "api")
        try:
            return _json(store.put(title=title, body=body, **extra))
        except TypeError as e:
            return _bad(str(e))

    def pending_enrichment(request: Request) -> Response:
        try:
            limit = max(1, min(int(request.query_params.get("limit", 10)), 50))
        except ValueError:
            return _bad("limit must be a number")
        items = store.needing_llm(limit)
        return _json([{k: i[k] for k in ("id", "title", "kind", "body", "source", "scope")} for i in items])

    async def set_item_enrichment(request: Request) -> Response:
        try:
            d = await request.json()
            summary, questions, atoms = str(d["summary"]), str(d.get("questions", "")), str(d.get("atoms", ""))
        except (ValueError, KeyError, TypeError):
            return _bad("need JSON with summary (+ questions, atoms)")
        item_id = request.path_params["id"]
        if store.get(item_id, touch=False) is None:
            return _bad("not found", 404)
        store.set_enrichment(item_id, summary[:2000], questions[:4000], atoms[:8000], level=2)
        return _json({"id": item_id, "action": "enriched"})

    async def update_item(request: Request) -> Response:
        item_id = request.path_params["id"]
        item = store.get(item_id, touch=False)
        if item is None:
            return _bad("not found", 404)
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _bad("invalid JSON")
        if not isinstance(data, dict):
            return _bad("expected a JSON object")
        merged = {k: data.get(k, item[k]) for k in ("title", "body", "kind", "tags", "scope")}
        if len(str(merged["body"])) > MAX_BODY_CHARS:
            return _bad("body too large", 413)
        pinned = bool(data["pinned"]) if "pinned" in data else None
        res = store.put(id=item_id, source=item["source"], source_ref=item["source_ref"],
                        pinned=pinned, **{k: str(v) for k, v in merged.items()})
        return _json(res)

    def delete_item(request: Request) -> Response:
        item_id = request.path_params["id"]
        if request.query_params.get("hard") == "1":
            ok = store.delete(item_id)
        else:
            ok = store.archive(item_id)
        return _json({"ok": True}) if ok else _bad("not found", 404)

    async def append_item(request: Request) -> Response:
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _bad("invalid JSON")
        text = str(data.get("text", "")) if isinstance(data, dict) else ""
        if not text.strip():
            return _bad("text required")
        res = store.append(request.path_params["id"], text)
        return _json(res) if res else _bad("not found", 404)

    def status(request: Request) -> Response:
        return _json({"stats": store.stats(), "health": maintenance.health(store, specs),
                      "version": __version__})

    return [
        Route("/", ui, methods=["GET"]),
        Route("/ui", ui, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/ui-config", ui_config, methods=["GET"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/recent", recent, methods=["GET"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/api/items", list_items, methods=["GET"]),
        Route("/api/pending-enrichment", pending_enrichment, methods=["GET"]),
        Route("/api/items", create_item, methods=["POST"]),
        Route("/api/items/{id}/enrichment", set_item_enrichment, methods=["POST"]),
        Route("/api/items/{id}", get_item, methods=["GET"]),
        Route("/api/items/{id}", update_item, methods=["PUT"]),
        Route("/api/items/{id}", delete_item, methods=["DELETE"]),
        Route("/api/items/{id}/append", append_item, methods=["POST"]),
    ]


# --------------------------------------------------------------------- auth
class OAuthConfig:
    """Accept access tokens (JWTs) issued by an external OIDC provider such as Pocket ID.

    A token is accepted only if: the signature verifies against the issuer's JWKS, `iss` matches,
    `aud` contains one of `client_ids`, it has not expired, and `sub` is in `subjects`. The
    `subjects` pin is what stops other users of the same provider (and machine tokens) getting in.
    """

    def __init__(self, issuer: str, client_ids: list[str], subjects: list[str], resource_url: str,
                 jwks_url: str | None = None, jwk_client: Any = None):
        if not (client_ids and subjects):
            raise ValueError("OAuth needs BRAIN_OAUTH_CLIENT_IDS and BRAIN_OAUTH_SUBJECTS")
        self.issuer = issuer.rstrip("/")
        self.client_ids = client_ids
        self.subjects = subjects
        self.resource_url = resource_url
        if jwk_client is None:
            import jwt

            jwk_client = jwt.PyJWKClient(jwks_url or f"{self.issuer}/.well-known/jwks.json",
                                         cache_keys=True, lifespan=3600,
                                         headers={"User-Agent": "brain-oauth/1.0"})  # Cloudflare 403s urllib's default UA
        self.jwk_client = jwk_client

    def decode(self, token: str) -> dict[str, Any] | None:
        import jwt

        try:
            key = self.jwk_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.client_ids, options={"require": ["exp", "iss", "aud", "sub"]})
        except Exception as e:
            log.warning("oauth token rejected: %s: %s", type(e).__name__, str(e)[:120])
            return None
        if claims.get("sub") not in self.subjects:
            log.warning("oauth token rejected: sub %r not in allowed subjects (aud=%s)", claims.get("sub"), claims.get("aud"))
            return None
        return claims


class BrainTokenVerifier:
    """Verifies bearer tokens: the static BRAIN_TOKEN (agents/REST/Claude Code) or an OAuth JWT."""

    def __init__(self, static_token: str | None, oauth: OAuthConfig | None):
        self.static = static_token.encode() if static_token else None
        self.oauth = oauth

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.static and hmac.compare_digest(token.encode(), self.static):
            return AccessToken(token=token, client_id="static-token", scopes=[], subject="static-token")
        if self.oauth and token.count(".") == 2:
            claims = await asyncio.to_thread(self.oauth.decode, token)
            if claims:
                aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
                return AccessToken(token=token, client_id=str(claims.get("client_id") or aud[0]), scopes=[],
                                   expires_at=claims.get("exp"), subject=claims["sub"],
                                   resource=self.oauth.resource_url)
        return None


class BearerAuth:
    """Pure-ASGI bearer check for the REST API.

    Accepts `Authorization: Bearer <t>` or `X-Brain-Token`. /mcp and /.well-known are left to the MCP
    SDK's own auth middleware so it can emit the OAuth `WWW-Authenticate: ... resource_metadata=` challenge.
    """

    OPEN_PATHS = {"/", "/ui", "/healthz", "/ui-config"}

    def __init__(self, app, verifier: BrainTokenVerifier, metadata_url: str | None = None):
        self.app = app
        self.verifier = verifier
        self.challenge = "Bearer" + (f' resource_metadata="{metadata_url}"' if metadata_url else "")

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or path in self.OPEN_PATHS or scope["method"] == "OPTIONS"
                or path == "/mcp" or path.startswith(("/mcp/", "/.well-known/"))):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        supplied = headers.get("x-brain-token", "")
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if supplied and await self.verifier.verify_token(supplied):
            return await self.app(scope, receive, send)
        resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": self.challenge})
        await resp(scope, receive, send)


def create_app(store: Store, specs: list[dict[str, Any]] | None = None, token: str | None = None,
               allowed_hosts: list[str] | None = None, allow_no_auth: bool = False,
               oauth: OAuthConfig | None = None, ui_client_id: str | None = None) -> Starlette:
    if not token and not oauth and not allow_no_auth:
        raise RuntimeError("refusing to start without BRAIN_TOKEN (set BRAIN_ALLOW_NO_AUTH=1 to override)")
    specs = specs or []
    verifier = BrainTokenVerifier(token, oauth) if (token or oauth) else None
    auth_settings = None
    metadata_url = None
    if verifier:
        resource = oauth.resource_url if oauth else "http://localhost/mcp"
        auth_settings = AuthSettings(issuer_url=oauth.issuer if oauth else "http://localhost",
                                     resource_server_url=resource, validate_token_resource=False)
        if oauth:
            from urllib.parse import urlsplit

            u = urlsplit(resource)
            metadata_url = f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource{u.path}"
    mcp = build_mcp(store, specs, auth=auth_settings, token_verifier=verifier)
    if allowed_hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=allowed_hosts,
                                             allowed_origins=[f"https://{h}" for h in allowed_hosts]
                                             + [f"http://{h}" for h in allowed_hosts])
    else:
        # Protected by the bearer token instead; set BRAIN_ALLOWED_HOSTS to also pin Host headers.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app(transport_security=security, stateless_http=True, json_response=True)
    ui_oidc = {"issuer": oauth.issuer, "client_id": ui_client_id} if (oauth and ui_client_id) else None
    app.router.routes.extend(build_routes(store, specs, ui_oidc))
    if verifier:
        app.add_middleware(BearerAuth, verifier=verifier, metadata_url=metadata_url)
    return app


def oauth_from_env() -> OAuthConfig | None:
    issuer = os.environ.get("BRAIN_OAUTH_ISSUER")
    if not issuer:
        return None
    split = lambda k: [x.strip() for x in os.environ.get(k, "").split(",") if x.strip()]  # noqa: E731
    public = os.environ.get("BRAIN_PUBLIC_URL", "").rstrip("/")
    if not public:
        raise RuntimeError("BRAIN_OAUTH_ISSUER requires BRAIN_PUBLIC_URL (e.g. https://brain.example.com)")
    client_ids = split("BRAIN_OAUTH_CLIENT_IDS")
    ui_client = os.environ.get("BRAIN_UI_CLIENT_ID")
    if ui_client and ui_client not in client_ids:
        client_ids.append(ui_client)  # the web UI's own client is accepted too
    return OAuthConfig(issuer, client_ids, split("BRAIN_OAUTH_SUBJECTS"),
                       resource_url=f"{public}/mcp", jwks_url=os.environ.get("BRAIN_OAUTH_JWKS_URL"))


def app_from_env() -> Starlette:
    """Factory used by uvicorn: `uvicorn --factory brain.server:app_from_env`."""
    from .worker import load_config

    store = Store(os.environ.get("BRAIN_DB", "/data/brain.db"))
    cfg = load_config(os.environ.get("BRAIN_CONFIG"))
    hosts = [h.strip() for h in os.environ.get("BRAIN_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return create_app(
        store, cfg.get("connectors", []), token=os.environ.get("BRAIN_TOKEN"),
        allowed_hosts=hosts or None, allow_no_auth=os.environ.get("BRAIN_ALLOW_NO_AUTH") == "1",
        oauth=oauth_from_env(), ui_client_id=os.environ.get("BRAIN_UI_CLIENT_ID"),
    )
