"""eval-model tool: the safety-critical parts — the candidate path can never
be the production path, and the in-process config override always restores."""
import importlib.util, pathlib
import config

def _load():
    p = pathlib.Path(__file__).resolve().parent.parent / "tools" / "eval-model.py"
    spec = importlib.util.spec_from_file_location("eval_model", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_candidate_path_is_derived_and_never_prod():
    ev = _load()
    assert ev.candidate_path("/x/history-rag.db", "mxbai-embed-large") == \
        "/x/history-rag.eval-mxbai-embed-large.db"
    for prod in ("/x/history-rag.db", "/x/odd-name", "/x/a.db"):
        assert ev.candidate_path(prod, "m") != prod

def test_build_candidate_overrides_then_restores(monkeypatch):
    ev = _load()
    import index
    seen = {}
    def fake_main():
        seen["db"] = config.DB_PATH
        seen["model"] = config.EMBED_MODEL
        seen["dim"] = config.DIM
    monkeypatch.setattr(index, "main", fake_main)
    before = (config.DB_PATH, config.EMBED_MODEL, config.DIM)
    ev.build_candidate("/tmp/cand.db", "cand-model", 1024)
    assert seen == {"db": "/tmp/cand.db", "model": "cand-model", "dim": 1024}
    assert (config.DB_PATH, config.EMBED_MODEL, config.DIM) == before

def test_build_candidate_restores_even_on_failure(monkeypatch):
    ev = _load()
    import index
    def boom():
        raise RuntimeError("build failed")
    monkeypatch.setattr(index, "main", boom)
    before = (config.DB_PATH, config.EMBED_MODEL, config.DIM)
    try:
        ev.build_candidate("/tmp/cand.db", "cand-model", 1024)
    except RuntimeError:
        pass
    assert (config.DB_PATH, config.EMBED_MODEL, config.DIM) == before
