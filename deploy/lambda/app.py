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
import base64, hashlib, html, json, os, sys, time
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
form{margin-bottom:1rem}
.row{display:flex;gap:.5rem}
.row input{flex:1;min-width:0}
input,select{font:inherit;padding:.5rem;border:1px solid #999;
             border-radius:.5rem;background:#fff;color:inherit}
input[type=checkbox]{width:auto;padding:0}
button{font-size:1rem;padding:.6rem 1rem}
details{margin-top:.5rem;font-size:.9rem}
details summary{color:#555}
.filters{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;
         margin-top:.5rem}
.filters label{display:flex;flex-direction:column;gap:.15rem;color:#555}
.filters .check{flex-direction:row;align-items:center;gap:.4rem;
                grid-column:1/-1}
.note{background:#fff3cd;border:1px solid #e0c366;border-radius:.5rem;
      padding:.5rem .75rem}
.health{color:#888;font-size:.8rem;margin:.25rem 0}
article{border-top:1px solid #ddd;padding:.75rem 0}
article header{color:#555;font-size:.85rem;margin-bottom:.25rem}
pre{white-space:pre-wrap;word-break:break-word;margin:.25rem 0;font:inherit}
a{color:#0645ad}
footer{color:#888;font-size:.75rem;margin-top:2rem}
@media(prefers-color-scheme:dark){
  body{background:#111;color:#ddd}
  input,select{background:#222;color:#ddd;border-color:#555}
  details summary,.filters label{color:#999}
  .note{background:#3a3000;border-color:#7a6a1a}
  article{border-color:#333}
  article header{color:#999}
  a{color:#8ab4f8}}
"""

# The page's single scripted enhancement: disable the button + show
# progress during submit, re-enable on bfcache back-navigation. ASCII-only
# (\\u2026 escape) so the hashed bytes are unambiguous; the CSP pins this
# exact script, so injected chunk text still cannot execute.
_SCRIPT = ("(function(){var f=document.querySelector('form');if(!f)return;"
           "var b=f.querySelector('button');"
           "f.addEventListener('submit',function(){b.disabled=true;"
           "b.textContent='Searching\\u2026'});"
           "window.addEventListener('pageshow',function(){b.disabled=false;"
           "b.textContent='Search'})})();")
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-"
        + base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
        + "'").encode()

def _esc(v) -> str:
    return html.escape(str(v))

def _page(inner: str) -> str:
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>history</title><style>{_STYLE}</style></head><body>"
            f"{inner}<footer>queries travel in the URL — they stay in this "
            f"browser's history</footer><script>{_SCRIPT}</script>"
            "</body></html>")

def _int(raw: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return default

def _stats() -> dict:
    """history_stats feeds the source dropdown and the health banner; a
    broken stats call must not take the page down with it."""
    try:
        return json.loads(server.history_stats())
    except Exception:
        return {}

def _age(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"

def _banner(stats: dict) -> str:
    """Health above the form: warnings when something's wrong, plus an
    always-on freshness line so staleness is visible before it's dire."""
    h = stats.get("health") or {}
    if not h:
        return ""
    parts = []
    if h.get("note"):
        parts.append(f'<p class="note">{_esc(h["note"])}</p>')
    if h.get("failing_sources"):
        parts.append('<p class="note">failing sources: '
                     f'{_esc(", ".join(sorted(h["failing_sources"])))}</p>')
    if h.get("age_minutes") is not None:
        parts.append(f'<p class="health">index {_esc(h.get("status", "?"))} '
                     f'· refreshed {_age(int(h["age_minutes"]))} ago</p>')
    return "".join(parts)

def _form(g, stats: dict) -> str:
    source, since, until = g("source"), g("since"), g("until")
    location, undated = g("location"), bool(g("undated"))
    k = _int(g("k"), 5, 1, 25)
    active = any([source, since, until, location, undated, k != 5])
    names = sorted(stats.get("sources", {}))
    if source and source not in names:
        names.append(source)   # keep an active out-of-catalog filter visible
    opts = [f'<option value=""{"" if source else " selected"}>All</option>']
    for s in names:
        sel = " selected" if s == source else ""
        opts.append(f'<option value="{_esc(s)}"{sel}>{_esc(s)}</option>')
    return (
        '<form method="get">'
        '<div class="row">'
        f'<input type="search" name="q" value="{_esc(g("q"))}" '
        'placeholder="search history" autofocus>'
        "<button>Search</button></div>"
        f'<details{" open" if active else ""}><summary>filters</summary>'
        '<div class="filters">'
        f'<label>source <select name="source">{"".join(opts)}</select></label>'
        f'<label>results <input type="number" name="k" min="1" max="25" '
        f'value="{k}"></label>'
        f'<label>since <input type="date" name="since" value="{_esc(since)}">'
        "</label>"
        f'<label>until <input type="date" name="until" value="{_esc(until)}">'
        "</label>"
        f'<label>location <input name="location" value="{_esc(location)}" '
        'placeholder="prefix, e.g. chrome:"></label>'
        f'<label class="check"><input type="checkbox" name="undated" '
        f'value="1"{" checked" if undated else ""}> include undated</label>'
        "</div></details></form>")

def _filter_args(g) -> dict:
    args = {}
    for name in ("source", "location", "since", "until"):
        if g(name):
            args[name] = g(name)
    if g("undated"):
        args["include_undated"] = True
    return args

def _result_article(r, ts: str) -> str:
    head = f"<b>{_esc(r['source'])}</b>"
    if ts:
        head += f" · {_esc(ts)}"
    if r.get("location"):
        head += f" · {_esc(r['location'])}"
    return (f"<article><header>{head}</header>"
            f"<pre>{_esc(r['text'])}</pre>"
            f'<a href="search?expand={_esc(r["id"])}">expand</a></article>')

def _render_results(g, chrome: str) -> str:
    k = _int(g("k"), 5, 1, 25)
    data = json.loads(server.search_history(query=g("q"), k=k,
                                            **_filter_args(g)))
    if "error" in data:
        return _page(chrome + f"<p>{_esc(data['error'])}</p>")
    parts = [chrome]
    for r in data.get("results", []):
        parts.append(_result_article(r, (r.get("timestamp") or "")[:10]))
    if not data.get("results"):
        parts.append("<p>no matches</p>")
    return _page("".join(parts))

def _render_window(g, chrome: str) -> str:
    offset = _int(g("offset"), 0, 0, 10**9)
    args = _filter_args(g)
    if offset:
        args["offset"] = offset
    data = json.loads(server.list_window(**args))
    if "error" in data:
        return _page(chrome + f"<p>{_esc(data['error'])}</p>")
    parts = [chrome]
    for r in data.get("results", []):
        parts.append(_result_article(r, (r.get("timestamp") or "")[:16]))
    count, total = data.get("count", 0), data.get("total", 0)
    if not count:
        parts.append("<p>nothing in this window</p>")
    else:
        parts.append(f"<p>{offset + 1}–{offset + count} of {total}")
        if offset + count < total:
            nxt = {n: g(n) for n in ("source", "location", "since", "until",
                                     "undated") if g(n)}
            nxt["offset"] = offset + count
            parts.append(f' · <a href="search?{_esc(urlencode(nxt))}">'
                         "older &rarr;</a>")
        parts.append("</p>")
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
        (b"content-security-policy", _CSP),
        (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": data})

async def _search_page(scope, send):
    qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))

    def g(name: str) -> str:
        return (qs.get(name) or [""])[0].strip()

    if g("expand"):
        body = _render_expand(g("expand"))
    else:
        stats = _stats()
        chrome = _banner(stats) + _form(g, stats)
        if g("q"):
            body = _render_results(g, chrome)
        elif g("since") or g("until"):
            body = _render_window(g, chrome)
        else:
            body = _page(chrome)
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
