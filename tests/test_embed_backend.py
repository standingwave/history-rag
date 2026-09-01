"""Query-embed backend dispatch: the default stays on Ollama with the exact
request shape it always sent; anything else fails loudly (a typo'd backend
must not silently fall through to Ollama and quietly query the wrong
space). Hosted backends were removed with the Lambda (2026-09-01) — the
Convex app embeds queries server-side, and formerly-hosted names must now
fail like any other unknown backend."""
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


@pytest.mark.parametrize("backend", ["bogus", "nomic-api", "mixedbread-api",
                                     "hf-inference"])
def test_non_ollama_backends_raise(capture_post, monkeypatch, backend):
    monkeypatch.setattr(config, "EMBED_BACKEND", backend)
    with pytest.raises(ValueError, match=backend):
        server._embed("hello")
    assert "url" not in capture_post                 # nothing was sent
