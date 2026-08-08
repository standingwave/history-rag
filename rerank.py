"""Optional cross-encoder stage over search_history's candidate pool.

Bi-encoder retrieval (one vector per chunk, L2 to the query) makes the index
searchable, but its distances are only weakly comparable across sources with
different registers and chunk lengths. A cross-encoder reads (query, chunk)
PAIRS, so it scores absolute relevance — measured on this index it pins
every section of the right note above other sources' near-misses and demotes
the short-title junk (YouTube shorts) that raw L2 loves.

Backend "flashrank": small ONNX cross-encoders on CPU, no torch. The default
ms-marco-TinyBERT-L-2-v2 scores ~64 pairs in ~120ms; the model downloads
once into ~/.claude/flashrank/. Reranking only ever reorders candidates the
vector stage already found — any failure here logs once, marks the stage
broken for this process, and search falls back to distance order.
"""
import os, sys
import config

CACHE_DIR = os.path.expanduser("~/.claude/flashrank")
MAX_CHARS = 1500                 # cross-encoder input cap per passage

_ranker = None
_failed = False

def available() -> bool:
    return config.RERANK_BACKEND == "flashrank" and not _failed

def rerank(query: str, texts: list):
    """Relevance score per text (higher = better), aligned with input order;
    None when the stage is off or broken (callers keep distance order)."""
    global _ranker, _failed
    if not available() or not texts:
        return None
    try:
        from flashrank import Ranker, RerankRequest
        if _ranker is None:
            os.makedirs(CACHE_DIR, exist_ok=True)
            _ranker = Ranker(model_name=config.RERANK_MODEL,
                             cache_dir=CACHE_DIR)
        req = RerankRequest(query=query, passages=[
            {"id": i, "text": t[:MAX_CHARS]} for i, t in enumerate(texts)])
        scores = [0.0] * len(texts)
        for r in _ranker.rerank(req):
            scores[r["id"]] = float(r["score"])
        return scores
    except Exception as e:
        _failed = True           # stay off for this process; restart retries
        print(f"rerank: falling back to distance order: {e}", file=sys.stderr)
        return None
