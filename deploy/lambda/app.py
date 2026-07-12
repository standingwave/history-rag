"""AWS Lambda entrypoint for the history MCP server.

A read-only replica (wip/SPEC-lambda-remote.md): the index is downloaded from
S3 to /tmp (ETag-cached across warm invocations) and served over stateless
streamable HTTP behind a secret path segment. server.py is imported
unmodified — everything remote-specific lives here.

Required env:
  CLAUDE_RAG_SYNC_BUCKET   S3 bucket holding the index
  CLAUDE_RAG_URL_SECRET    random hex path segment gating all requests
  CLAUDE_RAG_EMBED_BACKEND embed backend matching the indexed model — no
                           Ollama here: "mixedbread-api" or "nomic-api"
  MXBAI_API_KEY            key for the above (or NOMIC_API_KEY)
Optional env:
  CLAUDE_RAG_SYNC_KEY      S3 object key       (default history-rag.db)
  CLAUDE_RAG_DB_FRESHNESS  seconds between S3 ETag checks (default 300)
"""
import html, json, os, sys, time
from urllib.parse import parse_qs

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

# ── /search: a no-JS HTML page for phone browsers (wip/SPEC-direct-access) ──
# Same secret gate as MCP; every rendered value passes html.escape because
# the index holds attacker-influenceable text (page titles, commit messages).

_STYLE = """
body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0 auto;
     max-width:40rem;padding:1rem;background:#fff;color:#111}
form{display:flex;gap:.5rem;margin-bottom:1rem}
input{flex:1;font-size:1rem;padding:.6rem;border:1px solid #999;
      border-radius:.5rem;min-width:0}
button{font-size:1rem;padding:.6rem 1rem}
article{border-top:1px solid #ddd;padding:.75rem 0}
article header{color:#555;font-size:.85rem;margin-bottom:.25rem}
pre{white-space:pre-wrap;word-break:break-word;margin:.25rem 0;font:inherit}
a{color:#0645ad}
footer{color:#888;font-size:.75rem;margin-top:2rem}
@media(prefers-color-scheme:dark){
  body{background:#111;color:#ddd}
  input{background:#222;color:#ddd;border-color:#555}
  article{border-color:#333}
  article header{color:#999}
  a{color:#8ab4f8}}
"""

def _esc(v) -> str:
    return html.escape(str(v))

def _page(inner: str) -> str:
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>history</title><style>{_STYLE}</style></head><body>"
            f"{inner}<footer>queries travel in the URL — they stay in this "
            "browser's history</footer></body></html>")

def _form(q: str = "") -> str:
    return ('<form method="get">'
            f'<input type="search" name="q" value="{_esc(q)}" '
            'placeholder="search history" autofocus>'
            "<button>Search</button></form>")

def _render_results(q: str, k: int) -> str:
    data = json.loads(server.search_history(q, k=k))
    if "error" in data:
        return _page(_form(q) + f"<p>{_esc(data['error'])}</p>")
    parts = [_form(q)]
    for r in data.get("results", []):
        head = f"#{r['rank']} · <b>{_esc(r['source'])}</b>"
        if r.get("timestamp"):
            head += f" · {_esc(r['timestamp'][:10])}"
        if r.get("location"):
            head += f" · {_esc(r['location'])}"
        parts.append(f"<article><header>{head}</header>"
                     f"<pre>{_esc(r['text'])}</pre>"
                     f'<a href="search?expand={_esc(r["id"])}">expand</a>'
                     "</article>")
    if not data.get("results"):
        parts.append("<p>no matches</p>")
    return _page("".join(parts))

def _render_expand(cid: str) -> str:
    data = json.loads(server.expand(cid))
    back = '<p><a href="search">&larr; search</a></p>'
    if "error" in data:
        return _page(back + f"<p>{_esc(data['error'])}</p>")
    c = data["chunk"]
    head = f"<b>{_esc(c['source'])}</b>"
    if c.get("timestamp"):
        head += f" · {_esc(c['timestamp'])}"
    if c.get("location"):
        head += f" · {_esc(c['location'])}"
    parts = [back, f"<article><header>{head}</header>"
                   f"<pre>{_esc(c['text'])}</pre></article>"]
    if data.get("context") is not None:
        ctx = json.dumps(data["context"], indent=2, ensure_ascii=False)
        parts.append(f"<article><header>context "
                     f"({_esc(data.get('context_source'))})</header>"
                     f"<pre>{_esc(ctx)}</pre></article>")
    return _page("".join(parts))

async def _send_html(send, body: str):
    data = body.encode()
    await send({"type": "http.response.start", "status": 200, "headers": [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(data)).encode()),
        (b"content-security-policy",
         b"default-src 'none'; style-src 'unsafe-inline'"),
        (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": data})

async def _search_page(scope, send):
    qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
    expand_id = (qs.get("expand") or [""])[0].strip()
    query = (qs.get("q") or [""])[0].strip()
    if expand_id:
        body = _render_expand(expand_id)
    elif query:
        try:
            k = int((qs.get("k") or ["5"])[0])
        except ValueError:
            k = 5
        body = _render_results(query, max(1, min(k, 25)))
    else:
        body = _page(_form())
    await _send_html(send, body)

_inner = None

async def _gate(scope, receive, send):
    """Rebuild the MCP app on every lifespan startup, then gate HTTP behind
    the secret path. Mangum opens a fresh event loop and runs the lifespan
    once per invocation, but the SDK's StreamableHTTPSessionManager refuses
    to run twice — so each cycle drops FastMCP's cached manager and builds a
    new app bound to the current loop. The endpoint mounts at /mcp."""
    global _inner
    if scope["type"] != "http":
        server.mcp._session_manager = None
        _inner = server.mcp.streamable_http_app()
        await _inner(scope, receive, send)
        return
    prefix = "/" + SECRET
    path = scope.get("path", "")
    if path == prefix or path.startswith(prefix + "/"):
        _refresh_db()
        sub = path[len(prefix):] or "/"
        if sub == "/search" and scope.get("method") == "GET":
            await _search_page(scope, send)
            return
        await _inner({**scope, "path": sub}, receive, send)
    else:
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

handler = Mangum(_gate, lifespan="on")
