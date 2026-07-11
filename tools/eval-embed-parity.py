"""Test whether a hosted embedding API matches the index's vector space.

Gate 0 of wip/SPEC-lambda-remote.md. The index embeds raw text with no prompt
prefixes (Ollama); hosted APIs serving the same weights may apply prefixes or
run different precision, so their query vectors may not land where the local
ones do. The API is chosen by the configured model: nomic-embed-text tests
Nomic's API across task_types, mxbai-embed-large tests Mixedbread's API with
and without their retrieval query prompt. For each query this embeds via
local Ollama (the production query path) and via each API variant, then
compares (a) cosine similarity of the two query vectors and (b) top-k result
overlap against the production index. A PASS means Lambda can embed queries
via that variant (zip deploy); all-fail means bundling the GGUF (container).

Read-only: the production DB is only queried, never written.

Usage:
  NOMIC_API_KEY=... | MXBAI_API_KEY=...  tools/eval-embed-parity.py \
      [--queries F] [--k 10]
"""
import argparse, math, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests, sqlite_vec
import config

MXBAI_RETRIEVAL_PROMPT = ("Represent this sentence for searching relevant "
                          "passages: ")
PASS_COSINE, PASS_OVERLAP = 0.99, 0.9

def _post(url, key, body):
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                      json=body, timeout=60)
    if not r.ok:
        # The API's error detail is in the body; a bare raise_for_status
        # hides the one line that says what to fix.
        sys.exit(f"api {r.status_code} from {url}: {r.text[:500]}")
    return r.json()

def _variants():
    """(label, embed_fn) per API variant appropriate to the indexed model."""
    if config.EMBED_MODEL == "nomic-embed-text":
        key = os.environ.get("NOMIC_API_KEY") or sys.exit(
            "NOMIC_API_KEY not set — create one at https://atlas.nomic.ai")
        def nomic(task_type):
            return lambda q: _post(config.NOMIC_API_URL, key, {
                "model": config.NOMIC_API_MODEL, "task_type": task_type,
                "dimensionality": config.DIM,
                "texts": [q]})["embeddings"][0]
        return [(t, nomic(t)) for t in ("search_query", "search_document")]
    if config.EMBED_MODEL == "mxbai-embed-large":
        key = os.environ.get("MXBAI_API_KEY") or sys.exit(
            "MXBAI_API_KEY not set — create one at https://www.mixedbread.com")
        def mxbai(prompt):
            return lambda q: _post(config.MXBAI_API_URL, key, {
                "model": config.MXBAI_API_MODEL, "input": [prompt + q],
                "dimensions": config.DIM, "normalized": True,
                "encoding_format": "float"})["data"][0]["embedding"]
        return [("raw", mxbai("")),
                ("retrieval-prompt", mxbai(MXBAI_RETRIEVAL_PROMPT))]
    sys.exit(f"no hosted API known for model {config.EMBED_MODEL!r}; "
             f"the GGUF-in-container path is the only remote option.")

def embed_ollama(text: str):
    r = requests.post(config.OLLAMA, json={"model": config.EMBED_MODEL,
                                           "input": text}, timeout=60)
    r.raise_for_status()
    return r.json()["embeddings"][0]

def cosine(a, b):
    if len(a) != len(b):
        sys.exit(f"dimension mismatch: local {len(a)} vs api {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def knn_ids(db, qvec, k: int):
    rows = db.execute(
        "SELECT v.distance, c.id FROM vec_chunks v JOIN chunks c ON c.id=v.id "
        "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (sqlite_vec.serialize_float32(qvec), k)).fetchall()
    return [r[1] for r in rows]

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries",
                    default=os.path.join(os.path.dirname(__file__),
                                         "eval-queries.txt"))
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    variants = _variants()
    db = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    config.check_stamp(db)

    queries = [l.strip() for l in open(args.queries)
               if l.strip() and not l.startswith("#")]
    stats = {label: {"cos": [], "overlap": []} for label, _ in variants}

    for q in queries:
        t0 = time.monotonic()
        ovec = embed_ollama(q)
        oms = (time.monotonic() - t0) * 1000
        oids = knn_ids(db, ovec, args.k)
        line = [f"Q: {q}\n  ollama {oms:4.0f}ms"]
        for label, fn in variants:
            t0 = time.monotonic()
            avec = fn(q)
            ams = (time.monotonic() - t0) * 1000
            c = cosine(ovec, avec)
            aids = knn_ids(db, avec, args.k)
            ov = len(set(oids) & set(aids)) / max(1, len(oids))
            stats[label]["cos"].append(c)
            stats[label]["overlap"].append(ov)
            line.append(f"  {label}: {ams:4.0f}ms cos {c:.4f} "
                        f"overlap@{args.k} {ov:.0%}")
        print(" |".join(line))

    print(f"\nsummary over {len(queries)} queries "
          f"(pass: cos>={PASS_COSINE}, overlap>={PASS_OVERLAP}):")
    for label, _ in variants:
        mc = sum(stats[label]["cos"]) / len(queries)
        mo = sum(stats[label]["overlap"]) / len(queries)
        verdict = "PASS" if mc >= PASS_COSINE and mo >= PASS_OVERLAP else "fail"
        print(f"  {label:16} mean cos {mc:.4f}  mean overlap {mo:.0%}"
              f"  -> {verdict}")
    print("\nany PASS: set CLAUDE_RAG_EMBED_BACKEND to that API for the "
          "Lambda (zip deploy).\nall fail: bundle the GGUF in a container "
          "image (outcome B in the spec).")

if __name__ == "__main__":
    main()
