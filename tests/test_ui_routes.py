"""The Lambda's UI routes (wip/SPEC-ui-rebuild.md): the gate accepts the
secret in the path or in the /login cookie and 404s otherwise; the static
client serves with its CSP; /config exposes only display fields; ask runs
only as an explicit POST; legacy /search URLs redirect to the hash form
without executing anything; MCP paths still fall through. app.py is
imported with its Lambda-only deps stubbed — no AWS, no network."""
import asyncio, json, sys, types

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


def _request(path, query="", method="GET", body=b"", headers=()):
    sent = []

    async def send(msg):
        sent.append(msg)

    chunks = [body]

    async def receive():
        data = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": data, "more_body": False}

    scope = {"type": "http", "method": method, "path": path,
             "query_string": query.encode(),
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(app._gate(scope, receive, send))
    status = sent[0]["status"]
    hdrs = {}
    for k, v in sent[0].get("headers", []):
        hdrs.setdefault(k.decode(), []).append(v.decode())
    flat = {k: v[0] for k, v in hdrs.items()}
    body_out = b"".join(m.get("body", b"") for m in sent[1:]).decode()
    return status, flat, body_out


def _get(path, query="", **kw):
    return _request(path, query, **kw)


@pytest.fixture(autouse=True)
def _no_s3(monkeypatch):
    monkeypatch.setattr(app, "_refresh_db", lambda: None)


COOKIE = ("cookie", "hist=s3cr3t")


# ── gate ─────────────────────────────────────────────────────────────────────

def test_gate_404s_without_secret_or_cookie():
    for path in ("/", "/ui.js", "/config", "/search", "/mcp", "/nope"):
        status, _, body = _get(path)
        assert status == 404 and body == ""


def test_secret_path_serves_the_ui():
    status, headers, body = _get("/s3cr3t/")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    assert '<script src="ui.js">' in body
    assert 'id="app"' in body


def test_bare_secret_redirects_to_slash():
    # without the trailing slash, the page's relative URLs would resolve
    # against / and miss the gate
    status, headers, _ = _get("/s3cr3t")
    assert status == 302 and headers["location"] == "/s3cr3t/"


def test_cookie_serves_the_ui_at_root():
    status, headers, body = _get("/", headers=[COOKIE])
    assert status == 200 and 'id="app"' in body


def test_wrong_cookie_still_404s():
    status, _, _ = _get("/", headers=[("cookie", "hist=wrong")])
    assert status == 404
    status, _, _ = _get("/", headers=[("cookie", "other=s3cr3t")])
    assert status == 404


def test_cookie_parsed_among_others():
    status, _, _ = _get("/", headers=[("cookie", "a=b; hist=s3cr3t; c=d")])
    assert status == 200


# ── /login ───────────────────────────────────────────────────────────────────

def test_login_sets_cookie_and_redirects():
    status, headers, _ = _get("/login", "token=s3cr3t")
    assert status == 302 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith("hist=s3cr3t; ")
    for attr in ("Path=/", "Max-Age=31536000", "Secure", "HttpOnly",
                 "SameSite=Lax"):
        assert attr in cookie


def test_login_with_bad_token_404s():
    status, headers, _ = _get("/login", "token=wrong")
    assert status == 404 and "set-cookie" not in headers
    status, _, _ = _get("/login")
    assert status == 404


# ── static + CSP ─────────────────────────────────────────────────────────────

def test_ui_csp_locks_to_self():
    _, headers, _ = _get("/s3cr3t/")
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert headers["x-content-type-options"] == "nosniff"


def test_ui_js_serves_as_javascript():
    status, headers, body = _get("/s3cr3t/ui.js")
    assert status == 200
    assert headers["content-type"].startswith("text/javascript")
    assert "'use strict'" in body


def test_ui_files_only_touch_served_routes():
    """Every fetch target in ui.js must be a route the server owns —
    the smoke that catches a client/server drift."""
    js = app._UI_JS.decode()
    assert "fetch('mcp'" in js
    assert "fetch('ask'" in js
    assert "fetch('config'" in js
    import re
    targets = set(re.findall(r"fetch\('([^']+)'", js))
    assert targets <= {"mcp", "ask", "config"}
    # nothing points at an absolute or external URL
    assert "fetch(\"http" not in js and "fetch('http" not in js
    # and everything is served as plain ASCII (no mojibake on the wire)
    app._UI_JS.decode("utf-8")
    app._UI_HTML.decode("utf-8")


# ── /config ──────────────────────────────────────────────────────────────────

def test_config_exposes_display_fields_only(monkeypatch):
    monkeypatch.setattr(app.ask, "presets", lambda: [
        {"name": "haiku", "backend": "anthropic", "model": "claude-haiku-4-5",
         "key_env": "K", "_key": "sk-secret", "latency": "~10–20s",
         "est_cost": "$0.004"},
        {"name": "gpt", "backend": "openai-compatible", "model": "g",
         "base_url": "https://api.example", "_key": "sk-2"}])
    status, headers, body = _get("/s3cr3t/config")
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {"models": [
        {"name": "haiku", "latency": "~10–20s", "est_cost": "$0.004"},
        {"name": "gpt"}]}
    assert "sk-secret" not in body and "claude-haiku" not in body
    assert "base_url" not in body and "key_env" not in body


def test_config_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(app.ask, "presets", lambda: [])
    _, _, body = _get("/s3cr3t/config")
    assert json.loads(body) == {"models": []}


# ── /ask ─────────────────────────────────────────────────────────────────────

def test_ask_posts_to_the_loop(monkeypatch):
    seen = {}

    def fake_ask(q, model="", strict=False):
        seen.update({"q": q, "model": model, "strict": strict})
        return {"answer": "A [id:x].", "citations": ["x"],
                "usage": {"model": "haiku", "turns": 2, "in": 5, "out": 7}}

    monkeypatch.setattr(app.ask, "ask", fake_ask)
    status, headers, body = _request("/s3cr3t/ask", method="POST",
        body=json.dumps({"q": "why", "model": "haiku"}).encode())
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert seen == {"q": "why", "model": "haiku", "strict": False}
    assert json.loads(body)["citations"] == ["x"]


def test_ask_strict_flag_passes_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(app.ask, "ask",
                        lambda q, model="", strict=False:
                        seen.update(strict=strict) or {"answer": ""})
    _request("/s3cr3t/ask", method="POST",
             body=json.dumps({"q": "why", "strict": True}).encode())
    assert seen == {"strict": True}


def test_ask_refuses_get(monkeypatch):
    monkeypatch.setattr(app.ask, "ask",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("GET must never run an ask")))
    status, headers, _ = _get("/s3cr3t/ask")
    assert status == 405 and headers["allow"] == "POST"


def test_ask_rejects_bad_bodies(monkeypatch):
    monkeypatch.setattr(app.ask, "ask",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not run")))
    status, _, body = _request("/s3cr3t/ask", method="POST", body=b"not json")
    assert status == 400 and "malformed" in json.loads(body)["error"]
    status, _, body = _request("/s3cr3t/ask", method="POST",
                               body=json.dumps({"model": "m"}).encode())
    assert status == 400 and "q is required" in json.loads(body)["error"]


def test_ask_error_payload_passes_through(monkeypatch):
    monkeypatch.setattr(app.ask, "ask",
                        lambda q, model="", strict=False:
                        {"error": "ask mode isn't configured"})
    status, _, body = _request("/s3cr3t/ask", method="POST",
                               body=json.dumps({"q": "x"}).encode())
    assert status == 200
    assert "isn't configured" in json.loads(body)["error"]


def test_ask_works_via_cookie(monkeypatch):
    monkeypatch.setattr(app.ask, "ask",
                        lambda q, model="", strict=False: {"answer": "A"})
    status, _, body = _request("/ask", method="POST",
                               body=json.dumps({"q": "x"}).encode(),
                               headers=[COOKIE])
    assert status == 200 and json.loads(body)["answer"] == "A"


# ── legacy /search redirect ──────────────────────────────────────────────────

def test_legacy_search_redirects_to_hash():
    status, headers, _ = _get("/s3cr3t/search", "q=proxy+bug&source=claude")
    assert status == 302
    assert headers["location"] == "/s3cr3t/#q=proxy+bug&source=claude"


def test_legacy_redirect_drops_execution_markers(monkeypatch):
    """go/tab/json/fragment stay behind: the client decides what to run,
    and an old ask bookmark must land as prefill, never as a paid call."""
    monkeypatch.setattr(app.ask, "ask",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("redirect ran an ask")))
    status, headers, _ = _get("/s3cr3t/search",
                              "q=x&mode=ask&model=haiku&go=1&json=1&tab=1")
    assert status == 302
    loc = headers["location"]
    assert loc == "/s3cr3t/#q=x&mode=ask&model=haiku"


def test_legacy_redirect_keeps_expand_and_window_params():
    status, headers, _ = _get("/s3cr3t/search",
                              "since=2026-07-01&until=2026-07-02&view=summaries"
                              "&offset=50&expand=abc&fragment=1&k=25")
    loc = headers["location"]
    assert "since=2026-07-01" in loc and "until=2026-07-02" in loc
    assert "view=summaries" in loc and "offset=50" in loc
    assert "expand=abc" in loc
    assert "fragment" not in loc and "k=" not in loc


def test_legacy_redirect_via_cookie_stays_at_root():
    status, headers, _ = _get("/search", "q=x", headers=[COOKIE])
    assert status == 302 and headers["location"] == "/#q=x"


# ── MCP fall-through ─────────────────────────────────────────────────────────

def test_mcp_paths_still_fall_through(monkeypatch):
    seen = []

    async def fake_inner(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    monkeypatch.setattr(app, "_inner", fake_inner)
    status, _, _ = _request("/s3cr3t/mcp", method="POST")
    assert status == 200 and seen == ["/mcp"]
    status, _, _ = _request("/mcp", method="POST", headers=[COOKIE])
    assert status == 200 and seen == ["/mcp", "/mcp"]


def test_unknown_subpaths_404_behind_the_gate():
    status, _, _ = _get("/s3cr3t/admin")
    assert status == 404
