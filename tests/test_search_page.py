"""/search page on the Lambda gate: the secret still gates everything,
the form renders, results render escaped (the index holds attacker-
influenceable text), expand round-trips, k is clamped, and MCP paths still
fall through to the inner app. app.py is imported with its Lambda-only
deps stubbed — no AWS, no network."""
import asyncio, base64, hashlib, json, sys, types

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

from tests.helpers import load_script

app = load_script("deploy/lambda/app.py", "lambda_app")


def _get(path, query=""):
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
    # deterministic dropdown/banner input; tests override where they care
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 0, "sources": {}}))


def test_gate_still_404s_without_secret():
    status, _, body = _get("/search")
    assert status == 404 and body == ""


def test_form_renders_with_security_headers():
    status, headers, body = _get("/s3cr3t/search")
    assert status == 200
    assert "<form" in body and 'name="q"' in body
    assert headers["content-security-policy"].startswith(
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-")
    assert headers["x-content-type-options"] == "nosniff"


def test_csp_hash_pins_the_rendered_script():
    _, headers, body = _get("/s3cr3t/search")
    script = body.split("<script>")[1].split("</script>")[0]
    digest = base64.b64encode(
        hashlib.sha256(script.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in headers["content-security-policy"]
    # progressive enhancement: JS-off must get a working button
    assert "<button>Search</button>" in body


def test_form_controls_default_to_all(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 2,
                             "sources": {"claude": {}, "browser": {}}}))
    _, _, body = _get("/s3cr3t/search")
    assert '<option value="" selected>All</option>' in body
    assert body.index(">browser<") < body.index(">claude<")   # sorted
    assert "<details><summary>filters" in body                # collapsed
    assert 'type="date" name="since"' in body
    assert 'type="number" name="k" min="1" max="25"' in body


def test_filters_map_to_kwargs_and_details_open(monkeypatch):
    seen = {}

    def fake_search(query, k=5, **kw):
        seen.update({"query": query, "k": k, **kw})
        return json.dumps({"query": query, "count": 0, "results": []})

    monkeypatch.setattr(app.server, "search_history", fake_search)
    _, _, body = _get("/s3cr3t/search",
                      "q=x&source=claude&since=2026-07-01&undated=1&k=7")
    assert seen == {"query": "x", "k": 7, "source": "claude",
                    "since": "2026-07-01", "include_undated": True}
    assert "<details open>" in body                           # filter active
    assert 'value="claude" selected' in body                  # echoed


def test_empty_filter_params_produce_no_kwargs(monkeypatch):
    seen = {}

    def fake_search(query, k=5, **kw):
        seen.update(kw)
        return json.dumps({"query": query, "count": 0, "results": []})

    monkeypatch.setattr(app.server, "search_history", fake_search)
    _get("/s3cr3t/search", "q=x&source=&since=&until=&location=")
    assert seen == {}


def test_bounds_without_q_call_list_window(monkeypatch):
    seen = {}

    def fake_window(**kw):
        seen.update(kw)
        return json.dumps({"count": 50, "total": 120, "window": {},
                           "results": [{"id": "w1", "source": "shell",
                                        "timestamp": "2026-07-01T10:00:00+00:00",
                                        "location": "", "text": "ls"}]})

    def no_search(*a, **k):
        raise AssertionError("search_history must not run in window mode")

    monkeypatch.setattr(app.server, "list_window", fake_window)
    monkeypatch.setattr(app.server, "search_history", no_search)
    _, _, body = _get("/s3cr3t/search", "since=2026-07-01&until=2026-07-02")
    assert seen == {"since": "2026-07-01", "until": "2026-07-02"}
    assert 'href="search?expand=w1"' in body
    assert "1–50 of 120" in body
    assert "offset=50" in body and "since=2026-07-01" in body  # older link


def test_window_paging_stops_at_the_end(monkeypatch):
    monkeypatch.setattr(app.server, "list_window",
                        lambda **kw: json.dumps(
                            {"count": 20, "total": 120, "window": {},
                             "results": [{"id": "w2", "source": "shell",
                                          "timestamp": "", "location": "",
                                          "text": "x"}]}))
    _, _, body = _get("/s3cr3t/search", "since=2026-07-01&offset=100")
    assert "101–120 of 120" in body
    assert "older &rarr;" not in body


def test_health_note_renders_as_banner(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 0, "sources": {},
                             "health": {"note": "index <stalled>"}}))
    _, _, body = _get("/s3cr3t/search")
    assert 'class="note"' in body and "index &lt;stalled&gt;" in body


def test_healthy_stats_render_freshness_line_above_form(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 5, "sources": {},
                             "health": {"last_run": "x", "age_minutes": 17,
                                        "status": "ok"}}))
    _, _, body = _get("/s3cr3t/search")
    assert "index ok · refreshed 17m ago" in body
    assert 'class="note"' not in body                # healthy: no warning
    assert body.index('class="health"') < body.index("<form")


def test_failing_sources_render_warning(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 5, "sources": {},
                             "health": {"age_minutes": 200, "status": "partial",
                                        "failing_sources": {"git": "boom",
                                                            "browser": "x"}}}))
    _, _, body = _get("/s3cr3t/search")
    assert "failing sources: browser, git" in body
    assert "index partial · refreshed 3h ago" in body


def test_no_health_renders_no_line():
    _, _, body = _get("/s3cr3t/search")     # autouse stub has no health key
    assert 'class="health"' not in body and 'class="note"' not in body


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
