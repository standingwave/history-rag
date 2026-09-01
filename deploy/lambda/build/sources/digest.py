"""Daily digest source: one summary chunk per (local day, stream).

Precomputed rollups make "what did I do today/this week?" a ~30-chunk read
instead of a paged crawl of every raw chunk in the window. Streams:
  browser  one chunk per (day, browser profile) — visit counts by site, the
           day's searches, notable titles. Read from the browsers' visit
           tables via browser.iter_visits, NOT the index: indexed browser
           chunks carry only each URL's last visit, which would credit a page
           visited Monday and Friday to Friday alone.
  claude   one chunk per day — sessions with their opening prompt as the
           topic. Read from the index (claude turns are per-turn dated).
  shell    one chunk per day — dated runs by cwd, via shell.iter_dated_runs.

Text is templated, never model-written: same inputs -> same text -> the
idempotent indexer skips the re-embed. The text stays compact (embedding
quality drops past the model's context); the full rollup rides in meta,
which expand() returns whole.

Only the last `recompute_days` local days are re-yielded once caught up
(today is still growing; yesterday can gain late data — atuin sync, a
session straddling midnight). Older digests are never yielded again, so they
settle into archive and survive their backing data aging out (Chromium keeps
~90 days of visits). A fresh index backfills `backfill_days`. Catch-up after
a gap (laptop off for a week) resumes from the last digested day.
"""
import hashlib, json, os, sqlite3, sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

MAX_TEXT = 1200
TOP_DOMAINS, TOP_SEARCHES, TOP_TITLES = 5, 5, 3    # named in the text
META_DOMAINS, META_TITLES, META_COMMANDS = 40, 10, 10   # kept in meta
TOPIC_CHARS = 100

_STREAMS = ("browser", "claude", "shell")

def _cfg():
    import config
    srcs = config.get("digest", "sources", "CLAUDE_RAG_DIGEST_SOURCES",
                      list(_STREAMS))
    if isinstance(srcs, str):
        srcs = [s.strip() for s in srcs.replace(":", ",").split(",")]
    srcs = [s for s in srcs if s]
    unknown = [s for s in srcs if s not in _STREAMS]
    if unknown:
        print(f"digest: ignoring unknown sources {unknown}", file=sys.stderr)
    rec = int(config.get("digest", "recompute_days",
                         "CLAUDE_RAG_DIGEST_RECOMPUTE_DAYS", 3))
    back = int(config.get("digest", "backfill_days",
                          "CLAUDE_RAG_DIGEST_BACKFILL_DAYS", 90))
    return ([s for s in srcs if s in _STREAMS], max(1, rec), max(1, back))

def _index_ro():
    """Read-only handle on the index, or None. The digest source is the one
    source that reads the index (to resume after the last digested day, and
    for claude turns); read-only so a source can never corrupt it."""
    import config
    if not os.path.exists(config.DB_PATH):
        return None
    try:
        return sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return None

def _days_to_digest(recompute: int, backfill: int) -> list[str]:
    """ISO local days to (re)compute: the last `recompute` days, extended
    back to the day after the newest stored digest (gap catch-up), bounded
    by `backfill` for fresh indexes. Oldest first."""
    today = date.today()
    floor = today - timedelta(days=backfill - 1)
    start = floor
    db = _index_ro()
    if db is not None:
        try:
            row = db.execute(
                "SELECT MAX(json_extract(meta, '$.date')) FROM chunks "
                "WHERE source = 'digest'").fetchone()
            if row and row[0]:
                start = min(today - timedelta(days=recompute - 1),
                            date.fromisoformat(row[0]) + timedelta(days=1))
        except (sqlite3.Error, ValueError):
            pass
        finally:
            db.close()
    start = max(start, floor)
    return [(start + timedelta(days=i)).isoformat()
            for i in range((today - start).days + 1)]

# ── day/time helpers (appusage's local-day-stamped-in-UTC convention) ────────

def _day_utc(day: str) -> str:
    return datetime.fromisoformat(day).astimezone(timezone.utc).isoformat()

def _local_day_of_epoch(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return ""

def _local_day_of_ts(ts: str) -> str:
    try:
        return (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                .astimezone().date().isoformat())
    except ValueError:
        return ""

def _weekday(day: str) -> str:
    return date.fromisoformat(day).strftime("%A")

def _abbrev(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path

def _chunk(kind: str, stream: str, day: str, text: str, meta: dict):
    cid = "digest:" + hashlib.sha256(
        f"{kind}\0{stream}\0{day}".encode()).hexdigest()[:24]
    return cid, text[:MAX_TEXT], {
        "source": "digest",
        "timestamp": _day_utc(day),
        "location": stream,
        "meta": {"date": day, "digest_of": kind, **meta},
    }

# ── per-stream builders ──────────────────────────────────────────────────────

def _browser_chunks(days):
    from sources import browser
    start = datetime.fromisoformat(days[0]).timestamp()
    dayset = set(days)
    acc: dict[tuple, dict] = {}
    for location, epoch, url, title in browser.iter_visits(start):
        day = _local_day_of_epoch(epoch)
        if day not in dayset:
            continue
        a = acc.setdefault((day, location), {
            "visits": 0, "domains": Counter(), "searches": [],
            "seen": set(), "pages": Counter(), "title_of": {}})
        a["visits"] += 1
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if host:
            a["domains"][host] += 1
        hit = browser.search_terms(url)
        if hit:
            if hit not in a["seen"]:
                a["seen"].add(hit)
                a["searches"].append({"engine": hit[0], "terms": hit[1]})
        elif title:
            a["pages"][url] += 1
            a["title_of"].setdefault(url, title)

    for (day, location), a in sorted(acc.items()):
        doms = sorted(a["domains"].items(), key=lambda kv: (-kv[1], kv[0]))
        text = (f"Browser digest for {day} ({_weekday(day)}), {location}: "
                f"{a['visits']} visits across {len(doms)} sites. Top: "
                + ", ".join(f"{d} ({n})" for d, n in doms[:TOP_DOMAINS]) + ".")
        if a["searches"]:
            text += " Searched " + "; ".join(
                f'{s["engine"]} for "{s["terms"]}"'
                for s in a["searches"][:TOP_SEARCHES]) + "."
        pages = sorted(a["pages"].items(), key=lambda kv: (-kv[1], kv[0]))
        titles = [a["title_of"][u][:80] for u, _ in pages[:TOP_TITLES]]
        if titles:
            text += " Notable: " + "; ".join(f'"{t}"' for t in titles) + "."
        meta = {"visits": a["visits"], "domains": dict(doms[:META_DOMAINS]),
                "searches": a["searches"],
                "top_titles": [{"title": a["title_of"][u][:200], "visits": n}
                               for u, n in pages[:META_TITLES]]}
        yield _chunk("browser", location, day, text, meta)

def _claude_chunks(days):
    db = _index_ro()
    if db is None:
        return
    dayset = set(days)
    # day -> session_id -> [first_ts, project, turns, best_lineno, first_prompt]
    acc: dict[str, dict] = {}
    try:
        rows = db.execute(
            "SELECT timestamp, location, text, meta FROM chunks "
            "WHERE source = 'claude' AND timestamp != ''")
        for ts, loc, text, meta_json in rows:
            day = _local_day_of_ts(ts)
            if day not in dayset:
                continue
            m = json.loads(meta_json) if meta_json else {}
            sid = m.get("session_id", "")
            s = acc.setdefault(day, {}).setdefault(
                sid, [ts, "", 0, None, ""])
            s[2] += 1
            if ts < s[0]:
                s[0] = ts
            if loc and not s[1]:
                s[1] = loc
            lineno = m.get("lineno", 0)
            if m.get("role") == "user" and (s[3] is None or lineno < s[3]):
                s[3], s[4] = lineno, text
    except sqlite3.Error:
        return
    finally:
        db.close()

    for day in sorted(acc):
        sessions = sorted(acc[day].values())          # by first timestamp
        total = sum(s[2] for s in sessions)
        projects = []
        for s in sessions:
            p = _abbrev(s[1])
            if p and p not in projects:
                projects.append(p)
        text = (f"Claude digest for {day} ({_weekday(day)}): "
                f"{len(sessions)} session{'s' * (len(sessions) != 1)}, "
                f"{total} turns"
                + (", in " + ", ".join(projects) if projects else "") + ".")
        topics = [s[4][:TOPIC_CHARS] for s in sessions if s[4]]
        if topics:
            text += " Topics: " + "; ".join(f'"{t}"' for t in topics) + "."
        meta = {"total_turns": total,
                "sessions": [{"project": _abbrev(s[1]),
                              "first_prompt": s[4][:200], "turns": s[2]}
                             for s in sessions]}
        yield _chunk("claude", "claude", day, text, meta)

def _shell_chunks(days):
    from sources import shell
    start = datetime.fromisoformat(days[0]).timestamp()
    dayset = set(days)
    acc: dict[str, dict] = {}
    for epoch, cmd, cwd in shell.iter_dated_runs(start):
        day = _local_day_of_epoch(epoch)
        if day not in dayset:
            continue
        a = acc.setdefault(day, {"runs": 0, "by_cwd": Counter(),
                                 "commands": Counter()})
        a["runs"] += 1
        if cwd:
            a["by_cwd"][cwd] += 1
        a["commands"][cmd] += 1

    for day in sorted(acc):
        a = acc[day]
        text = (f"Shell digest for {day} ({_weekday(day)}): "
                f"{a['runs']} command{'s' * (a['runs'] != 1)}")
        cwds = sorted(a["by_cwd"].items(), key=lambda kv: (-kv[1], kv[0]))
        if cwds:
            text += f", mostly in {cwds[0][0]} ({cwds[0][1]})"
        cmds = sorted(a["commands"].items(), key=lambda kv: (-kv[1], kv[0]))
        text += ". Top: " + "; ".join(
            f'"{c[:60]}" (x{n})' for c, n in cmds[:3]) + "."
        meta = {"runs": a["runs"], "by_cwd": dict(cwds[:20]),
                "top_commands": [{"command": c[:200], "runs": n}
                                 for c, n in cmds[:META_COMMANDS]]}
        yield _chunk("shell", "shell", day, text, meta)

_BUILDERS = {"browser": _browser_chunks, "claude": _claude_chunks,
             "shell": _shell_chunks}

def iter_chunks():
    streams, recompute, backfill = _cfg()
    days = _days_to_digest(recompute, backfill)
    if not days:
        return
    for name in streams:
        yield from _BUILDERS[name](days)
