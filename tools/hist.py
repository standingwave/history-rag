#!/usr/bin/env python3
"""Terminal client for the hosted history service (deploy/lambda).

The Lambda replica speaks stateless MCP — plain JSON-RPC POSTs — so this is
just an envelope around the four tools (design: wip/SPEC-direct-access.md).
Stdlib only: copy this one file to any machine holding the secret URL.

    hist search "that proxy bug we hit" -k 5 --source claude
    hist window --since 2026-07-01 --group-by day
    hist expand 042e2d9b21022acd --context 10
    hist stats --locations
    hist ask "when did I set up the lambda replica?" --model haiku

Endpoint resolution: $HISTORY_RAG_URL, else the URL field of the
'history-rag remote' LastPass entry (name overridable via
$HISTORY_RAG_LPASS_ENTRY). The URL is the credential — keep it out of
shell history and dotfiles; the env var is for scripts that already
handle it carefully.

Suggested: alias hist='python3 <repo>/tools/hist.py'
"""
import argparse, json, os, subprocess, sys
import urllib.error, urllib.parse, urllib.request

TIMEOUT = 60        # first request after idle pays the cold start (~2-5s)
ASK_TIMEOUT = 120   # the agent loop runs multiple model calls


def resolve_url() -> str:
    url = os.environ.get("HISTORY_RAG_URL", "").strip()
    if not url:
        entry = os.environ.get("HISTORY_RAG_LPASS_ENTRY", "history-rag remote")
        try:
            r = subprocess.run(["lpass", "show", entry, "--url"],
                               capture_output=True, text=True, timeout=30)
            url = r.stdout.strip() if r.returncode == 0 else ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            url = ""
    if not url:
        sys.exit("no endpoint: set HISTORY_RAG_URL to the service URL, or "
                 "store it as the URL of the 'history-rag remote' LastPass "
                 "entry (entry name overridable via HISTORY_RAG_LPASS_ENTRY)")
    url = url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    return url


def _fetch(req, timeout: int) -> str:
    """One request with the transport-error mapping (no URLs in output —
    the URL is a credential)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit("endpoint returned 404 — the secret path is wrong or "
                     "was rotated; re-check the stored URL")
        if e.code >= 500:
            sys.exit(f"endpoint returned {e.code} — the service failed; "
                     "its CloudWatch logs have the traceback")
        sys.exit(f"endpoint returned {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"could not reach the endpoint ({e.reason}) — network, "
                 "DNS, or a mistyped host")

def call_tool(url: str, tool: str, arguments: dict) -> str:
    """POST one tools/call, return the tool's raw JSON string."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    envelope = json.loads(_fetch(req, TIMEOUT))
    if "error" in envelope:
        err = envelope["error"]
        sys.exit(f"MCP error: {err.get('message', err)}")
    return envelope["result"]["content"][0]["text"]

def ask_request(url: str, question: str, model: str) -> str:
    """GET the page's ask handler in JSON mode — same base URL, /search
    instead of /mcp (wip/SPEC-ask-mode.md)."""
    base = url[:-len("/mcp")] if url.endswith("/mcp") else url
    params = {"q": question, "mode": "ask", "json": "1"}
    if model:
        params["model"] = model
    req = urllib.request.Request(
        base + "/search?" + urllib.parse.urlencode(params))
    return _fetch(req, ASK_TIMEOUT)


def build_call(args) -> tuple[str, dict]:
    if args.cmd == "search":
        a = {"query": args.query, "k": args.k}
        for name in ("source", "location", "since", "until"):
            if getattr(args, name):
                a[name] = getattr(args, name)
        if args.include_undated:
            a["include_undated"] = True
        if args.max_distance is not None:
            a["max_distance"] = args.max_distance
        return "search_history", a
    if args.cmd == "window":
        a = {}
        for name in ("since", "until", "source", "location"):
            if getattr(args, name):
                a[name] = getattr(args, name)
        if args.group_by:
            a["group_by"] = args.group_by
        if args.limit is not None:
            a["limit"] = args.limit
        if args.offset:
            a["offset"] = args.offset
        if args.include_undated:
            a["include_undated"] = True
        return "list_window", a
    if args.cmd == "expand":
        return "expand", {"id": args.id, "context": args.context}
    return "history_stats", {"locations": True} if args.locations else {}


def _day(ts: str) -> str:
    return (ts or "")[:10]


def _flat(text: str, n: int) -> str:
    return " ".join(text.split())[:n]


def human_search(data):
    results = data.get("results", [])
    if not results:
        print("no matches")
    for r in results:
        head = f"{r['rank']:2}. [{r['source']}] {_day(r.get('timestamp', ''))}"
        if r.get("location"):
            head += f"  {r['location']}"
        print(head)
        print(f"    {_flat(r['text'], 300)}")
        print(f"    id {r['id']}  d={r['distance']}")
    if data.get("note"):
        print(data["note"])


def human_window(data):
    if "groups" in data:
        for g in data["groups"]:
            key = " · ".join(str(g[d]) or "(none)" for d in data["group_by"])
            span = f"{_day(g.get('earliest') or '')}..{_day(g.get('latest') or '')}"
            print(f"{g['count']:6}  {key}  ({span})")
        if data.get("groups_truncated"):
            print("(group list truncated; raise --limit)")
        return
    for r in data.get("results", []):
        ts = (r.get("timestamp") or "")[:16]
        print(f"{ts:16}  [{r['source']}] {r['id']}  {_flat(r['text'], 120)}")
    print(f"{data.get('count', 0)} of {data.get('total', 0)} shown")


def human_expand(data):
    c = data["chunk"]
    head = f"[{c['source']}] {c.get('timestamp', '')}"
    if c.get("location"):
        head += f"  {c['location']}"
    print(head)
    print()
    print(c["text"])
    if data.get("context") is not None:
        print(f"\n--- context ({data.get('context_source')}) ---")
        print(json.dumps(data["context"], indent=2, ensure_ascii=False))


def human_stats(data):
    health = data.get("health", {})
    if health.get("note"):
        print(f"!! {health['note']}")
    emb = data.get("embedding", {})
    model = f"  ({emb['model']}/{emb['dim']})" if emb else ""
    print(f"{data['total_chunks']} chunks{model}")
    for src, s in sorted(data.get("sources", {}).items()):
        span = f"{_day(s.get('earliest') or '')}..{_day(s.get('latest') or '')}"
        print(f"{src:9} {s['chunks']:7}  {span}")
        for loc, n in s.get("locations", {}).items():
            print(f"          {n:7}  {loc}")


def human_ask(data):
    print(data.get("answer") or "")
    if data.get("note"):
        print(f"({data['note']})")
    if data.get("citations"):
        print("\ncitations: " + ", ".join(data["citations"]))
    u = data.get("usage") or {}
    print(f"[{u.get('model', '')} · {u.get('turns', 0)} turns · "
          f"{u.get('in', 0)}+{u.get('out', 0)} tokens]")


HUMANS = {"search": human_search, "window": human_window,
          "expand": human_expand, "stats": human_stats, "ask": human_ask}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="hist", description="Search the hosted history service.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="semantic search")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5, help="max results (default 5)")
    s.add_argument("--source", default="")
    s.add_argument("--location", default="")
    s.add_argument("--since", default="")
    s.add_argument("--until", default="")
    s.add_argument("--include-undated", action="store_true")
    s.add_argument("--max-distance", type=float, default=None)

    w = sub.add_parser("window", help="exhaustive chronological listing")
    w.add_argument("--since", default="")
    w.add_argument("--until", default="")
    w.add_argument("--source", default="")
    w.add_argument("--location", default="")
    w.add_argument("--group-by", default="",
                   help="day | source | location | domain (comma-separated)")
    w.add_argument("--limit", type=int, default=None)
    w.add_argument("--offset", type=int, default=0)
    w.add_argument("--include-undated", action="store_true")

    e = sub.add_parser("expand", help="read one chunk in full, with context")
    e.add_argument("id")
    e.add_argument("--context", type=int, default=5)

    t = sub.add_parser("stats", help="what the index covers, and run health")
    t.add_argument("--locations", action="store_true")

    a = sub.add_parser("ask", help="ask a model a question over the history")
    a.add_argument("question")
    a.add_argument("--model", default="",
                   help="preset name from [ask.models] (default: first)")

    for cmd in (s, w, e, t, a):
        cmd.add_argument("--json", action="store_true",
                         help="emit the tool's raw JSON for piping")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.cmd == "ask":
        raw = ask_request(resolve_url(), args.question, args.model)
    else:
        tool, arguments = build_call(args)
        raw = call_tool(resolve_url(), tool, arguments)
    data = json.loads(raw)
    if isinstance(data, dict) and "error" in data:
        print(data["error"], file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(raw)
        return
    HUMANS[args.cmd](data)


if __name__ == "__main__":
    main()
