"""/search page on the Lambda gate: the secret still gates everything,
the form renders, results render escaped (the index holds attacker-
influenceable text), expand round-trips, k is clamped, and MCP paths still
fall through to the inner app. app.py is imported with its Lambda-only
deps stubbed — no AWS, no network."""
import asyncio, importlib.util, json, pathlib, sys, types

import sqlite3 as _real_sqlite3

import pytest

# app.py's Lambda-only imports, stubbed before load. pysqlite3 maps to the
# real sqlite3 so app.py's sys.modules["sqlite3"] swap is a no-op here.
sys.modules.setdefault("pysqlite3", _real_sqlite3)
_boto3 = types.ModuleType("boto3")
_boto3.client = lambda *a, **k: types.SimpleNamespace()
sys.modules.setdefault("boto3", _boto3)
_mangum = types.ModuleType("mangum")
_mangum.Mangum = lambda app, lifespan="auto": app
sys.modules.setdefault("mangum", _mangum)

import os
os.environ.setdefault("CLAUDE_RAG_SYNC_BUCKET", "test-bucket")
os.environ.setdefault("CLAUDE_RAG_URL_SECRET", "s3cr3t")

_spec = importlib.util.spec_from_file_location(
    "lambda_app", pathlib.Path(__file__).resolve().parent.parent
    / "deploy" / "lambda" / "app.py")
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


def _get(path, query="", inner=None):
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": "http", "method": "GET", "path": path,
             "query_string": query.encode()}
    asyncio.run(app._gate(scope, receive, send))
    status = sent[0]["status"]
    headers = {k.decode(): v.decode() for k, v in sent[0].get("headers", [])}
    body = b"".join(m.get("body", b"") for m in sent[1:]).decode()
    return status, headers, body


@pytest.fixture(autouse=True)
def _no_s3(monkeypatch):
    monkeypatch.setattr(app, "_refresh_db", lambda: None)


def test_gate_still_404s_without_secret():
    status, _, body = _get("/search")
    assert status == 404 and body == ""


def test_form_renders_with_security_headers():
    status, headers, body = _get("/s3cr3t/search")
    assert status == 200
    assert "<form" in body and 'name="q"' in body
    assert headers["content-security-policy"] == \
        "default-src 'none'; style-src 'unsafe-inline'"
    assert headers["x-content-type-options"] == "nosniff"


def test_results_render_escaped_with_expand_link(monkeypatch):
    calls = []

    def fake_search(query, k=5):
        calls.append((query, k))
        return json.dumps({"query": query, "count": 1, "results": [
            {"rank": 1, "id": "abc123", "source": "browser", "distance": 0.5,
             "text": "<script>alert(1)</script> & more",
             "timestamp": "2026-07-02T10:00:00+00:00",
             "location": "chrome:<Default>"}]})

    monkeypatch.setattr(app.server, "search_history", fake_search)
    status, _, body = _get("/s3cr3t/search", "q=hello&k=99")
    assert status == 200
    assert calls == [("hello", 25)]                 # k clamped to 25
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in body
    assert "<script>alert" not in body
    assert "chrome:&lt;Default&gt;" in body
    assert 'href="search?expand=abc123"' in body
    assert 'value="hello"' in body                  # query echoed, escaped


def test_bad_k_falls_back_to_default(monkeypatch):
    calls = []
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5: calls.append(k) or
                        json.dumps({"query": query, "count": 0, "results": []}))
    _get("/s3cr3t/search", "q=x&k=banana")
    assert calls == [5]


def test_expand_roundtrip(monkeypatch):
    seen = []

    def fake_expand(cid):
        seen.append(cid)
        return json.dumps({"chunk": {"id": cid, "source": "git",
                                     "timestamp": "2026-07-01T00:00:00+00:00",
                                     "location": "repo@host",
                                     "text": "fix <thing>", "meta": {}},
                           "context": ["a", "b"], "context_source": "index"})

    monkeypatch.setattr(app.server, "expand", fake_expand)
    status, _, body = _get("/s3cr3t/search", "expand=abc123")
    assert status == 200
    assert seen == ["abc123"]
    assert "fix &lt;thing&gt;" in body
    assert "context" in body and "index" in body


def test_tool_error_renders_as_text(monkeypatch):
    monkeypatch.setattr(app.server, "expand",
                        lambda cid: json.dumps({"error": "no chunk with id 'z'"}))
    status, _, body = _get("/s3cr3t/search", "expand=z")
    assert status == 200
    assert "no chunk with id" in body


def test_mcp_paths_still_fall_through(monkeypatch):
    seen = []

    async def fake_inner(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    monkeypatch.setattr(app, "_inner", fake_inner)
    status, _, _ = _get("/s3cr3t/mcp")
    assert status == 200 and seen == ["/mcp"]
