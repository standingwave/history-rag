"""Query-embed backend dispatch: the default stays on Ollama with the exact
request shape it always sent; nomic-api sends the API's shape with the key;
anything else fails loudly (a typo'd backend must not silently fall through
to Ollama and quietly query the wrong space)."""
import pytest
import config, server


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"embeddings": [[0.0] * config.DIM]}


@pytest.fixture
def capture_post(monkeypatch):
    calls = {}

    def post(url, **kw):
        calls["url"] = url
        calls.update(kw)
        return _Resp()

    monkeypatch.setattr(server.requests, "post", post)
    return calls


def test_default_backend_is_ollama_unchanged(capture_post):
    assert config.EMBED_BACKEND == "ollama"          # the zero-config default
    vec = server._embed("hello")
    assert capture_post["url"] == config.OLLAMA
    assert capture_post["json"] == {"model": config.EMBED_MODEL,
                                    "input": "hello"}
    assert "headers" not in capture_post             # no auth locally
    assert len(vec) == config.DIM


def test_nomic_backend_sends_api_shape(capture_post, monkeypatch):
    monkeypatch.setattr(config, "EMBED_BACKEND", "nomic-api")
    monkeypatch.setattr(config, "NOMIC_API_KEY", "sekret")
    server._embed("hello")
    assert capture_post["url"] == config.NOMIC_API_URL
    assert capture_post["json"] == {"model": config.NOMIC_API_MODEL,
                                    "task_type": config.NOMIC_TASK_TYPE,
                                    "dimensionality": config.DIM,
                                    "texts": ["hello"]}
    assert capture_post["headers"]["Authorization"] == "Bearer sekret"


def test_mixedbread_backend_sends_api_shape(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BACKEND", "mixedbread-api")
    monkeypatch.setattr(config, "MXBAI_API_KEY", "sekret")
    calls = {}

    class _MxResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.0] * config.DIM}]}

    def post(url, **kw):
        calls["url"] = url
        calls.update(kw)
        return _MxResp()

    monkeypatch.setattr(server.requests, "post", post)
    vec = server._embed("hello")
    assert calls["url"] == config.MXBAI_API_URL
    assert calls["json"] == {"model": config.MXBAI_API_MODEL,
                             "input": ["hello"],   # default: no query prompt
                             "dimensions": config.DIM,
                             "normalized": True,
                             "encoding_format": "float"}
    assert calls["headers"]["Authorization"] == "Bearer sekret"
    assert len(vec) == config.DIM


def test_unknown_backend_raises(capture_post, monkeypatch):
    monkeypatch.setattr(config, "EMBED_BACKEND", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        server._embed("hello")
    assert "url" not in capture_post                 # nothing was sent
