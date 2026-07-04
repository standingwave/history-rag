"""Test environment. Everything points into a throwaway tmp dir BEFORE any
project module is imported — config freezes some values at import time, so
this env setup must run first (pytest imports conftest before test modules).
Time-zone-sensitive tests assume America/Los_Angeles; pinned here."""
import os, sys, tempfile, time, pathlib

_TMP = tempfile.mkdtemp(prefix="history-rag-tests-")
os.environ["CLAUDE_RAG_CONFIG"] = os.path.join(_TMP, "config.toml")  # absent -> defaults
os.environ["CLAUDE_RAG_DB"] = os.path.join(_TMP, "index.db")
os.environ["CLAUDE_RAG_APPUSAGE_DB"] = os.path.join(_TMP, "appusage.db")
os.environ["CLAUDE_RAG_ATUIN_DB"] = os.path.join(_TMP, "atuin.db")   # absent -> no atuin
os.environ["CLAUDE_RAG_HISTFILES"] = ""
os.environ["CLAUDE_RAG_NOTIFY"] = "false"   # no real macOS notifications
os.environ["TZ"] = "America/Los_Angeles"
time.tzset()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hashlib
import pytest

@pytest.fixture(autouse=True)
def _reset_hidden_globals():
    """Per-test reset of module-level caches that would leak config state."""
    import sources.browser as browser
    browser._keep_table = None
    yield
    browser._keep_table = None

@pytest.fixture
def fake_embed(monkeypatch):
    """Deterministic hash->vector embedder; patches index and server so no
    Ollama is needed. Same text always embeds identically, different text
    differently — enough to exercise storage, KNN, and re-embed plumbing.
    `.calls` records every text embedded, so tests can assert re-embed
    behavior (e.g. metadata refresh must embed nothing)."""
    import config, index, server

    def one(text: str):
        h = hashlib.sha256(text.encode()).digest()          # 32 bytes
        vec = [(b - 128) / 128.0 for b in h] * (config.DIM // 32 + 1)
        return vec[:config.DIM]

    one.calls = []
    def batch(texts):
        one.calls.extend(texts)
        return [one(t) for t in texts]

    monkeypatch.setattr(index, "embed_batch", batch)
    monkeypatch.setattr(server, "_embed", one)
    return one

@pytest.fixture
def scratch_db(monkeypatch, tmp_path):
    """Point index AND server (attribute-access config) at a fresh temp DB."""
    import config
    path = str(tmp_path / "ix.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    return path
