"""Rerank stage: per-source pooling, the score envelope, and the fail-open
fallback. The cross-encoder itself is faked — model quality is not a unit
concern."""
import json
import pytest
import rerank, server
from tests.helpers import mk_source, rec, run_index

@pytest.fixture
def rr_on(monkeypatch):
    monkeypatch.setattr(rerank, "available", lambda: True)

def _seed(monkeypatch):
    run_index(monkeypatch, [
        mk_source("alpha", [(f"a{i}", f"alpha text {i}", rec("alpha"))
                            for i in range(3)]),
        mk_source("beta", [("b0", "the beta answer", rec("beta"))]),
    ])

def test_unfiltered_pool_spans_sources_and_score_orders(scratch_db, fake_embed,
                                                        monkeypatch, rr_on):
    _seed(monkeypatch)
    offered = {}
    def fake(q, texts):
        offered["texts"] = texts
        return [1.0 if "beta" in t else 0.1 for t in texts]
    monkeypatch.setattr(rerank, "rerank", fake)
    r = json.loads(server.search_history("anything", k=2))
    assert r["reranked"] is True and r["count"] == 2
    # every source's candidates reached the cross-encoder
    assert any("beta" in t for t in offered["texts"])
    assert sum("alpha" in t for t in offered["texts"]) == 3
    # score, not distance, orders the results
    top = r["results"][0]
    assert top["id"] == "b0" and top["score"] == 1.0 and top["rank"] == 1
    assert all("score" in x for x in r["results"])

def test_scoped_search_reranks_within_source(scratch_db, fake_embed,
                                             monkeypatch, rr_on):
    _seed(monkeypatch)
    monkeypatch.setattr(rerank, "rerank",
                        lambda q, texts: [float(i) for i in range(len(texts))])
    r = json.loads(server.search_history("anything", k=2, source="alpha"))
    assert r["reranked"] is True and r.get("exact") is True
    assert {x["source"] for x in r["results"]} == {"alpha"}
    assert r["results"][0]["score"] == 2.0    # highest fake score wins

def test_rerank_failure_falls_back_to_distance(scratch_db, fake_embed,
                                               monkeypatch, rr_on):
    _seed(monkeypatch)
    monkeypatch.setattr(rerank, "rerank", lambda q, texts: None)
    r = json.loads(server.search_history("alpha text 1", k=2))
    assert "reranked" not in r and r["count"] == 2
    assert all("score" not in x for x in r["results"])
    assert [x["rank"] for x in r["results"]] == [1, 2]

def test_rerank_off_by_default(scratch_db, fake_embed, monkeypatch):
    _seed(monkeypatch)
    r = json.loads(server.search_history("alpha text 1", k=2))
    assert "reranked" not in r
    assert all("score" not in x for x in r["results"])

def test_rerank_module_fails_open(monkeypatch):
    import sys, config
    monkeypatch.setattr(config, "RERANK_BACKEND", "flashrank")
    monkeypatch.setattr(rerank, "_failed", False)
    monkeypatch.setattr(rerank, "_ranker", None)
    monkeypatch.setitem(sys.modules, "flashrank", None)   # import breaks
    assert rerank.rerank("q", ["text"]) is None
    assert rerank._failed and not rerank.available()
