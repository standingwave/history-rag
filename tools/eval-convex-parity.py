#!/usr/bin/env python3
"""Does Convex search agree with the local index? (wip/SPEC-convex-app.md
stage 3, checks #1 and #2.)

For each query: local `server.search_history` (Ollama query embedding,
rerank off so both sides rank by vector distance alone) versus Convex
`search:searchInternal` (HF Inference query embedding, one namespace per
source searched in parallel). Both hold the same stored vectors, so any
gap is the query embedding, the per-namespace candidate split, or the
month-filter window compromise.

  #1 parity:  top-k overlap, all sources, no window. Pass: median ≥ 4/5.
  #2 windows: the same queries with 1-, 7- and 90-day windows ending
              today; a miss is a local result Convex didn't return.
              Pass: no misses at ≤ 7 days.

Read-only on both sides.

Usage: tools/eval-convex-parity.py [--queries F] [--k 5] [--windows]
Config: [convex] url + deploy key (as tools/sync-convex.py)
"""
import argparse, importlib.util, json, os, statistics, sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config
config.RERANK_BACKEND = "none"
import server

def _sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_convex", os.path.join(ROOT, "tools", "sync-convex.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def load_queries(path):
    with open(path) as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def local_ids(q, k, since="", until=""):
    r = json.loads(server.search_history(q, k=k, since=since, until=until))
    return [x["id"] for x in r.get("results", [])], r

def convex_ids(client, q, k, since=None, until=None):
    args = {"query": q, "limit": k}
    if since: args["since"] = since
    if until: args["until"] = until
    r = client.action("search:searchInternal", args)
    return [x["id"] for x in r["results"]], r

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", default=os.path.expanduser("~/.claude/eval-queries.txt"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--windows", action="store_true")
    a = ap.parse_args()
    queries = load_queries(a.queries)
    client = _sync_module()._client()

    print(f"#1 parity — {len(queries)} queries, top-{a.k}, all sources")
    overlaps = []
    for q in queries:
        l, _ = local_ids(q, a.k)
        c, cr = convex_ids(client, q, a.k)
        ov = len(set(l) & set(c))
        overlaps.append(ov)
        srcs = "".join(s[0] for s in (x["source"] for x in cr["results"]))
        print(f"  {ov}/{a.k}  {q[:60]:60}  convex:{srcs}")
    med = statistics.median(overlaps)
    print(f"  median overlap {med}/{a.k}  mean {statistics.mean(overlaps):.2f}  "
          f"-> {'PASS' if med >= 4 * a.k / 5 else 'fail'}")

    if not a.windows:
        return
    today = date.today()
    print(f"\n#2 windows — k=10, ending {today}")
    for days in (1, 7, 90):
        since = (today - timedelta(days=days - 1)).isoformat()
        until = today.isoformat()
        misses = 0; total_local = 0; dropped = 0; cands = 0
        for q in queries:
            l, _ = local_ids(q, 10, since, until)
            c, cr = convex_ids(client, q, 10, since, until)
            total_local += len(l)
            misses += len(set(l) - set(c))
            dropped += cr["dropped"]; cands += cr["candidates"]
        waste = dropped / cands if cands else 0
        verdict = ("PASS" if misses == 0 else "fail") if days <= 7 else \
                  ("PASS" if waste <= 0.2 else "fail")
        print(f"  {days:3}d  local results {total_local:3}  misses {misses:3}  "
              f"dropped {dropped}/{cands} ({waste:.0%})  -> {verdict}")

if __name__ == "__main__":
    main()
