"""Browser-history source: Safari + Chromium-family (Chrome, Helium) URLs.

One chunk per (browser, profile, URL) — "<title> — <url>" — so each profile
keeps its own record and searches can tell work from personal browsing. Visits
of the same URL within one profile merge (visit counts summed, latest wins the
timestamp). Ids hash the browser + profile *directory* + URL, so renaming a
profile doesn't orphan chunks; the human-readable profile name (Chromium's
Preferences profile.name, else the dir name) only decorates location and meta.
Safari's default store has no profile: location stays plain "safari", and
Safari 17+ profiles appear under their UUID dirs.

Query strings and fragments are stripped before anything else: they carry
tokens and churn. URLs or titles that still look credential-bearing are
dropped via the shared secret regex, as are localhost and non-http(s) schemes.

Chromium locks its History DB while the browser runs, so every DB is copied
to a temp file and the copy is read. Safari's History.db needs Full Disk
Access for the indexing process (grant it to Terminal and/or the launchd
python); any browser whose DB can't be read is skipped with a stderr note, so
this source no-ops per-browser rather than failing.

CLAUDE_RAG_BROWSERS overrides the default locations: colon-separated
name=path entries, e.g. "arc=~/Library/.../Arc/User Data/Default/History".
The schema (Safari vs Chromium) is sniffed from the tables, not the name.
"""
import os, glob, hashlib, json, shutil, sqlite3, sys, tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from sources.common import SECRET_RE

MAX_CHARS = 500

# Chromium keeps one History DB per profile; Guest/System profiles are
# synthetic and skipped.
_DEFAULTS = [
    ("safari", "~/Library/Safari/History.db"),
    ("safari", "~/Library/Safari/Profiles/*/History.db"),
    ("chrome", "~/Library/Application Support/Google/Chrome/*/History"),
    ("helium", "~/Library/Application Support/net.imput.helium/*/History"),
]
_SKIP_PROFILES = ("Guest Profile", "System Profile")
_SKIP_HOSTS = {"localhost", "127.0.0.1", "[::1]", "0.0.0.0"}

_CFABSOLUTE_TO_UNIX = 978307200       # Safari epoch (2001-01-01) -> Unix
_WEBKIT_TO_UNIX = 11644473600         # Chromium epoch (1601-01-01) -> Unix

def _dbs():
    # Env var REPLACES the defaults (full control); the config file's
    # [browser].extra table ADDS to them.
    env = os.environ.get("CLAUDE_RAG_BROWSERS", "")
    if env:
        out = []
        for entry in env.split(":"):
            name, _, path = entry.partition("=")
            if name and path:
                out.append((name.strip(), os.path.expanduser(path.strip())))
        return out
    out = []
    for name, pat in _DEFAULTS:
        for p in sorted(glob.glob(os.path.expanduser(pat))):
            if not any(skip in p for skip in _SKIP_PROFILES):
                out.append((name, p))
    import config
    extra = config.get("browser", "extra", "", {})
    if isinstance(extra, dict):
        out += [(n, os.path.expanduser(str(p))) for n, p in extra.items()]
    return out

def _profile(path: str) -> str:
    """Profile directory name; '' for Safari's default (profile-less) store."""
    parent = os.path.basename(os.path.dirname(path))
    return "" if parent == "Safari" else parent

def _display_name(path: str, profile: str) -> str:
    """Human-readable profile name (Chromium Preferences), else the dir name."""
    pref = os.path.join(os.path.dirname(path), "Preferences")
    try:
        with open(pref) as f:
            return json.load(f).get("profile", {}).get("name", "") or profile
    except (OSError, ValueError):
        return profile

def _read_chromium(db):
    for url, title, count, ts in db.execute(
            "SELECT url, title, visit_count, last_visit_time FROM urls"):
        yield url, title or "", count or 1, (ts or 0) / 1e6 - _WEBKIT_TO_UNIX

def _read_safari(db):
    rows = db.execute("""
        SELECT i.url, i.visit_count, MAX(v.visit_time),
               MAX(CASE WHEN v.title != '' THEN v.title END)
        FROM history_items i JOIN history_visits v ON v.history_item = i.id
        GROUP BY i.id""")
    for url, count, ts, title in rows:
        yield url, title or "", count or 1, (ts or 0) + _CFABSOLUTE_TO_UNIX

def _reader(db):
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "urls" in tables:
        return _read_chromium
    if "history_items" in tables:
        return _read_safari
    return None

# Query params that ARE the page's identity, kept per domain suffix; every
# other param is stripped (tokens, tracking, churn). Without the "v" here all
# youtube.com/watch pages collapse into a single chunk. Extend or override
# per-domain via [browser].keep_params in the config file (an empty list
# disables a default). The secret regex still runs on the result.
_DEFAULT_KEEP_PARAMS = {"youtube.com": ["v"]}
_keep_table = None

def _keep_for(host: str):
    global _keep_table
    if _keep_table is None:
        import config
        table = {k: list(v) for k, v in _DEFAULT_KEEP_PARAMS.items()}
        cfg = config.get("browser", "keep_params", "", {})
        if isinstance(cfg, dict):
            for dom, params in cfg.items():
                table[dom.lower()] = [str(p) for p in params]
        _keep_table = table
    for dom, params in _keep_table.items():
        if host == dom or host.endswith("." + dom):
            return params
    return None

def _clean_url(url: str):
    """Strip query+fragment (minus per-domain keep_params); reject non-web
    schemes and local hosts."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if host in _SKIP_HOSTS or host.endswith(".local"):
        return None
    query = ""
    keep = _keep_for(host)
    if keep and parts.query:
        kept = sorted((k, v) for k, v in parse_qsl(parts.query) if k in keep)
        query = urlencode(kept)   # sorted + re-encoded -> stable ids
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")).rstrip("/")

# Kept params that mean "this page is a search" (vs identity params like
# youtube's v). Search chunks announce themselves so "what did I search"
# queries retrieve every engine equally — a YouTube results page titled
# "<query> - YouTube" would otherwise embed like a video, not a search.
_SEARCH_PARAMS = ("q", "search_query")

def _search_text(url: str):
    parts = urlsplit(url)
    if not parts.query:
        return None
    for k, v in parse_qsl(parts.query):
        if k in _SEARCH_PARAMS and v:
            host = (parts.hostname or "").removeprefix("www.")
            return f'Searched {host} for "{v}" — {url}'
    return None

def _iso(ts: float) -> str:
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""

def iter_chunks():
    best: dict[tuple, list] = {}   # (browser, profile, url) -> [title, count, ts, display]
    for browser, path in _dbs():
        if not os.path.exists(path):
            continue
        profile = _profile(path)
        display = _display_name(path, profile) if profile else ""
        tmp = None
        try:
            # Copy first: browsers hold their DB locked while running.
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copyfile(path, tmp)
            db = sqlite3.connect(tmp)
            read = _reader(db)
            if read is None:
                db.close()
                continue
            for url, title, count, ts in read(db):
                cleaned = _clean_url(url) if url else None
                if not cleaned or SECRET_RE.search(cleaned) or SECRET_RE.search(title):
                    continue
                key = (browser, profile, cleaned)
                rec = best.get(key)
                if rec is None:
                    best[key] = [title, count, ts, display]
                    continue
                rec[1] += count
                if ts > rec[2]:
                    rec[2] = ts
                    if title:
                        rec[0] = title
                elif not rec[0]:
                    rec[0] = title
            db.close()
        except (OSError, sqlite3.Error) as e:
            print(f"browser: skipping {browser} ({path}): {e}", file=sys.stderr)
        finally:
            if tmp:
                os.unlink(tmp)

    for (browser, profile, url), (title, count, ts, display) in best.items():
        cid = "browser:" + hashlib.sha256(
            f"{browser}\0{profile}\0{url}".encode()).hexdigest()[:26]
        text = (_search_text(url)
                or (f"{title} — {url}" if title else url))[:MAX_CHARS]
        meta = {"url": url, "title": title[:200], "visit_count": int(count)}
        if display:
            meta["profile"] = display
        yield cid, text, {
            "source": "browser",
            "timestamp": _iso(ts),
            "location": f"{browser}:{display}" if display else browser,
            "meta": meta,
        }
