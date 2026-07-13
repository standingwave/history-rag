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
import base64, hashlib, html, json, os, re, sys, time
from datetime import datetime, timedelta
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
.tabs{display:flex;background:var(--card);border:1px solid var(--line);
      border-radius:.8rem;padding:.2rem;gap:.2rem;margin-bottom:.9rem}
.tabs a{flex:1;text-align:center;font-size:.88rem;padding:.42rem 0;
        border-radius:.6rem;color:var(--muted);text-decoration:none}
.tabs a.on{background:var(--acc);color:var(--bg);font-weight:600}
.group{margin:.65rem 0}
.group-label{font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;
             color:var(--muted);margin-bottom:.35rem}
.sband{border-left:3px solid var(--muted);border-radius:0 .5rem .5rem 0;
       background:rgba(128,128,140,.09);padding:.5rem .7rem;
       margin:.35rem 0;font-size:.9rem}
.sband-label{font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;
             color:var(--muted);margin-bottom:.15rem}
.sband p{margin:.1rem 0}
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
.chips{display:flex;gap:.4rem;overflow-x:auto;padding:.3rem 0;
       -webkit-overflow-scrolling:touch}
.chip input{position:absolute;opacity:0;pointer-events:none}
.chip span{display:inline-block;padding:.25rem .8rem;border-radius:1rem;
           border:1px solid var(--line);background:var(--card);
           color:var(--muted);white-space:nowrap;font-size:.85rem}
.chip input:checked+span{background:var(--acc);color:var(--bg);
                         border-color:var(--acc)}
.qp{padding:.25rem .8rem;border-radius:1rem;border:1px solid var(--line);
    background:var(--card);color:var(--muted);white-space:nowrap;
    font-size:.85rem}
a.expand svg{transition:transform .15s}
a.expand[data-open] svg{transform:rotate(180deg)}
a.busy{opacity:.4}
.inline-expand>*{margin-left:.6rem}
.inline-expand article{border-style:dashed}
.chip-s{display:inline-block;padding:.1rem .6rem;border-radius:1rem;
        border:1px solid var(--line);background:var(--card);
        color:var(--muted);font-size:.78rem}
.turn.user{background:#2563eb12}
.add{color:#16a34a}
.del{color:#dc2626}
.err{color:#dc2626}
@media(prefers-color-scheme:dark){
 .turn.user{background:#8ab4f814}
 .add{color:#4ade80}.del{color:#f87171}.err{color:#f87171}}
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

# Scripts are ASCII-only static constants (\\u escapes) so the hashed
# bytes are unambiguous; the CSP lists exactly their hashes, so injected
# chunk text still cannot execute. All HTML the scripts insert is
# server-rendered (fetched fragments) — JS never builds markup from data.

# Submit feedback: disable the form's buttons, label the clicked one by
# the form's mode, restore originals on bfcache back-navigation.
_SCRIPT_SUBMIT = (
    "(function(){var f=document.querySelector('form');if(!f)return;"
    "var bs=f.querySelectorAll('button');"
    "var mi=f.querySelector('input[name=mode]');"
    "var label={ask:'Asking\\u2026',browse:'Loading\\u2026'}"
    "[mi?mi.value:'search']||'Searching\\u2026';"
    "var orig=[];for(var i=0;i<bs.length;i++)orig.push(bs[i].textContent);"
    "f.addEventListener('submit',function(e){"
    "var b=(e&&e.submitter)?e.submitter:bs[0];"
    "for(var i=0;i<bs.length;i++)bs[i].disabled=true;"
    "b.textContent=label});"
    "window.addEventListener('pageshow',function(){"
    "for(var i=0;i<bs.length;i++){bs[i].disabled=false;"
    "bs[i].textContent=orig[i]}})})();")

# Live form (source/view chips auto-submit — the hidden mode input rides
# along, so a chip tap can never switch modes) + inline expand (fragment
# fetch; failures fall through to plain navigation).
_SCRIPT_LIVE = (
    "(function(){var f=document.querySelector('form');"
    "if(f){"
    "var subm=function(){f.requestSubmit?f.requestSubmit():f.submit()};"
    "var act=function(){return(f.q&&f.q.value.trim())"
    "||(f.since&&f.since.value)||(f.until&&f.until.value)};"
    "var rs=f.querySelectorAll('input[name=source],input[name=view]');"
    "for(var i=0;i<rs.length;i++)rs[i].addEventListener('change',"
    "function(){if(act())subm()});"
    "}"
    "var open=null;"
    "function host(a){return a.closest('article')||a.closest('table')}"
    "function frag(a){var h=host(a);var n=h?h.nextElementSibling:null;"
    "return n&&n.classList.contains('inline-expand')?n:null}"
    "function collapse(a){var n=frag(a);if(n)n.hidden=true;"
    "a.removeAttribute('data-open');if(open===a)open=null}"
    "function show(a,n){n.hidden=false;a.setAttribute('data-open','1');"
    "open=a;var t=n.querySelector('.target');"
    "if(t)t.scrollIntoView({block:'nearest'})}"
    "document.addEventListener('click',function(e){"
    "var c=e.target&&e.target.closest;"
    "var m=c?e.target.closest('a.morectx'):null;"
    "if(m){var box=m.closest('.inline-expand');"
    "if(!box)return;"        # full-page morectx: navigate normally
    "e.preventDefault();m.classList.add('busy');"
    "fetch(m.getAttribute('href')+'&fragment=1')"
    ".then(function(r){if(!r.ok)throw 0;return r.text()})"
    ".then(function(h){box.innerHTML=h})"
    ".catch(function(){location.href=m.getAttribute('href')});"
    "return}"
    "var a=c?e.target.closest('a.expand'):null;"
    "if(!a)return;"
    "e.preventDefault();"
    "if(a.hasAttribute('data-open')){collapse(a);return}"
    "if(open)collapse(open);"
    "var kept=frag(a);"
    "if(kept){show(a,kept);return}"      # collapsed once: no refetch
    "a.classList.add('busy');"
    "fetch(a.getAttribute('href')+'&fragment=1')"
    ".then(function(r){if(!r.ok)throw 0;return r.text()})"
    ".then(function(h){a.classList.remove('busy');"
    "var d=document.createElement('div');"
    "d.className='inline-expand';d.innerHTML=h;"
    "host(a).after(d);show(a,d)})"
    ".catch(function(){location.href=a.getAttribute('href')})});"
    "var dead=document.querySelectorAll('a.expand[data-noctx]');"
    "for(var q=0;q<dead.length;q++)(function(l){"
    "var h=l.closest('article');var p=h?h.querySelector('.clamp'):null;"
    "if(p&&p.scrollHeight<=p.clientHeight+1)l.remove()})(dead[q]);"
    "})();")

_SCRIPTS = [_SCRIPT_SUBMIT, _SCRIPT_LIVE]

def _sha(s: str) -> str:
    return base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()

_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
        "connect-src 'self'; script-src "
        + " ".join(f"'sha256-{_sha(s)}'" for s in _SCRIPTS)).encode()

def _esc(v) -> str:
    return html.escape(str(v))

def _page(inner: str, tail: str = "") -> str:
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>history</title><style>{_STYLE}</style></head><body>"
            f"{inner}<footer>queries travel in the URL — they stay in this "
            f"browser's history</footer>{tail}"
            + "".join(f"<script>{s}</script>" for s in _SCRIPTS)
            + "</body></html>")

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

# ── the three-mode form (wip/SPEC-search-form-rebuild.md) ──────────────────
# One intent per mode, one verb-labeled submit, only the controls that
# apply. Tabs are links (no-JS safe, bookmarkable); auto-submit carries
# the mode via a per-form hidden input.

_MODES = ("search", "ask", "browse")

def _mode_of(g) -> str:
    m = g("mode")
    if m in _MODES:
        return m
    if not g("q") and (g("since") or g("until")):
        return "browse"           # legacy bookmark shape
    return "search"               # incl. unknown modes: never an error

def _tabs(mode: str, g, has_ask: bool) -> str:
    def href(m, keys):
        # tab=1: tabs switch forms and carry prefill state — they never
        # execute. Bookmarks, back links, and paging links (no tab=1)
        # keep executing as always.
        params = ({} if m == "search" else {"mode": m})
        params.update({k: g(k) for k in keys if g(k)})
        params["tab"] = "1"
        return f"search?{urlencode(params)}"

    entries = [("search", "Search", ("q", "since", "until", "source", "k",
                                     "location"))]
    if has_ask:
        entries.append(("ask", "Ask", ("q", "model")))
    entries.append(("browse", "Browse", ("since", "until", "source",
                                         "view")))
    out = ['<nav class="tabs">']
    for m, label, keys in entries:
        cls = ' class="on"' if m == mode else ""
        out.append(f'<a{cls} href="{_esc(href(m, keys))}">{label}</a>')
    return "".join(out) + "</nav>"

def _source_chips(g, stats: dict) -> str:
    source = g("source")
    # digest is a summary tier, not a content type — no chip (the
    # source=digest URL shape stays honored)
    names = sorted(n for n in stats.get("sources", {}) if n != "digest")
    if source and source not in names:
        names.append(source)   # keep an active out-of-catalog filter visible
    chips = ['<label class="chip"><input type="radio" name="source" '
             f'value=""{"" if source else " checked"}><span>All</span>'
             "</label>"]
    for s in names:
        chk = " checked" if s == source else ""
        chips.append('<label class="chip"><input type="radio" name="source" '
                     f'value="{_esc(s)}"{chk}><span>{_esc(s)}</span></label>')
    return ('<div class="group"><div class="group-label">source</div>'
            f'<div class="chips">{"".join(chips)}</div></div>')

def _form_search(g, stats: dict) -> str:
    since, until, location = g("since"), g("until"), g("location")
    undated, k = bool(g("undated")), _int(g("k"), 5, 1, 25)
    active = any([since, until, location, undated, k != 5])
    undated_ctl = ""
    if since or until:            # only meaningful once a bound is set
        undated_ctl = (f'<label class="check"><input type="checkbox" '
                       f'name="undated" value="1"'
                       f'{" checked" if undated else ""}> include undated'
                       "</label>")
    # autofocus only on the bare landing page: iOS Safari applies deferred
    # focus on first touch, yanking scroll back up to the input otherwise
    af = "" if (g("q") or since or until) else " autofocus"
    return (
        '<form method="get">'
        '<div class="row">'
        f'<input type="search" name="q" value="{_esc(g("q"))}" '
        f'placeholder="search history"{af}>'
        "<button>Search</button></div>"
        f"{_source_chips(g, stats)}"
        f'<details{" open" if active else ""}><summary>more filters</summary>'
        '<div class="filters">'
        f'<label>since <input type="date" name="since" value="{_esc(since)}">'
        "</label>"
        f'<label>until <input type="date" name="until" value="{_esc(until)}">'
        "</label>"
        f'<label>results <input type="number" name="k" min="1" max="25" '
        f'value="{k}"></label>'
        f'<label>location <input name="location" value="{_esc(location)}" '
        'placeholder="prefix, e.g. chrome:"></label>'
        f"{undated_ctl}</div></details></form>")

def _form_ask(g) -> str:
    ps = ask.presets()
    picker = ""
    if len(ps) >= 2:                    # a non-choice gets no UI
        sel = g("model") or ps[0]["name"]
        mchips = []
        for p in ps:
            chk = " checked" if p["name"] == sel else ""
            mchips.append('<label class="chip"><input type="radio" '
                          f'name="model" value="{_esc(p["name"])}"{chk}>'
                          f'<span>{_esc(p["name"])}</span></label>')
        picker = ('<div class="group"><div class="group-label">model</div>'
                  f'<div class="chips">{"".join(mchips)}</div></div>')
    af = "" if g("q") else " autofocus"
    return (
        '<form method="get">'
        '<input type="hidden" name="mode" value="ask">'
        # go=1 is the explicit submit marker: the Ask tab link carries q
        # for prefill, and must never execute a paid ask by navigation
        '<input type="hidden" name="go" value="1">'
        '<div class="row">'
        f'<input type="search" name="q" value="{_esc(g("q"))}" '
        f'placeholder="ask your history"{af}>'
        "<button>Ask</button></div>"
        f"{picker}"
        '<p class="health">the model works your history tools and cites '
        "what it reads — takes 10–60s</p></form>")

def _form_browse(g, stats: dict) -> str:
    today = datetime.now().astimezone().date().isoformat()
    since = g("since") or today
    until = g("until") or today
    view = g("view")
    toggle = "".join(
        '<label class="chip"><input type="radio" name="view" '
        f'value="{v}"{" checked" if view == v else ""}>'
        f"<span>{t}</span></label>"
        for v, t in (("summaries", "Summaries"), ("", "Everything")))
    qps = "".join(f'<button class="qp" name="range" value="{v}">{t}</button>'
                  for v, t in (("today", "Today"), ("7d", "7d"),
                               ("30d", "30d")))
    return (
        '<form method="get">'
        '<input type="hidden" name="mode" value="browse">'
        f'<div class="group"><div class="group-label">view</div>'
        f'<div class="chips">{toggle}</div></div>'
        f'<div class="chips">{qps}</div>'
        '<div class="filters">'
        f'<label>since <input type="date" name="since" value="{_esc(since)}">'
        "</label>"
        f'<label>until <input type="date" name="until" value="{_esc(until)}">'
        "</label></div>"
        f"{_source_chips(g, stats)}"
        '<div class="row"><button>Browse</button></div></form>')

def _form(g, stats: dict) -> str:
    mode = _mode_of(g)
    tabs = _tabs(mode, g, has_ask=bool(ask.presets()))
    if mode == "ask":
        return tabs + _form_ask(g)
    if mode == "browse":
        return tabs + _form_browse(g, stats)
    return tabs + _form_search(g, stats)

def _filter_args(g) -> dict:
    args = {}
    for name in ("source", "location", "since", "until"):
        if g(name):
            args[name] = g(name)
    if g("undated"):
        args["include_undated"] = True
    return args

def _back_qs(g) -> str:
    """The originating search, as a query string — carried on expand links
    so the expand page's back link restores the search, not the homepage."""
    keep = {n: g(n) for n in ("q", "source", "location", "since", "until",
                              "undated", "k", "offset", "model", "view")
            if g(n)}
    if _mode_of(g) == "browse":   # ask deliberately excluded: back from a
        keep["mode"] = "browse"   # citation must never trigger a paid re-ask
    return urlencode(keep)

# ── card renderers: chunk text is the embedded prose; render the meta's
# structure where it's richer. Fallback (None or an error) is the plain
# clamped <pre>, so unformatted sources lose nothing.

def _card_shell(r, clamp=True):
    cls = "clamp mono" if clamp else "mono"
    return f'<pre class="{cls}">{_esc(r["text"])}</pre>'

def _card_git(r, clamp=True):
    subject, _, body = r["text"].partition("\n")
    out = f"<b>{_esc(subject)}</b>"
    if body.strip():
        out += f"\n{_esc(body.strip())}"
    cls = ' class="clamp"' if clamp else ""
    return f"<pre{cls}>{out}</pre>"

def _card_calendar(r, clamp=True):
    cls = ' class="clamp"' if clamp else ""
    out = f"<pre{cls}>{_esc(r['text'])}</pre>"
    att = (r.get("meta") or {}).get("attendees") or []
    if att:
        out += f'<p class="health">with {_esc(", ".join(att[:8]))}</p>'
    return out

def _card_appusage(r, clamp=True):
    m = r.get("meta") or {}
    if "first" not in m:
        return None                     # per-app chunk: plain text card
    stat = (f"active {m.get('first', '')}–{m.get('last', '')} · "
            f"{_dur(m.get('active_seconds', 0))} · "
            f"{m.get('switches', 0)} switches")
    if m.get("breaks"):
        away = sum(b.get("minutes", 0) for b in m["breaks"])
        stat += f" · {len(m['breaks'])} breaks ({_dur(away * 60)})"
    parts = [f'<p class="health">{_esc(stat)}</p>']
    if m.get("focus"):
        parts.append('<p class="health">focus: ' + " · ".join(
            f"{_dur(f.get('minutes', 0) * 60)} {_esc(f.get('app') or '')}"
            for f in m["focus"]) + "</p>")
    if m.get("calls"):
        parts.append('<p class="health">calls: ' + " · ".join(
            f"{_dur(c.get('minutes', 0) * 60)}"
            + (f" ({_esc(c['app'])})" if c.get("app") else "")
            for c in m["calls"]) + "</p>")
    if m.get("categories"):
        parts.append('<div class="chips">' + "".join(
            f'<span class="chip-s">{_esc(cat)} {_dur(secs)}</span>'
            for cat, secs in m["categories"].items()) + "</div>")
    return "".join(parts)

_CARD_RENDERERS = {"shell": _card_shell, "git": _card_git,
                   "calendar": _card_calendar, "appusage": _card_appusage}

# Sources whose expanders read live local stores absent on the Lambda —
# their expand adds nothing remotely, so short cards drop the link (the
# script measures actual clamping; JS-off keeps the link, harmlessly).
_NOCTX = {"shell", "appusage"}

def _result_article(r, bq: str = "") -> str:
    head = _badge(r["source"])
    ts = _fmt_ts(r.get("timestamp") or "")
    if ts:
        head += f"<span>{_esc(ts)}</span>"
    if r.get("location"):
        head += f"<span>{_esc(r['location'])}</span>"
    role = (r.get("meta") or {}).get("role") if r["source"] == "claude" else None
    if role:
        head += f"<span>{_esc(role)}</span>"
    body = None
    renderer = _CARD_RENDERERS.get(r["source"])
    if renderer:
        try:
            body = renderer(r)
        except Exception:
            body = None
    if body is None:
        url = ((r.get("meta") or {}).get("url")
               if r["source"] == "browser" else None)
        text = _esc(r["text"])
        if url:
            text = f'<a class="out" href="{_esc(url)}">{text}</a>'
        body = f'<pre class="clamp">{text}</pre>'
    href = f"search?expand={r['id']}" + (f"&{bq}" if bq else "")
    noctx = " data-noctx" if r["source"] in _NOCTX else ""
    return (f"<article><header>{head}</header>{body}"
            f'<a class="expand"{noctx} href="{_esc(href)}">'
            f'{_icon("expand")} expand</a></article>')

def _render_results(g, chrome: str, tail: str = "") -> str:
    k = _int(g("k"), 5, 1, 25)
    data = json.loads(server.search_history(query=g("q"), k=k,
                                            **_filter_args(g)))
    if "error" in data:
        return _page(chrome + f"<p>{_esc(data['error'])}</p>", tail)
    parts, bq = [chrome], _back_qs(g)
    for r in data.get("results", []):
        parts.append(_result_article(r, bq))
    if not data.get("results"):
        parts.append("<p>no matches</p>")
    return _page("".join(parts), tail)

def _is_summary(r) -> bool:
    return (r["source"] == "digest"
            or (r["source"] == "appusage"
                and "first" in (r.get("meta") or {})))

def _summary_band(r, bq: str = "") -> str:
    """Summary-tier chunks render as bands bound to their day header —
    day → summary → detail, encoded in form."""
    meta = r.get("meta") or {}
    if r["source"] == "digest":
        label = f"digest · {meta.get('digest_of', '')}"
        loc = r.get("location") or ""
        if loc and loc != meta.get("digest_of"):
            label += f" · {loc}"
        body = f"<p>{_esc(r['text'])}</p>"
    else:
        label = "day shape"
        body = _card_appusage(r, clamp=False) or f"<p>{_esc(r['text'])}</p>"
    href = f"search?expand={r['id']}" + (f"&{bq}" if bq else "")
    return (f'<div class="sband"><div class="sband-label">{_esc(label)}'
            f"</div>{body}"
            f'<a class="expand" href="{_esc(href)}">'
            f'{_icon("expand")} expand</a></div>')

def _render_window(g, chrome: str, tail: str = "") -> str:
    offset = _int(g("offset"), 0, 0, 10**9)
    args = _filter_args(g)
    args["include_meta"] = True    # the card renderers feed on meta
    if g("view") == "summaries":
        args["summaries"] = True
    if offset:
        args["offset"] = offset
    data = json.loads(server.list_window(**args))
    if "error" in data:
        return _page(chrome + f"<p>{_esc(data['error'])}</p>", tail)
    parts, day, bq = [chrome], None, _back_qs(g)
    for r in data.get("results", []):
        label = _day_label(r.get("timestamp") or "")
        if label != day:
            parts.append(f'<h2 class="day">{_esc(label)}</h2>')
            day = label
        parts.append(_summary_band(r, bq) if _is_summary(r)
                     else _result_article(r, bq))
    count, total = data.get("count", 0), data.get("total", 0)
    if not count:
        parts.append("<p>nothing in this window</p>")
    else:
        parts.append(f"<p>{offset + 1}–{offset + count} of {total}")
        if offset + count < total:
            nxt = {"mode": "browse"}
            nxt.update({n: g(n) for n in ("source", "location", "since",
                                          "until", "undated", "view")
                        if g(n)})
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

def _ctx_claude(ctx, bq=""):
    parts = []
    for t in ctx.get("turns") or []:
        cls = "turn"
        if (t.get("role") or "") == "user":
            cls += " user"
        if t.get("target"):
            cls += " target"
        head = _esc(t.get("role") or "?")
        ts = _fmt_ts(t.get("timestamp") or "")
        if ts:
            head += f" · {_esc(ts)}"
        parts.append(f'<article class="{cls}"><header>{head}</header>'
                     f"<pre>{_esc(t.get('text') or '')}</pre></article>")
    return "".join(parts)

def _timeline(items, day="", bq=""):
    rows = []
    for v in items:
        cls = ' class="target"' if v.get("target") else ""
        text = _esc(v.get("text") or "")
        if v.get("id"):        # rows are chunks — let them expand too
            href = f"search?expand={v['id']}" + (f"&{bq}" if bq else "")
            text = f'<a class="expand" href="{_esc(href)}">{text}</a>'
        rows.append(f"<tr{cls}>"
                    f"<td>{_esc(_time_only(v.get('timestamp') or ''))}</td>"
                    f"<td>{text}</td></tr>")
    head = f'<h2 class="day">{_esc(_day_label(day))}</h2>' if day else ""
    return f'{head}<table class="ctx">{"".join(rows)}</table>'

def _ctx_browser(ctx, bq=""):
    return _timeline(ctx.get("visits") or [], ctx.get("day", ""), bq)

def _ctx_calendar(ctx, bq=""):
    return _timeline(ctx.get("agenda") or [], ctx.get("day", ""), bq)

def _ctx_appusage(ctx, bq=""):
    rows = "".join(f"<tr><td>{_esc(_dur(s))}</td><td>{_esc(app)}</td></tr>"
                   for app, s in (ctx.get("seconds_by_app") or {}).items())
    return f'<table class="ctx">{rows}</table>'

_DIFFSTAT = re.compile(r"^(.*\|\s*\d+ )(\+*)(-*)$")

def _ctx_git(ctx, bq=""):
    out = []
    for line in (ctx.get("show") or "").splitlines():
        m = _DIFFSTAT.match(line)
        if m:                       # tint the +/- bars; else leave untouched
            line = (_esc(m.group(1))
                    + (f'<span class="add">{m.group(2)}</span>'
                       if m.group(2) else "")
                    + (f'<span class="del">{m.group(3)}</span>'
                       if m.group(3) else ""))
        else:
            line = _esc(line)
        out.append(line)
    return '<pre class="mono">' + "\n".join(out) + "</pre>"

def _ctx_obsidian(ctx, bq=""):
    if ctx.get("note_text") is not None:
        return f"<pre>{_esc(ctx['note_text'])}</pre>"
    return "".join(
        f"<article><header>{_esc(s.get('location') or '')}</header>"
        f"<pre>{_esc(s.get('text') or '')}</pre></article>"
        for s in ctx.get("sections") or [])

def _ctx_shell(ctx, bq=""):
    rows = []
    for c in ctx.get("commands") or []:
        cls = ' class="target"' if c.get("target") else ""
        extra = (f' <span class="err">exit {_esc(str(c["exit"]))}</span>'
                 if c.get("exit") else "")
        rows.append(f"<tr{cls}>"
                    f"<td>{_esc(_time_only(c.get('timestamp') or ''))}</td>"
                    f'<td class="mono">{_esc(c.get("command") or "")}'
                    f"<br><small>{_esc(c.get('cwd') or '')}{extra}</small>"
                    "</td></tr>")
    return f'<table class="ctx">{"".join(rows)}</table>'

def _ctx_digest(ctx, bq=""):
    r = ctx.get("rollup") or {}
    parts, stats = [], []
    if r.get("visits"):
        stats.append(f"{r['visits']} visits")
    if r.get("total_turns"):
        stats.append(f"{r['total_turns']} turns")
    if r.get("runs"):
        stats.append(f"{r['runs']} commands")
    if stats:
        parts.append(f'<p class="health">{_esc(" · ".join(stats))}</p>')
    def table(title, rows):
        parts.append(f'<h2 class="day">{title}</h2>'
                     f'<table class="ctx">{rows}</table>')
    if r.get("domains"):
        table("sites", "".join(
            f"<tr><td>{_esc(n)}</td><td>{_esc(d)}</td></tr>"
            for d, n in r["domains"].items()))
    if r.get("searches"):
        table("searches", "".join(
            f"<tr><td></td><td>{_esc(s)}</td></tr>" for s in r["searches"]))
    if r.get("top_titles"):
        table("pages", "".join(
            f"<tr><td>{_esc(t.get('visits') or '')}</td>"
            f"<td>{_esc(t.get('title') or '')}</td></tr>"
            for t in r["top_titles"]))
    if r.get("sessions"):
        table("sessions", "".join(
            f"<tr><td>{_esc(s.get('turns') or '')}</td>"
            f"<td>{_esc(s.get('project') or '')}<br>"
            f"<small>{_esc(s.get('first_prompt') or '')}</small></td></tr>"
            for s in r["sessions"]))
    if r.get("by_cwd"):
        table("runs by directory", "".join(
            f"<tr><td>{_esc(n)}</td><td>{_esc(c)}</td></tr>"
            for c, n in r["by_cwd"].items()))
    if r.get("top_commands"):
        table("top commands", "".join(
            f"<tr><td>x{_esc(c.get('runs') or '')}</td>"
            f'<td class="mono">{_esc(c.get("command") or "")}</td></tr>'
            for c in r["top_commands"]))
    if not parts:                   # unknown rollup stream: honest fallback
        return ("<pre>"
                + _esc(json.dumps(r, indent=2, ensure_ascii=False)) + "</pre>")
    return "".join(parts)

_CONTEXT_RENDERERS = {
    "claude": _ctx_claude, "browser": _ctx_browser,
    "calendar": _ctx_calendar, "appusage": _ctx_appusage,
    "git": _ctx_git, "obsidian": _ctx_obsidian, "shell": _ctx_shell,
    "digest": _ctx_digest,
}

def _context_html(source: str, ctx, bq="") -> str:
    if isinstance(ctx, dict) and "note" in ctx and set(ctx) <= {"note",
                                                                "scope"}:
        return f"<p>{_esc(ctx['note'])}</p>"      # git-gone, expand-degrade
    renderer = _CONTEXT_RENDERERS.get(source)
    if renderer and isinstance(ctx, dict):
        try:
            return renderer(ctx, bq)
        except Exception:
            pass
    return ("<pre>"
            + _esc(json.dumps(ctx, indent=2, ensure_ascii=False)) + "</pre>")

def _meta_line(source: str, meta: dict) -> str:
    """The chunk meta fields worth reading, one muted line per source;
    empty fields render nothing."""
    bits = []
    if source == "browser":
        if meta.get("visit_count"):
            bits.append(_esc(f"{meta['visit_count']} visits"))
        if meta.get("url"):
            bits.append(f'<a class="out" href="{_esc(meta["url"])}">'
                        f'{_esc(str(meta["url"])[:80])}</a>')
    elif source == "shell":
        if meta.get("count"):
            bits.append(_esc(f"{meta['count']} runs"))
        if meta.get("cwd"):
            bits.append(_esc(meta["cwd"]))
        if meta.get("exit"):
            bits.append(f'<span class="err">exit {_esc(str(meta["exit"]))}'
                        "</span>")
    elif source == "git":
        if meta.get("sha"):
            bits.append(_esc(str(meta["sha"])[:12]))
        if (meta.get("count") or 0) > 1:
            bits.append(_esc(f"{meta['count']} copies"))
    elif source == "calendar":
        if meta.get("attendees"):
            bits.append(_esc("with " + ", ".join(meta["attendees"][:8])))
    elif source == "appusage":
        if meta.get("category"):
            bits.append(_esc(meta["category"]))
    elif source == "claude":
        if meta.get("role"):
            bits.append(_esc(meta["role"]))
    return f'<p class="health">{" · ".join(bits)}</p>' if bits else ""

def _expand_articles(cid: str, n: int = 5, bq: str = "") -> str:
    """The expand view's content, chrome-free — served whole to the full
    page and as the fragment the inline-expand script fetches."""
    data = json.loads(server.expand(cid, context=n))
    if "error" in data:
        return f"<p>{_esc(data['error'])}</p>"
    c = data["chunk"]
    head = _badge(c["source"])
    ts = _fmt_ts(c.get("timestamp") or "")
    if ts:
        head += f"<span>{_esc(ts)}</span>"
    if c.get("location"):
        head += f"<span>{_esc(c['location'])}</span>"
    body = None
    renderer = _CARD_RENDERERS.get(c["source"])
    if renderer:
        try:
            body = renderer(c, clamp=False)
        except Exception:
            body = None
    if body is None:
        body = f"<pre>{_esc(c['text'])}</pre>"
    parts = [f"<article><header>{head}</header>"
             f"{_meta_line(c['source'], c.get('meta') or {})}{body}"
             "</article>"]
    if data.get("context") is not None:
        label = "context"
        if data.get("context_source"):
            label += f" ({_esc(data['context_source'])})"
        parts.append(f'<h2 class="day">{label}</h2>')
        parts.append(_context_html(c["source"], data["context"], bq))
        if n < 25:
            href = (f"search?expand={cid}&context={min(max(n * 2, 10), 25)}"
                    + (f"&{bq}" if bq else ""))
            parts.append(f'<p><a class="morectx" href="{_esc(href)}">'
                         "show more context</a></p>")
    return "".join(parts)

def _render_expand(cid: str, n: int = 5, bq: str = "") -> str:
    back_href = "search" + (f"?{bq}" if bq else "")
    back = f'<p><a href="{_esc(back_href)}">&larr; search</a></p>'
    return _page(back + _expand_articles(cid, n, bq))

def _render_answer(result: dict, g, chrome: str, tail: str) -> str:
    """The ask answer card: escaped text with [id:…] citations turned
    into numbered expand links, usage line beneath. Back params skip
    `mode` deliberately — returning from a citation lands on the cheap
    search view, never a paid re-ask."""
    if "error" in result:
        return _page(chrome + f'<p class="note">{_icon("warn")}<span>'
                     f'{_esc(result["error"])}</span></p>', tail)
    bq = _back_qs(g)
    n = 0

    def link(m):
        nonlocal n
        n += 1
        href = f"search?expand={m.group(1)}" + (f"&{bq}" if bq else "")
        return f'<a class="expand" href="{_esc(href)}">[{n}]</a>'

    text = ask.CITE_RE.sub(link, _esc(result.get("answer") or ""))
    u = result.get("usage") or {}
    usage = (f"{u.get('model', '')} · {u.get('turns', 0)} turns · "
             f"{u.get('in', 0)}+{u.get('out', 0)} tokens")
    note = (f'<p class="health">{_esc(result["note"])}</p>'
            if result.get("note") else "")
    return _page(chrome + f'<article class="answer"><pre>{text}</pre>'
                 f'</article>{note}<p class="health">{_esc(usage)}</p>', tail)

async def _send_json(send, obj: dict):
    data = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(data)).encode()),
        (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": data})

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

    if g("range") in ("today", "7d", "30d"):
        # browse presets are plain submits; the server computes the dates
        days = {"today": 0, "7d": 6, "30d": 29}[g("range")]
        until = datetime.now().astimezone().date()
        qs["until"] = [until.isoformat()]
        qs["since"] = [(until - timedelta(days=days)).isoformat()]

    if g("expand"):
        n = _int(g("context"), 5, 0, 25)
        if g("fragment"):
            await _send_html(send, _expand_articles(g("expand"), n,
                                                    _back_qs(g)))
            return
        body = _render_expand(g("expand"), n, _back_qs(g))
    else:
        stats = _stats()
        chrome = _banner(stats) + _form(g, stats)
        tail = _stats_panel(stats)
        mode = _mode_of(g)
        if g("tab"):                       # tab navigation: form only
            body = _page(chrome + (_empty_state(stats)
                                   if mode == "search" and not g("q")
                                   else ""), tail)
        elif mode == "ask" and g("q") and g("go"):
            result = ask.ask(g("q"), g("model"))
            if g("json"):
                await _send_json(send, result)
                return
            body = _render_answer(result, g, chrome, tail)
        elif mode == "browse" and (g("since") or g("until")):
            body = _render_window(g, chrome, tail)
        elif mode == "search" and g("q"):
            body = _render_results(g, chrome, tail)
        elif mode == "search":
            body = _page(chrome + _empty_state(stats), tail)
        else:
            body = _page(chrome, tail)     # ask/browse form, nothing run yet
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
