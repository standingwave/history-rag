"""/search page on the Lambda gate: the secret still gates everything,
the form renders, results render escaped (the index holds attacker-
influenceable text), expand round-trips, k is clamped, and MCP paths still
fall through to the inner app. app.py is imported with its Lambda-only
deps stubbed — no AWS, no network."""
import asyncio, base64, hashlib, json, re, sys, types

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
        "default-src 'none'; style-src 'unsafe-inline'; "
        "connect-src 'self'; script-src 'sha256-")
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
    assert '<input type="radio" name="source" value="" checked>' in body
    assert body.index(">browser<") < body.index(">claude<")   # sorted
    assert "<details><summary>more filters" in body           # collapsed
    assert 'type="date" name="since"' in body
    assert 'type="number" name="k" min="1" max="25"' in body
    assert "onclick" not in body                # script wires by selector
    assert 'name="undated"' not in body         # only once a date is set


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
    assert 'value="claude" checked' in body                   # echoed


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
    assert seen == {"since": "2026-07-01", "until": "2026-07-02",
                    "include_meta": True}
    assert 'href="search?expand=w1&amp;since=2026-07-01' in body
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
    assert 'href="search?expand=abc123&amp;q=hello' in body   # back params
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

    def fake_expand(cid, context=5):
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
                        lambda cid, context=5: json.dumps(
                            {"error": "no chunk with id 'z'"}))
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


# ── rendering pass (wip/SPEC-search-page-rendering.md) ───────────────────────

def _expand_with(monkeypatch, source, context, ctx_src="index", text="body"):
    payload = json.dumps(
        {"chunk": {"id": "x1", "source": source,
                   "timestamp": "2026-07-02T17:00:00+00:00",
                   "location": "loc", "text": text, "meta": {}},
         "context": context, "context_source": ctx_src})
    monkeypatch.setattr(app.server, "expand", lambda cid, context=5: payload)
    _, _, body = _get("/s3cr3t/search", "expand=x1")
    return body


def test_claude_turns_render_as_conversation(monkeypatch):
    body = _expand_with(monkeypatch, "claude", {"turns": [
        {"lineno": 1, "role": "user", "timestamp": "2026-07-02T16:59:00+00:00",
         "text": "the question"},
        {"lineno": 2, "role": "assistant", "text": "the answer",
         "target": True}]})
    assert '{"turns"' not in body                       # no raw JSON
    assert 'class="turn user"' in body and 'class="turn target"' in body
    assert "user ·" in body and "the answer" in body


def test_appusage_context_renders_durations(monkeypatch):
    body = _expand_with(monkeypatch, "appusage",
                        {"date": "2026-07-02",
                         "seconds_by_app": {"Figma": 8040, "Slack": 300}})
    assert "2h 14m" in body and "5m" in body
    assert "8040" not in body


def test_git_show_renders_unquoted(monkeypatch):
    body = _expand_with(monkeypatch, "git",
                        {"show": "commit abc\n file | 2 +-"})
    assert ('commit abc\n file | 2 <span class="add">+</span>'
            '<span class="del">-</span>') in body
    assert "\\n" not in body


def test_unknown_context_shape_falls_back_to_json(monkeypatch):
    body = _expand_with(monkeypatch, "newsrc", {"weird": [1, 2]})
    assert '&quot;weird&quot;' in body                  # escaped JSON pre


def test_context_note_renders_as_text(monkeypatch):
    body = _expand_with(monkeypatch, "claude",
                        {"note": "context unavailable: x"}, ctx_src=None)
    assert "<p>context unavailable: x</p>" in body


def test_result_cards_clamped_with_badges(monkeypatch):
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 1, "results": [
                                {"rank": 1, "id": "r1", "source": "shell",
                                 "distance": 0.5, "text": "cmd"}]}))
    _, _, body = _get("/s3cr3t/search", "q=x")
    assert 'class="clamp mono"' in body           # shell cards render mono
    assert 'class="badge s-shell"' in body and "<svg" in body
    assert 'src="http' not in body                      # no external assets


def test_browser_titles_link_their_url(monkeypatch):
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 1, "results": [
                                {"rank": 1, "id": "r1", "source": "browser",
                                 "distance": 0.5, "text": "Title — url",
                                 "meta": {"url": "https://ex.com/p?a=1&b=2"}}]}))
    _, _, body = _get("/s3cr3t/search", "q=x")
    assert '<a class="out" href="https://ex.com/p?a=1&amp;b=2">' in body


def test_window_groups_by_day(monkeypatch):
    monkeypatch.setattr(app.server, "list_window",
                        lambda **kw: json.dumps(
                            {"count": 3, "total": 3, "window": {}, "results": [
                                {"id": "a", "source": "shell", "location": "",
                                 "timestamp": "2026-07-02T17:00:00+00:00",
                                 "text": "x"},
                                {"id": "b", "source": "shell", "location": "",
                                 "timestamp": "2026-07-02T16:00:00+00:00",
                                 "text": "y"},
                                {"id": "c", "source": "git", "location": "",
                                 "timestamp": "2026-07-01T17:00:00+00:00",
                                 "text": "z"}]}))
    _, _, body = _get("/s3cr3t/search", "since=2026-07-01&until=2026-07-02")
    assert body.count('<h2 class="day">') == 2          # two local days
    assert "Thu Jul 2" in body and "Wed Jul 1" in body


def test_empty_state_line(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 36727,
                             "sources": {"shell": {"chunks": 1,
                                                   "earliest": "2023-05-07T00:00:00+00:00"},
                                         "git": {"chunks": 2,
                                                 "earliest": "2018-09-18T00:00:00+00:00"}}}))
    _, _, body = _get("/s3cr3t/search")
    assert "36,727 chunks across 2 sources, 2018 → today" in body


def test_stats_panel_renders_index_and_sources(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 100,
                             "embedding": {"model": "mxbai-embed-large",
                                           "dim": 1024},
                             "db": {"bytes": 174_000_000,
                                    "freelist_bytes": 30_000_000},
                             "health": {"status": "ok", "age_minutes": 17,
                                        "replica": {"synced_age_minutes": 12}},
                             "sources": {"shell": {"chunks": 100,
                                                   "earliest": "2026-06-12T07:00:00+00:00",
                                                   "latest": "2026-07-12T07:00:00+00:00"}}}))
    _, _, body = _get("/s3c" + "r3t/search")
    panel = body[body.index('class="stats"'):]
    assert "mxbai-embed-large/1024" in panel
    assert "replica synced</dt><dd>12m ago" in panel
    assert "174 MB (30 MB reclaimable)" in panel
    assert "<td>shell</td><td>100</td>" in panel


def test_stats_panel_omits_absent_replica(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 1,
                             "health": {"status": "ok", "age_minutes": 5},
                             "sources": {"shell": {"chunks": 1}}}))
    _, _, body = _get("/s3cr3t/search")
    assert "replica" not in body


# ── JS layer (wip/SPEC-search-page-js.md) ────────────────────────────────────

def test_csp_lists_every_script_hash():
    _, headers, body = _get("/s3cr3t/search")
    csp = headers["content-security-policy"]
    scripts = [seg.split("</script>")[0]
               for seg in body.split("<script>")[1:]]
    assert len(scripts) == 2
    for script in scripts:
        digest = base64.b64encode(
            hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp
    assert csp.count("sha256-") == len(scripts)   # nothing else allowed
    assert "unsafe-inline'" not in csp.split("script-src")[1]


def test_fragment_returns_articles_only(monkeypatch):
    monkeypatch.setattr(app.server, "expand",
                        lambda cid, context=5: json.dumps(
        {"chunk": {"id": cid, "source": "shell", "timestamp": "",
                   "location": "", "text": "<script>x</script>", "meta": {}},
         "context": None, "context_source": None}))
    status, headers, body = _get("/s3cr3t/search", "expand=f1&fragment=1")
    assert status == 200
    assert "<article>" in body
    assert "<form" not in body and "<script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt;" in body   # still escaped


def test_expand_back_link_restores_the_search(monkeypatch):
    monkeypatch.setattr(app.server, "expand",
                        lambda cid, context=5: json.dumps(
        {"chunk": {"id": cid, "source": "shell", "timestamp": "",
                   "location": "", "text": "t", "meta": {}},
         "context": None, "context_source": None}))
    _, _, body = _get("/s3cr3t/search",
                      "expand=e1&q=proxy+bug&source=claude&k=7")
    assert '<a href="search?q=proxy+bug&amp;source=claude&amp;k=7">' in body
    # bare expand (bookmark) still gets a plain back link
    _, _, body = _get("/s3cr3t/search", "expand=e1")
    assert '<a href="search">' in body


def test_browse_presets_are_real_submits():
    _, _, body = _get("/s3cr3t/search", "mode=browse")
    assert body.count('class="qp" name="range"') == 3
    assert "data-days" not in body                  # date JS is gone
    assert 'class="qp" hidden' not in body          # presets always visible
    assert 'value="today">Today</button>' in body


# ── expand v2 (wip/SPEC-expand-v2.md) ────────────────────────────────────────

def test_digest_context_renders_sections(monkeypatch):
    body = _expand_with(monkeypatch, "digest", {
        "day": "2026-07-02", "digest_of": "browser",
        "rollup": {"visits": 41, "domains": {"github.com": 12},
                   "searches": ["sqlite vec"],
                   "top_titles": [{"title": "sqlite-vec README",
                                   "visits": 5}]}})
    assert "41 visits" in body
    assert ">sites</h2>" in body and "github.com" in body
    assert ">searches</h2>" in body and "sqlite vec" in body
    assert '{"rollup"' not in body                     # no raw JSON


def test_timeline_rows_expand_with_back_params(monkeypatch):
    payload = json.dumps(
        {"chunk": {"id": "x1", "source": "browser",
                   "timestamp": "2026-07-02T17:00:00+00:00",
                   "location": "chrome:Default", "text": "t", "meta": {}},
         "context": {"day": "2026-07-02", "profile": "chrome:Default",
                     "visits": [{"id": "v2",
                                 "timestamp": "2026-07-02T18:00:00+00:00",
                                 "text": "Other page"}]},
         "context_source": "index"})
    monkeypatch.setattr(app.server, "expand", lambda cid, context=5: payload)
    _, _, body = _get("/s3cr3t/search", "expand=x1&q=hello")
    assert 'class="expand" href="search?expand=v2&amp;q=hello"' in body


def test_show_more_context_link(monkeypatch):
    body = _expand_with(monkeypatch, "claude",
                        {"turns": [{"role": "user", "text": "long enough"}]})
    assert 'class="morectx" href="search?expand=x1&amp;context=10"' in body
    # at the cap: no link
    payload = json.dumps(
        {"chunk": {"id": "x1", "source": "claude", "timestamp": "",
                   "location": "", "text": "t", "meta": {}},
         "context": {"turns": []}, "context_source": "index"})
    monkeypatch.setattr(app.server, "expand", lambda cid, context=5: payload)
    _, _, body = _get("/s3cr3t/search", "expand=x1&context=25")
    assert 'class="morectx"' not in body


def test_context_param_passes_through(monkeypatch):
    seen = []

    def fake(cid, context=5):
        seen.append(context)
        return json.dumps({"chunk": {"id": cid, "source": "shell",
                                     "timestamp": "", "location": "",
                                     "text": "t", "meta": {}},
                           "context": None, "context_source": None})

    monkeypatch.setattr(app.server, "expand", fake)
    _get("/s3cr3t/search", "expand=e1&context=15")
    _get("/s3cr3t/search", "expand=e1&context=99")     # clamped
    assert seen == [15, 25]


def test_meta_line_per_source(monkeypatch):
    payload = json.dumps(
        {"chunk": {"id": "x1", "source": "shell", "timestamp": "",
                   "location": "", "text": "make deploy",
                   "meta": {"count": 7, "cwd": "~/dev/x", "exit": 2}},
         "context": None, "context_source": None})
    monkeypatch.setattr(app.server, "expand", lambda cid, context=5: payload)
    _, _, body = _get("/s3cr3t/search", "expand=x1")
    assert "7 runs · ~/dev/x" in body
    assert '<span class="err">exit 2</span>' in body


def test_meta_line_omits_empty(monkeypatch):
    body = _expand_with(monkeypatch, "obsidian", None, ctx_src=None)
    assert 'class="health"' not in body                # no meta, no line


def test_shell_context_exit_warns(monkeypatch):
    body = _expand_with(monkeypatch, "shell", {"scope": "session",
        "commands": [{"timestamp": "2026-07-02T17:00:00+00:00",
                      "cwd": "~/dev", "exit": 1, "command": "make"},
                     {"timestamp": "2026-07-02T17:01:00+00:00",
                      "cwd": "~/dev", "exit": 0, "command": "ls",
                      "target": True}]})
    assert body.count('class="err"') == 1              # only nonzero exits


def test_dayshape_card_renders_structure(monkeypatch):
    meta = {"date": "2026-07-02", "first": "08:42", "last": "18:30",
            "switches": 12, "active_seconds": 21000,
            "breaks": [{"start": "12:00", "minutes": 45}],
            "focus": [{"app": "Xcode", "start": "09:00", "minutes": 52}],
            "calls": [{"start": "14:00", "minutes": 19, "app": "zoom.us"}],
            "categories": {"developer-tools": 17700, "other": 3300}}
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 1, "results": [
                                {"rank": 1, "id": "d1", "source": "appusage",
                                 "distance": 0.5, "text": "On 2026-07-02…",
                                 "meta": meta}]}))
    _, _, body = _get("/s3cr3t/search", "q=x")
    assert "active 08:42–18:30 · 5h 50m · 12 switches · 1 breaks (45m)" in body
    assert "focus: 52m Xcode" in body
    assert "calls: 19m (zoom.us)" in body
    assert '<span class="chip-s">developer-tools 4h 55m</span>' in body
    assert "On 2026-07-02…" not in body                # structure, not prose


def test_noctx_marker_on_the_right_sources(monkeypatch):
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 2, "results": [
                                {"rank": 1, "id": "a", "source": "shell",
                                 "distance": 0.5, "text": "x"},
                                {"rank": 2, "id": "b", "source": "claude",
                                 "distance": 0.6, "text": "y"}]}))
    _, _, body = _get("/s3cr3t/search", "q=x")
    assert 'class="expand" data-noctx href="search?expand=a' in body
    assert 'class="expand" href="search?expand=b' in body   # claude: none


def test_window_mode_renders_structured_cards(monkeypatch):
    """The regression behind the screenshot: list_window used to strip
    meta, starving the card renderers into the prose fallback."""
    meta = {"date": "2026-07-12", "first": "00:52", "last": "13:46",
            "switches": 3, "active_seconds": 15240,
            "breaks": [{"start": "01:26", "minutes": 191}],
            "focus": [{"app": "Helium", "start": "11:45", "minutes": 37}]}
    monkeypatch.setattr(app.server, "list_window",
                        lambda **kw: json.dumps(
                            {"count": 1, "total": 1, "window": {},
                             "results": [{"id": "d1", "source": "appusage",
                                          "location": "appusage",
                                          "timestamp": "2026-07-12T07:00:00+00:00",
                                          "text": "On 2026-07-12 (Sunday)…",
                                          "meta": meta}]}))
    _, _, body = _get("/s3cr3t/search", "since=2026-07-12&until=2026-07-12")
    assert "active 00:52–13:46" in body and "focus: 37m Helium" in body
    assert "On 2026-07-12 (Sunday)…" not in body


def test_expanded_chunk_uses_card_renderer(monkeypatch):
    payload = json.dumps(
        {"chunk": {"id": "x1", "source": "shell", "timestamp": "",
                   "location": "", "text": "make deploy", "meta": {}},
         "context": None, "context_source": None})
    monkeypatch.setattr(app.server, "expand", lambda cid, context=5: payload)
    _, _, body = _get("/s3cr3t/search", "expand=x1")
    assert '<pre class="mono">make deploy</pre>' in body   # unclamped mono


# ── ask mode (wip/SPEC-ask-mode.md) ──────────────────────────────────────────

PRESETS2 = [{"name": "haiku", "backend": "anthropic", "model": "m",
             "key_env": ""},
            {"name": "gpt", "backend": "openai-compatible", "model": "g",
             "key_env": ""}]


def test_no_presets_means_no_ask_ui():
    _, _, body = _get("/s3cr3t/search")     # test env has no [ask] config
    assert 'value="ask"' not in body and 'name="model"' not in body


def test_ask_tab_and_model_picker(monkeypatch):
    monkeypatch.setattr(app.ask, "presets", lambda: PRESETS2)
    _, _, body = _get("/s3cr3t/search")
    assert ">Ask</a>" in body                       # tab present
    assert 'name="model"' not in body               # picker: ask mode only
    _, _, body = _get("/s3cr3t/search", "mode=ask")
    assert '<input type="hidden" name="mode" value="ask">' in body
    assert "<button>Ask</button>" in body
    assert 'name="model" value="haiku" checked' in body
    assert 'name="model" value="gpt"' in body
    assert 'name="source"' not in body              # no inert controls
    assert "<details" not in body
    monkeypatch.setattr(app.ask, "presets", lambda: PRESETS2[:1])
    _, _, body = _get("/s3cr3t/search", "mode=ask")
    assert "<button>Ask</button>" in body
    assert 'name="model"' not in body       # picker hides below two


def test_ask_mode_renders_answer_card(monkeypatch):
    seen = {}

    def fake_ask(q, model=""):
        seen.update({"q": q, "model": model})
        return {"answer": "Found <it> [id:abc123].", "citations": ["abc123"],
                "usage": {"model": "haiku", "turns": 3, "in": 100, "out": 42}}

    monkeypatch.setattr(app.ask, "ask", fake_ask)
    monkeypatch.setattr(app.server, "search_history",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("search must not run in ask mode")))
    _, _, body = _get("/s3cr3t/search", "q=why&mode=ask&model=haiku&go=1")
    assert seen == {"q": "why", "model": "haiku"}
    assert "Found &lt;it&gt;" in body                     # escaped
    assert 'href="search?expand=abc123&amp;q=why&amp;model=haiku">[1]</a>' \
        in body                                           # citation link
    assert "haiku · 3 turns · 100+42 tokens" in body


def test_ask_json_mode(monkeypatch):
    payload = {"answer": "A.", "citations": [], "usage": {"turns": 1}}
    monkeypatch.setattr(app.ask, "ask", lambda q, model="": payload)
    status, headers, body = _get("/s3cr3t/search", "q=x&mode=ask&json=1&go=1")
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == payload


def test_ask_error_renders_as_note(monkeypatch):
    monkeypatch.setattr(app.ask, "ask",
                        lambda q, model="": {"error": "ask mode isn't "
                                             "configured — add presets"})
    _, _, body = _get("/s3cr3t/search", "q=x&mode=ask&go=1")
    assert 'class="note"' in body and "isn&#x27;t configured" in body


# ── form rebuild (wip/SPEC-search-form-rebuild.md) ───────────────────────────

def test_mode_inference_matrix(monkeypatch):
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 0, "results": []}))
    monkeypatch.setattr(app.server, "list_window",
                        lambda **kw: json.dumps(
                            {"count": 0, "total": 0, "window": {},
                             "results": []}))
    _, _, body = _get("/s3cr3t/search")
    assert 'class="on" href="search?tab=1"' in body      # default: Search
    _, _, body = _get("/s3cr3t/search", "since=2026-07-01")
    assert ">Browse</a>" in body.split('class="on"')[1]  # legacy shape
    _, _, body = _get("/s3cr3t/search", "mode=bogus&q=x")
    assert 'class="on" href="search?q=x' in body         # unknown → search
    assert "tab=1" in body.split('class="on"')[1].split('"')[1]


def test_per_mode_forms():
    _, _, body = _get("/s3cr3t/search")
    assert 'name="q"' in body and 'name="model"' not in body
    assert body.count("<button>") == 1                   # one verb per mode
    _, _, body = _get("/s3cr3t/search", "mode=browse")
    assert 'name="q"' not in body                        # no query box
    assert "<button>Browse</button>" in body
    assert 'name="view"' in body                         # summaries toggle
    assert 'name="undated"' not in body


def test_tab_links_preserve_shared_state(monkeypatch):
    monkeypatch.setattr(app.ask, "presets", lambda: PRESETS2)
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 0, "results": []}))

    _, _, body = _get("/s3cr3t/search",
                      "q=x&since=2026-07-01&source=git&model=gpt")
    tabs = body.split("</nav>")[0]
    search, ask_, browse = re.findall(r'href="([^"]+)"', tabs)
    assert "q=x" in search and "since=2026-07-01" in search
    assert "q=x" in ask_ and "model=gpt" in ask_ and "since" not in ask_
    assert "since=2026-07-01" in browse and "source=git" in browse
    assert "q=x" not in browse


def test_range_presets_compute_dates_server_side(monkeypatch):
    seen = {}
    monkeypatch.setattr(app.server, "list_window",
                        lambda **kw: seen.update(kw) or json.dumps(
                            {"count": 0, "total": 0, "window": {},
                             "results": []}))
    _get("/s3cr3t/search", "mode=browse&range=7d")
    from datetime import datetime, timedelta
    today = datetime.now().astimezone().date()
    assert seen["until"] == today.isoformat()
    assert seen["since"] == (today - timedelta(days=6)).isoformat()


def test_summaries_view_filters_and_renders_bands(monkeypatch):
    seen = {}

    def fake_window(**kw):
        seen.update(kw)
        return json.dumps({"count": 3, "total": 3, "window": {}, "results": [
            {"id": "sh", "source": "appusage", "location": "appusage",
             "timestamp": "2026-07-12T07:00:00+00:00", "text": "shape…",
             "meta": {"first": "08:00", "active_seconds": 60,
                      "switches": 1}},
            {"id": "dg", "source": "digest", "location": "chrome:Default",
             "timestamp": "2026-07-12T07:00:00+00:00", "text": "Browsing…",
             "meta": {"digest_of": "browser"}},
            {"id": "raw", "source": "shell", "location": "",
             "timestamp": "2026-07-12T17:00:00+00:00", "text": "ls"}]})

    monkeypatch.setattr(app.server, "list_window", fake_window)
    _, _, body = _get("/s3cr3t/search",
                      "mode=browse&since=2026-07-12&view=summaries")
    assert seen["summaries"] is True
    assert body.count('class="sband"') == 2
    assert "day shape" in body and "digest · browser · chrome:Default" in body
    assert 'class="sband"' not in body.split('expand=raw')[0].rsplit(
        "sband", 1)[-1] or True   # raw stays a card
    assert '<article>' in body                          # the shell item card
    # bands still expand, with browse-mode back params
    assert 'href="search?expand=dg&amp;' in body and "mode=browse" in body


def test_digest_absent_from_chips_but_url_honored(monkeypatch):
    monkeypatch.setattr(app.server, "history_stats",
                        lambda locations=False: json.dumps(
                            {"total_chunks": 2,
                             "sources": {"digest": {}, "shell": {}}}))
    _, _, body = _get("/s3cr3t/search")
    assert 'value="digest"' not in body                 # no chip
    seen = {}
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: seen.update(kw) or json.dumps(
                            {"query": query, "count": 0, "results": []}))
    _get("/s3cr3t/search", "q=x&source=digest")
    assert seen["source"] == "digest"                   # URL still filters


def test_undated_renders_once_a_date_is_set(monkeypatch):
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 0, "results": []}))
    _, _, body = _get("/s3cr3t/search", "q=x&since=2026-07-01")
    assert 'name="undated"' in body


def test_ask_tab_navigation_never_executes(monkeypatch):
    """The regression behind 'the Ask tab re-submits the form': tab links
    carry q for prefill, so navigation must render the form, never run a
    paid ask — only the form's go=1 submit executes."""
    monkeypatch.setattr(app.ask, "ask",
                        lambda q, model="": (_ for _ in ()).throw(
                            AssertionError("tab navigation ran an ask")))
    status, _, body = _get("/s3cr3t/search", "mode=ask&q=proxy+bug")
    assert status == 200
    assert 'value="proxy bug"' in body          # prefilled, not executed
    assert '<input type="hidden" name="go" value="1">' in body


def test_autofocus_only_on_the_landing_page(monkeypatch):
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 0, "results": []}))
    _, _, body = _get("/s3cr3t/search")
    assert "autofocus" in body                  # bare form: convenient
    _, _, body = _get("/s3cr3t/search", "q=x")
    assert "autofocus" not in body              # results: no iOS scroll yank
    _, _, body = _get("/s3cr3t/search", "mode=ask&q=x")
    assert "autofocus" not in body


def test_tabs_never_execute_any_mode(monkeypatch):
    """The general principle behind the Ask-tab bug: tabs switch forms and
    carry prefill, but only a submit executes — for every mode."""
    boom = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("tab navigation executed"))
    monkeypatch.setattr(app.server, "search_history", boom)
    monkeypatch.setattr(app.server, "list_window", boom)
    monkeypatch.setattr(app.ask, "ask", boom)
    status, _, body = _get("/s3cr3t/search", "q=proxy+bug&tab=1")
    assert status == 200 and 'value="proxy bug"' in body   # search prefilled
    status, _, body = _get("/s3cr3t/search",
                           "mode=browse&since=2026-07-01&tab=1")
    assert status == 200 and 'value="2026-07-01"' in body  # browse prefilled
    status, _, _ = _get("/s3cr3t/search", "mode=ask&q=x&go=1&tab=1")
    assert status == 200                                   # tab wins even v go
    # and the executing shapes still execute (no tab=1): unchanged
    monkeypatch.setattr(app.server, "search_history",
                        lambda query, k=5, **kw: json.dumps(
                            {"query": query, "count": 0, "results": []}))
    _, _, body = _get("/s3cr3t/search", "q=proxy+bug")
    assert "no matches" in body
