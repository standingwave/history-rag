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
from datetime import datetime
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
:root{--bg:#fff;--fg:#191919;--muted:#68686e;--line:#e4e4e7;--card:#fafafa;
      --acc:#2563eb;--warn-bg:#fff3cd;--warn-line:#e0c366;--warn-fg:#4a3b00}
@media(prefers-color-scheme:dark){
 :root{--bg:#121214;--fg:#e6e6e9;--muted:#9a9aa2;--line:#2c2c30;
       --card:#1b1b1e;--acc:#8ab4f8;--warn-bg:#3a3000;--warn-line:#7a6a1a;
       --warn-fg:#e8d87a}}
body{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0 auto;
     max-width:40rem;padding:1rem;background:var(--bg);color:var(--fg)}
form{margin-bottom:1rem}
.row{display:flex;gap:.5rem}
.row input{flex:1;min-width:0}
input,select{font:inherit;padding:.5rem;border:1px solid var(--line);
             border-radius:.6rem;background:var(--card);color:inherit}
input[type=checkbox]{width:auto;padding:0}
button{font:inherit;padding:.6rem 1rem;border:0;border-radius:.6rem;
       background:var(--acc);color:var(--bg)}
details{margin-top:.5rem;font-size:.9rem}
summary{color:var(--muted)}
.filters{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;
         margin-top:.5rem}
.filters label{display:flex;flex-direction:column;gap:.15rem;
               color:var(--muted)}
.filters .check{flex-direction:row;align-items:center;gap:.4rem;
                grid-column:1/-1}
svg{width:1em;height:1em;vertical-align:-.12em}
.note{display:flex;gap:.5rem;align-items:center;background:var(--warn-bg);
      border:1px solid var(--warn-line);color:var(--warn-fg);
      border-radius:.6rem;padding:.5rem .75rem}
.note svg{flex:none}
.health,.empty{color:var(--muted);font-size:.8rem;margin:.25rem 0}
.empty{margin-top:1.5rem;text-align:center}
article{border:1px solid var(--line);border-radius:.75rem;
        background:var(--card);padding:.7rem .85rem;margin:.6rem 0}
article header{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;
               color:var(--muted);font-size:.8rem;margin-bottom:.3rem}
.badge{display:inline-flex;align-items:center;gap:.3rem;
       padding:.1rem .55rem;border-radius:1rem;font-size:.78rem}
.badge svg{width:.9em;height:.9em}
.s-claude{color:#c2410c;background:#c2410c1f}
.s-shell{color:#16a34a;background:#16a34a1f}
.s-browser{color:#0284c7;background:#0284c71f}
.s-git{color:#dc2626;background:#dc26261f}
.s-obsidian{color:#7c3aed;background:#7c3aed26}
.s-calendar{color:#db2777;background:#db27771f}
.s-appusage{color:#0d9488;background:#0d94881f}
.s-digest{color:#64748b;background:#64748b1f}
.s-x{color:var(--muted);background:var(--line)}
@media(prefers-color-scheme:dark){
 .s-claude{color:#fb923c}.s-shell{color:#4ade80}.s-browser{color:#38bdf8}
 .s-git{color:#f87171}.s-obsidian{color:#a78bfa}.s-calendar{color:#f472b6}
 .s-appusage{color:#2dd4bf}.s-digest{color:#94a3b8}}
pre{white-space:pre-wrap;word-break:break-word;margin:.25rem 0;font:inherit}
.mono,.mono pre{font-family:ui-monospace,monospace;font-size:.85rem}
.clamp{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;
       overflow:hidden}
a{color:var(--acc)}
a.out{color:inherit;text-decoration:none}
a.out:hover{text-decoration:underline}
h2.day{font-size:.85rem;color:var(--muted);margin:1.2rem 0 .2rem;
       font-weight:600}
.turn.target{border-left:3px solid var(--acc)}
tr.target td:first-child{border-left:3px solid var(--acc);
                         padding-left:.4rem}
table.ctx{border-collapse:collapse;width:100%;font-size:.9rem}
table.ctx td{padding:.25rem .5rem .25rem 0;vertical-align:top}
table.ctx td:first-child{color:var(--muted);white-space:nowrap}
table.ctx small{color:var(--muted)}
.stats dl{display:grid;grid-template-columns:auto 1fr;gap:.15rem .8rem;
          margin:.5rem 0}
.stats dt{color:var(--muted)}
.stats dd{margin:0}
.stats table{border-collapse:collapse;font-size:.85rem}
.stats td,.stats th{text-align:left;padding:.12rem .7rem .12rem 0}
.stats th{color:var(--muted);font-weight:normal}
footer{color:var(--muted);font-size:.75rem;margin-top:2rem}
"""

# Icons: inline SVG only — the CSP (default-src 'none') rules out icon
# fonts and CDNs. currentColor tints them with the theme and badge hue.
_ICONS = {
    "claude": '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.6 8.6 0 0 1-3.7-.9'
              'L3 20l1-5.3A8.4 8.4 0 1 1 21 11.5z"/>',
    "shell": '<polyline points="4 17 10 11 4 5"/>'
             '<line x1="12" y1="19" x2="20" y2="19"/>',
    "browser": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
               '<path d="M12 3a13.5 13.5 0 0 1 0 18 13.5 13.5 0 0 1 0-18z"/>',
    "git": '<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/>'
           '<circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "obsidian": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 '
                '0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    "calendar": '<rect x="3" y="4" width="18" height="17" rx="2"/>'
                '<line x1="16" y1="2" x2="16" y2="6"/>'
                '<line x1="8" y1="2" x2="8" y2="6"/>'
                '<line x1="3" y1="10" x2="21" y2="10"/>',
    "appusage": '<circle cx="12" cy="12" r="9"/>'
                '<polyline points="12 7 12 12 15 14"/>',
    "digest": '<polygon points="12 2 2 7 12 12 22 7 12 2"/>'
              '<polyline points="2 17 12 22 22 17"/>'
              '<polyline points="2 12 12 17 22 12"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "expand": '<polyline points="6 9 12 15 18 9"/>',
    "warn": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 '
            '1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
            '<line x1="12" y1="9" x2="12" y2="13"/>'
            '<line x1="12" y1="17" x2="12.01" y2="17"/>',
}

def _icon(name: str) -> str:
    path = _ICONS.get(name, "")
    if not path:
        return ""
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{path}</svg>')

def _badge(source: str) -> str:
    cls = source if source in _ICONS else "x"
    return (f'<span class="badge s-{cls}">{_icon(source)}'
            f"{_esc(source)}</span>")

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

def _page(inner: str, tail: str = "") -> str:
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>history</title><style>{_STYLE}</style></head><body>"
            f"{inner}<footer>queries travel in the URL — they stay in this "
            f"browser's history</footer>{tail}<script>{_SCRIPT}</script>"
            "</body></html>")

def _local(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None

def _fmt_ts(ts: str) -> str:
    """'Jul 2 · 10:42' local; year added when not current; day-stamped
    chunks (local midnight) show the date alone."""
    d = _local(ts or "")
    if d is None:
        return (ts or "")[:16]
    label = f"{d.strftime('%b')} {d.day}"
    if d.year != datetime.now().year:
        label += f", {d.year}"
    if (d.hour, d.minute) != (0, 0):
        label += d.strftime(" · %H:%M")
    return label

def _day_label(ts: str) -> str:
    d = _local(ts or "")
    if d is None:
        return "undated"
    label = f"{d.strftime('%a %b')} {d.day}"
    if d.year != datetime.now().year:
        label += f", {d.year}"
    return label

def _time_only(ts: str) -> str:
    d = _local(ts or "")
    return "" if d is None or (d.hour, d.minute) == (0, 0) \
        else d.strftime("%H:%M")

def _dur(seconds) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"

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
        parts.append(f'<p class="note">{_icon("warn")}<span>'
                     f'{_esc(h["note"])}</span></p>')
    if h.get("failing_sources"):
        parts.append(f'<p class="note">{_icon("warn")}<span>failing sources: '
                     f'{_esc(", ".join(sorted(h["failing_sources"])))}'
                     "</span></p>")
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

def _result_article(r) -> str:
    head = _badge(r["source"])
    ts = _fmt_ts(r.get("timestamp") or "")
    if ts:
        head += f"<span>{_esc(ts)}</span>"
    if r.get("location"):
        head += f"<span>{_esc(r['location'])}</span>"
    url = (r.get("meta") or {}).get("url") if r["source"] == "browser" else None
    text = _esc(r["text"])
    if url:
        text = f'<a class="out" href="{_esc(url)}">{text}</a>'
    return (f"<article><header>{head}</header>"
            f'<pre class="clamp">{text}</pre>'
            f'<a class="expand" href="search?expand={_esc(r["id"])}">'
            f'{_icon("expand")} expand</a></article>')

def _render_results(g, chrome: str, tail: str = "") -> str:
    k = _int(g("k"), 5, 1, 25)
    data = json.loads(server.search_history(query=g("q"), k=k,
                                            **_filter_args(g)))
    if "error" in data:
        return _page(chrome + f"<p>{_esc(data['error'])}</p>", tail)
    parts = [chrome]
    for r in data.get("results", []):
        parts.append(_result_article(r))
    if not data.get("results"):
        parts.append("<p>no matches</p>")
    return _page("".join(parts), tail)

def _render_window(g, chrome: str, tail: str = "") -> str:
    offset = _int(g("offset"), 0, 0, 10**9)
    args = _filter_args(g)
    if offset:
        args["offset"] = offset
    data = json.loads(server.list_window(**args))
    if "error" in data:
        return _page(chrome + f"<p>{_esc(data['error'])}</p>", tail)
    parts, day = [chrome], None
    for r in data.get("results", []):
        label = _day_label(r.get("timestamp") or "")
        if label != day:
            parts.append(f'<h2 class="day">{_esc(label)}</h2>')
            day = label
        parts.append(_result_article(r))
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
    return _page("".join(parts), tail)

def _empty_state(stats: dict) -> str:
    total, src = stats.get("total_chunks") or 0, stats.get("sources") or {}
    if not total or not src:
        return ""
    years = [i["earliest"][:4] for i in src.values() if i.get("earliest")]
    span = f", {min(years)} → today" if years else ""
    return (f'<p class="empty">{total:,} chunks across {len(src)} '
            f"source{'s' if len(src) != 1 else ''}{span}</p>")

def _stats_panel(stats: dict) -> str:
    src = stats.get("sources") or {}
    if not src:
        return ""
    h = stats.get("health") or {}
    rep = h.get("replica") or {}
    emb = stats.get("embedding") or {}
    db = stats.get("db") or {}
    items = []
    if h.get("status"):
        items.append(("status", h["status"]))
    if h.get("age_minutes") is not None:
        items.append(("refreshed", f"{_age(int(h['age_minutes']))} ago"))
    if rep.get("synced_age_minutes") is not None:
        items.append(("replica synced",
                      f"{_age(int(rep['synced_age_minutes']))} ago"))
    if rep.get("note"):
        items.append(("replica", rep["note"]))
    if emb:
        items.append(("model", f"{emb.get('model')}/{emb.get('dim')}"))
    items.append(("chunks", f"{stats.get('total_chunks', 0):,}"))
    if db.get("bytes"):
        size = f"{db['bytes'] / 1e6:.0f} MB"
        if db.get("freelist_bytes", 0) > db["bytes"] * 0.1:
            size += f" ({db['freelist_bytes'] / 1e6:.0f} MB reclaimable)"
        items.append(("db", size))
    dl = "".join(f"<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>" for k, v in items)
    rows = "".join(
        f"<tr><td>{_esc(s)}</td><td>{i.get('chunks', 0):,}</td>"
        f"<td>{_esc(_day_label(i.get('earliest') or ''))} → "
        f"{_esc(_day_label(i.get('latest') or ''))}</td></tr>"
        for s, i in sorted(src.items()))
    return ('<details class="stats"><summary>index stats</summary>'
            f"<dl>{dl}</dl><table><tr><th>source</th><th>chunks</th>"
            f"<th>range</th></tr>{rows}</table></details>")

# ── context renderers (wip/SPEC-search-page-rendering.md) ──────────────────
# One per context shape, keyed by source like server._EXPANDERS. The JSON
# <pre> fallback keeps a new source's expander working before its renderer
# exists, and absorbs shape drift — a renderer error must not take down
# the reading view.

def _ctx_claude(ctx):
    parts = []
    for t in ctx.get("turns") or []:
        cls = "turn target" if t.get("target") else "turn"
        head = _esc(t.get("role") or "?")
        ts = _fmt_ts(t.get("timestamp") or "")
        if ts:
            head += f" · {_esc(ts)}"
        parts.append(f'<article class="{cls}"><header>{head}</header>'
                     f"<pre>{_esc(t.get('text') or '')}</pre></article>")
    return "".join(parts)

def _timeline(items, day=""):
    rows = []
    for v in items:
        cls = ' class="target"' if v.get("target") else ""
        rows.append(f"<tr{cls}>"
                    f"<td>{_esc(_time_only(v.get('timestamp') or ''))}</td>"
                    f"<td>{_esc(v.get('text') or '')}</td></tr>")
    head = f'<h2 class="day">{_esc(_day_label(day))}</h2>' if day else ""
    return f'{head}<table class="ctx">{"".join(rows)}</table>'

def _ctx_browser(ctx):
    return _timeline(ctx.get("visits") or [], ctx.get("day", ""))

def _ctx_calendar(ctx):
    return _timeline(ctx.get("agenda") or [], ctx.get("day", ""))

def _ctx_appusage(ctx):
    rows = "".join(f"<tr><td>{_esc(_dur(s))}</td><td>{_esc(app)}</td></tr>"
                   for app, s in (ctx.get("seconds_by_app") or {}).items())
    return f'<table class="ctx">{rows}</table>'

def _ctx_git(ctx):
    return f'<pre class="mono">{_esc(ctx.get("show") or "")}</pre>'

def _ctx_obsidian(ctx):
    if ctx.get("note_text") is not None:
        return f"<pre>{_esc(ctx['note_text'])}</pre>"
    return "".join(
        f"<article><header>{_esc(s.get('location') or '')}</header>"
        f"<pre>{_esc(s.get('text') or '')}</pre></article>"
        for s in ctx.get("sections") or [])

def _ctx_shell(ctx):
    rows = []
    for c in ctx.get("commands") or []:
        cls = ' class="target"' if c.get("target") else ""
        where = c.get("cwd") or ""
        if c.get("exit"):
            where += f"  exit {c['exit']}"
        rows.append(f"<tr{cls}>"
                    f"<td>{_esc(_time_only(c.get('timestamp') or ''))}</td>"
                    f'<td class="mono">{_esc(c.get("command") or "")}'
                    f"<br><small>{_esc(where)}</small></td></tr>")
    return f'<table class="ctx">{"".join(rows)}</table>'

_CONTEXT_RENDERERS = {
    "claude": _ctx_claude, "browser": _ctx_browser,
    "calendar": _ctx_calendar, "appusage": _ctx_appusage,
    "git": _ctx_git, "obsidian": _ctx_obsidian, "shell": _ctx_shell,
}

def _context_html(source: str, ctx) -> str:
    if isinstance(ctx, dict) and "note" in ctx and set(ctx) <= {"note",
                                                                "scope"}:
        return f"<p>{_esc(ctx['note'])}</p>"      # git-gone, expand-degrade
    renderer = _CONTEXT_RENDERERS.get(source)
    if renderer and isinstance(ctx, dict):
        try:
            return renderer(ctx)
        except Exception:
            pass
    return ("<pre>"
            + _esc(json.dumps(ctx, indent=2, ensure_ascii=False)) + "</pre>")

def _render_expand(cid: str) -> str:
    data = json.loads(server.expand(cid))
    back = '<p><a href="search">&larr; search</a></p>'
    if "error" in data:
        return _page(back + f"<p>{_esc(data['error'])}</p>")
    c = data["chunk"]
    head = _badge(c["source"])
    ts = _fmt_ts(c.get("timestamp") or "")
    if ts:
        head += f"<span>{_esc(ts)}</span>"
    if c.get("location"):
        head += f"<span>{_esc(c['location'])}</span>"
    parts = [back, f"<article><header>{head}</header>"
                   f"<pre>{_esc(c['text'])}</pre></article>"]
    if data.get("context") is not None:
        label = "context"
        if data.get("context_source"):
            label += f" ({_esc(data['context_source'])})"
        parts.append(f'<h2 class="day">{label}</h2>')
        parts.append(_context_html(c["source"], data["context"]))
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
        tail = _stats_panel(stats)
        if g("q"):
            body = _render_results(g, chrome, tail)
        elif g("since") or g("until"):
            body = _render_window(g, chrome, tail)
        else:
            body = _page(chrome + _empty_state(stats), tail)
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
