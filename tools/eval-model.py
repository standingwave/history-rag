"""Evaluate a candidate embedding model side-by-side with production.

Builds a candidate index at a DERIVED path (<prod dir>/<prod>.eval-<model>.db
— the production path is never a target here, only read), then runs a fixed
query set against both and prints rankings joined on chunk ids (which are
embedder-independent). The TOML and the launchd job are untouched throughout:
the candidate build overrides config in-process, not via env vars.

Usage:
  eval-model.py --model mxbai-embed-large --dim 1024 [--queries F] [--k 10]
  eval-model.py --model mxbai-embed-large --delete     # remove candidate DB

Caveat printed in output: the candidate is built from today's sources, so it
lacks archive-only chunks (aged-out transcripts, expired browser rows);
comparisons only count ids present in both DBs.
"""
import argparse, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests, sqlite_vec
import config

DEEP_K = 200      # how far down the candidate ranking we look for prod's ids

def candidate_path(prod_path: str, model: str) -> str:
    d, base = os.path.split(prod_path)
    stem = base[:-3] if base.endswith(".db") else base
    return os.path.join(d, f"{stem}.eval-{model}.db")

def embed(model: str, text: str):
    r = requests.post(config.OLLAMA, json={"model": model, "input": text},
                      timeout=120)
    r.raise_for_status()
    return r.json()["embeddings"][0]

def open_db(path: str):
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db

def knn(db, qvec, k: int):
    t0 = time.monotonic()
    rows = db.execute(
        "SELECT v.distance, c.id, c.text FROM vec_chunks v "
        "JOIN chunks c ON c.id = v.id "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (sqlite_vec.serialize_float32(qvec), k)).fetchall()
    return rows, (time.monotonic() - t0) * 1000

def build_candidate(path: str, model: str, dim: int):
    """Run the indexer at the candidate path with the candidate model via
    in-process config override — env-free, so there is no variable a human
    can forget, and the production config never changes."""
    import index
    saved = (config.DB_PATH, config.EMBED_MODEL, config.DIM)
    saved_argv = sys.argv
    config.DB_PATH, config.EMBED_MODEL, config.DIM = path, model, dim
    sys.argv = ["index.py"]
    try:
        index.main()
    finally:
        config.DB_PATH, config.EMBED_MODEL, config.DIM = saved
        sys.argv = saved_argv

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--dim", type=int)
    ap.add_argument("--queries",
                    default=os.path.join(os.path.dirname(__file__),
                                         "eval-queries.txt"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--skip-build", action="store_true",
                    help="reuse an existing candidate DB")
    ap.add_argument("--delete", action="store_true",
                    help="remove the candidate DB and exit")
    args = ap.parse_args()

    prod = config.DB_PATH
    cand = candidate_path(prod, args.model)
    assert cand != prod

    if args.delete:
        if os.path.exists(cand):
            os.remove(cand)
            print(f"removed {cand}")
        else:
            print(f"no candidate at {cand}")
        return
    if not args.dim:
        sys.exit("--dim is required (e.g. 1024 for mxbai-embed-large)")

    try:
        embed(args.model, "availability probe")
    except requests.RequestException as e:
        sys.exit(f"model {args.model!r} not usable via Ollama ({e}); "
                 f"try: ollama pull {args.model}")

    print(f"prod:      {prod} ({config.EMBED_MODEL}/{config.DIM})")
    print(f"candidate: {cand} ({args.model}/{args.dim})")
    if not args.skip_build:
        build_candidate(cand, args.model, args.dim)

    pdb, cdb = open_db(prod), open_db(cand)
    config.check_stamp(pdb)                    # prod must match current config
    cand_ids = {r[0] for r in cdb.execute("SELECT id FROM chunks")}
    n_prod = pdb.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"\ncandidate holds {len(cand_ids)} of prod's {n_prod} chunks — "
          f"the gap is archive-only history; those ids are excluded from "
          f"comparison.\n")

    queries = [l.strip() for l in open(args.queries)
               if l.strip() and not l.startswith("#")]
    overlaps, top3_found = [], 0

    for q in queries:
        t0 = time.monotonic()
        pvec = embed(config.EMBED_MODEL, q)
        pe = (time.monotonic() - t0) * 1000
        prows, pt = knn(pdb, pvec, args.k)
        t0 = time.monotonic()
        cvec = embed(args.model, q)
        ce = (time.monotonic() - t0) * 1000
        crows, ct = knn(cdb, cvec, args.k)

        shared = [r for r in prows if r[1] in cand_ids]
        overlap = (len({r[1] for r in shared} & {r[1] for r in crows})
                   / max(1, len(shared)))
        overlaps.append(overlap)
        deep, _ = knn(cdb, cvec, DEEP_K)
        pos = {r[1]: i + 1 for i, r in enumerate(deep)}
        ranks = [str(pos.get(r[1], f">{DEEP_K}")) for r in shared[:3]]
        top3_found += sum(1 for r in ranks if r != f">{DEEP_K}")

        print(f"Q: {q}")
        print(f"  prod  embed {pe:4.0f}ms knn {pt:4.0f}ms | "
              f"cand embed {ce:4.0f}ms knn {ct:4.0f}ms | "
              f"overlap@{args.k} {overlap:.0%} | "
              f"prod-top3 ranks in cand: {', '.join(ranks) or '-'}")
        for (pd, pid, ptext), c in zip(prows[:3], crows[:3]):
            cd, cid, ctext = c
            print(f"    p {pd:5.3f} {ptext[:58]:58} | c {cd:5.3f} {ctext[:58]}")
        print()

    print(f"summary: mean overlap@{args.k} "
          f"{sum(overlaps) / max(1, len(overlaps)):.0%} over {len(queries)} "
          f"queries; prod-top3 found in candidate top-{DEEP_K}: {top3_found}"
          f"/{3 * len(queries)}")
    print(f"cleanup when done: tools/eval-model.py --model {args.model} --delete")

if __name__ == "__main__":
    main()
