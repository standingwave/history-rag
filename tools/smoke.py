"""Dev smoke test: exercise every MCP tool path in-process after a change.

Read-only against the real index. Search calls embed via Ollama, so it must
be running. Exits non-zero on any failure. Also warns when a running MCP
server process is older than server.py — edits don't apply until the user
reconnects it (/mcp), and forgetting that cost us several confused minutes
more than once.

Run:  ~/.claude/rag-venv/bin/python tools/smoke.py
"""
import datetime, json, os, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server

FAILURES = 0

def check(name, fn):
    global FAILURES
    try:
        detail = fn()
        print(f"  ok   {name}" + (f" — {detail}" if detail else ""))
    except Exception as e:
        FAILURES += 1
        print(f"  FAIL {name}: {type(e).__name__}: {e}")

def main():
    stats = json.loads(server.history_stats(locations=True))
    print(f"index: {stats['total_chunks']} chunks, "
          f"{len(stats['sources'])} sources")

    # One list -> expand round trip per source: proves ids resolve and every
    # registered expander runs without blowing up.
    for src, info in stats["sources"].items():
        def probe(src=src, info=info):
            if info.get("earliest"):
                w = json.loads(server.list_window(
                    since=info["earliest"][:10], source=src, limit=1))
                if w.get("error") or not w["results"]:
                    raise RuntimeError(w.get("error", "list_window empty"))
                cid = w["results"][0]["id"]
            else:  # fully undated source: fall back to semantic search
                r = json.loads(server.search_history("anything", k=1, source=src))
                if not r["results"]:
                    return "empty source, expand skipped"
                cid = r["results"][0]["id"]
            x = json.loads(server.expand(cid, context=2))
            if "error" in x:
                raise RuntimeError(x["error"])
            return f"expand -> {x['context_source'] or 'no context'}"
        check(f"list/expand [{src}]", probe)

    today = datetime.date.today().isoformat()
    check("search (pool path)", lambda: f"{json.loads(server.search_history('test', k=2))['count']} hits")
    check("search (window path)", lambda: (lambda r: f"exact={r.get('exact', False)}, {r['count']} hits")(
        json.loads(server.search_history("work", k=2, since=today))))
    check("list_window paging", lambda: f"total={json.loads(server.list_window(since=today, limit=2))['total']}")
    check("bad bounds -> error", lambda: json.loads(
        server.search_history("x", since="not-a-date"))["error"][:22])
    check("bad id -> error", lambda: json.loads(server.expand("nope:0"))["error"][:18])

    _warn_stale_server()
    print(f"failures: {FAILURES}")
    sys.exit(1 if FAILURES else 0)

def _warn_stale_server():
    """A registered MCP server keeps running old code after edits."""
    try:
        src_mtime = os.path.getmtime(server.__file__)
        pids = subprocess.run(["pgrep", "-f", r"python.*server\.py"],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            lstart = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                                    capture_output=True, text=True).stdout.strip()
            if not lstart:
                continue
            started = datetime.datetime.strptime(
                lstart, "%a %b %d %H:%M:%S %Y").timestamp()
            if started < src_mtime:
                print(f"  NOTE running server (pid {pid}) predates server.py "
                      f"— /mcp reconnect to apply changes")
    except Exception:
        pass  # best-effort advisory only

if __name__ == "__main__":
    main()
