"""AWS Lambda entrypoint for the history MCP server.

A read-only replica (wip/SPEC-lambda-remote.md): the index is downloaded from
S3 to /tmp (ETag-cached across warm invocations) and served over stateless
streamable HTTP behind a secret path segment. server.py is imported
unmodified — everything remote-specific lives here.

The browser UI (wip/SPEC-ui-rebuild.md) is a static client — ui.html +
ui.js, served verbatim — that calls the MCP endpoint from the page; this
module renders no HTML. Ask runs only as an explicit POST, so a paid model
call can never ride a navigation, prefetch, or back button.

Routes (behind the gate):
  GET  /            the UI shell (ui.html)
  GET  /ui.js       the UI script
  GET  /config      ask presets the client may show (display fields only)
  POST /ask         {q, model?, strict?} -> ask.ask() JSON
  GET  /api/search  search_history as JSON (q, k, source, location, since,
                    until, max_distance) — the Convex app's archive proxy
  GET  /api/window  list_window as JSON (since, until, source, location,
                    limit, offset, include_undated, group_by, include_meta,
                    summaries)
  GET  /api/expand  expand as JSON (id, context)
  GET  /search?...  legacy server-rendered-page URLs -> redirect to /#...
  *    /mcp         the MCP endpoint (unchanged)
  GET  /login?token=<secret>   sets the auth cookie (outside the gate)

The gate passes when the path carries the secret segment (unchanged — MCP
clients and hist keep working) or when the auth cookie holds it; /login
mints that cookie so page URLs stop carrying the credential.

Required env:
  CLAUDE_RAG_SYNC_BUCKET   S3 bucket holding the index
  CLAUDE_RAG_URL_SECRET    random hex path segment gating all requests
  CLAUDE_RAG_EMBED_BACKEND embed backend matching the indexed model — no
                           Ollama here: "hf-inference", "mixedbread-api",
                           or "nomic-api"
  HF_TOKEN                 key for the above (or MXBAI_API_KEY / NOMIC_API_KEY)
Optional env:
  CLAUDE_RAG_SYNC_KEY      S3 object key       (default history-rag.db)
  CLAUDE_RAG_DB_FRESHNESS  seconds between S3 ETag checks (default 300)
  CLAUDE_RAG_ASK_MODELS    ask presets JSON; presets may carry `latency`
                           and `est_cost` display strings for the UI
"""
import hmac, json, os, sys, time
from urllib.parse import parse_qs, urlencode

# The managed runtime's sqlite3 is built without loadable-extension support
# (sqlite-vec needs it); substitute pysqlite3 before anything imports sqlite3.
import pysqlite3
sys.modules["sqlite3"] = pysqlite3

os.environ.setdefault("CLAUDE_RAG_DB", "/tmp/history-rag.db")

import boto3
from mangum import Mangum

BUCKET = os.environ["CLAUDE_RAG_SYNC_BUCKET"]
KEY = os.environ.get("CLAUDE_RAG_SYNC_KEY", "history-rag.db")
SECRET = os.environ["CLAUDE_RAG_URL_SECRET"]
FRESHNESS = int(os.environ.get("CLAUDE_RAG_DB_FRESHNESS", "300"))

COOKIE = "hist"
COOKIE_MAX_AGE = 365 * 24 * 3600

_s3 = boto3.client("s3")
_state = {"checked": 0.0, "etag": None}

def _refresh_db():
    """Fetch the index if missing or changed upstream. HEADs S3 at most once
    per FRESHNESS seconds per warm container — staleness is already bounded
    by the Mac's index-and-sync cadence, so polling harder buys nothing."""
    db = os.environ["CLAUDE_RAG_DB"]
    if os.path.exists(db) and time.monotonic() - _state["checked"] < FRESHNESS:
        return
    head = _s3.head_object(Bucket=BUCKET, Key=KEY)
    if head["ETag"] != _state["etag"] or not os.path.exists(db):
        _s3.download_file(BUCKET, KEY, db + ".part")
        os.replace(db + ".part", db)      # never expose a partial download
        _state["etag"] = head["ETag"]
    _state["checked"] = time.monotonic()

import server  # noqa: E402  — import after the env vars above are set
import ask     # noqa: E402  — the ask-mode agent loop (same env rule)

# Stateless + JSON responses: each invocation is self-contained (no session
# affinity to pin, no SSE stream to hold open), which is exactly the shape a
# buffered Function URL invocation can serve.
server.mcp.settings.stateless_http = True
server.mcp.settings.json_response = True

# The SDK's DNS-rebinding protection rejects any Host it wasn't told about
# (421). It defends localhost servers; here the URL secret is the gate and
# the host is whatever Lambda assigns, so turn it off.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
server.mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False)

# ── static UI files, read once per container ────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))

def _load(name: str) -> bytes:
    with open(os.path.join(_DIR, name), "rb") as f:
        return f.read()

_UI_HTML = _load("ui.html")
_UI_JS = _load("ui.js")

# The page builds all DOM from tool JSON via textContent; the CSP is the
# backstop — no inline script, nothing loads from anywhere but this origin.
_CSP = (b"default-src 'none'; style-src 'unsafe-inline'; "
        b"script-src 'self'; connect-src 'self'; img-src 'self'")

# ── response helpers ────────────────────────────────────────────────────────

async def _send(send, status, headers, body=b""):
    await send({"type": "http.response.start", "status": status,
                "headers": [*headers,
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})

async def _send_json(send, obj, status=200):
    await _send(send, status,
                [(b"content-type", b"application/json"),
                 (b"x-content-type-options", b"nosniff")],
                json.dumps(obj).encode())

async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            return body

def _cookie_ok(scope) -> bool:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and hmac.compare_digest(v, SECRET):
                    return True
    return False

def _qs(scope) -> dict:
    return parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))

# ── routes (already behind the gate) ────────────────────────────────────────

# Ask preset fields the page may see. Everything else in a preset —
# model id, base_url, key_env — is server-side configuration and never
# crosses the wire.
_PRESET_PUBLIC = ("name", "latency", "est_cost")

def _config() -> dict:
    return {"models": [{k: p[k] for k in _PRESET_PUBLIC if p.get(k)}
                       for p in ask.presets()]}

# Legacy /search params the redirect keeps. Execution markers (go, tab),
# transport switches (json, fragment), and dead knobs stay behind; the
# client decides what to run — and never runs ask from a URL.
_LEGACY_KEEP = ("q", "mode", "source", "since", "until", "view", "offset",
                "model", "expand")

def _api(tool: str, qs: dict):
    """The MCP tools over plain GET, parsed JSON out — what the Convex app's
    archive proxy calls (wip/SPEC-convex-app.md stage 1). Unknown -> None."""
    p = lambda k, d="": qs.get(k, [d])[0]
    flag = lambda k: p(k).lower() in ("1", "true", "yes")
    if tool == "search":
        return json.loads(server.search_history(
            p("q"), k=int(p("k", "10") or 10), source=p("source"),
            location=p("location"), since=p("since"), until=p("until"),
            include_undated=flag("include_undated"),
            max_distance=float(p("max_distance", "0") or 0)))
    if tool == "window":
        return json.loads(server.list_window(
            since=p("since"), until=p("until"), source=p("source"),
            location=p("location"), limit=int(p("limit", "50") or 50),
            offset=int(p("offset", "0") or 0),
            include_undated=flag("include_undated"), group_by=p("group_by"),
            include_meta=flag("include_meta"), summaries=flag("summaries")))
    if tool == "expand":
        return json.loads(server.expand(p("id"),
                                        context=int(p("context", "5") or 5)))
    return None

async def _handle(mount, sub, scope, receive, send):
    method = scope.get("method", "GET")
    if sub == "/" and method == "GET":
        await _send(send, 200,
                    [(b"content-type", b"text/html; charset=utf-8"),
                     (b"content-security-policy", _CSP),
                     (b"x-content-type-options", b"nosniff"),
                     (b"cache-control", b"no-cache")], _UI_HTML)
    elif sub == "/ui.js" and method == "GET":
        await _send(send, 200,
                    [(b"content-type", b"text/javascript; charset=utf-8"),
                     (b"x-content-type-options", b"nosniff"),
                     (b"cache-control", b"no-cache")], _UI_JS)
    elif sub == "/config" and method == "GET":
        await _send_json(send, _config())
    elif sub == "/ask":
        if method != "POST":
            await _send(send, 405, [(b"allow", b"POST")])
            return
        _refresh_db()
        try:
            payload = json.loads((await _read_body(receive)) or b"{}")
            q = (payload.get("q") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            await _send_json(send, {"error": "malformed request body"}, 400)
            return
        if not q:
            await _send_json(send, {"error": "q is required"}, 400)
            return
        result = ask.ask(q, payload.get("model") or "",
                         strict=bool(payload.get("strict")))
        await _send_json(send, result)
    elif sub.startswith("/api/") and method == "GET":
        _refresh_db()
        out = _api(sub[5:], _qs(scope))
        if out is None:
            await _send(send, 404, [])
        else:
            await _send_json(send, out)
    elif sub == "/search" and method == "GET":
        qs = _qs(scope)
        keep = {k: qs[k][0] for k in _LEGACY_KEEP if qs.get(k, [""])[0]}
        target = mount + "/#" + urlencode(keep)
        await _send(send, 302, [(b"location", target.encode())])
    elif sub == "/mcp" or sub.startswith("/mcp/"):
        _refresh_db()
        await _inner({**scope, "path": sub}, receive, send)
    else:
        await _send(send, 404, [])

async def _login(scope, send):
    token = _qs(scope).get("token", [""])[0]
    if not hmac.compare_digest(token, SECRET):
        await _send(send, 404, [])
        return
    cookie = (f"{COOKIE}={SECRET}; Path=/; Max-Age={COOKIE_MAX_AGE}; "
              "Secure; HttpOnly; SameSite=Lax")
    await _send(send, 302, [(b"location", b"/"),
                            (b"set-cookie", cookie.encode())])

_inner = None

async def _gate(scope, receive, send):
    """Rebuild the MCP app on every lifespan startup, then gate HTTP behind
    the secret — carried either in the path (MCP clients, hist, legacy
    bookmarks) or in the auth cookie /login mints (the browser UI). Mangum
    opens a fresh event loop and runs the lifespan once per invocation, but
    the SDK's StreamableHTTPSessionManager refuses to run twice — so each
    cycle drops FastMCP's cached manager and builds a new app bound to the
    current loop. The endpoint mounts at /mcp."""
    global _inner
    if scope["type"] != "http":
        server.mcp._session_manager = None
        _inner = server.mcp.streamable_http_app()
        await _inner(scope, receive, send)
        return
    path = scope.get("path", "")
    if path == "/login" and scope.get("method") == "GET":
        await _login(scope, send)
        return
    prefix = "/" + SECRET
    if path == prefix and scope.get("method") == "GET":
        # no trailing slash: relative URLs on the page would resolve to /
        await _send(send, 302, [(b"location", (prefix + "/").encode())])
    elif path.startswith(prefix + "/"):
        await _handle(prefix, path[len(prefix):], scope, receive, send)
    elif _cookie_ok(scope):
        await _handle("", path or "/", scope, receive, send)
    else:
        await _send(send, 404, [])

handler = Mangum(_gate, lifespan="on")
