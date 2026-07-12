"""Ask mode: preset availability by key presence, tool defs derived from
the MCP registry, both provider adapters' wire shapes, the loop (tool
round-trip, max_turns cap, provider errors, result truncation), and
citation extraction."""
import copy, json, types

import pytest

import ask, config, server

PRESETS = [
    {"name": "m1", "backend": "openai-compatible",
     "base_url": "https://prov.example/v1", "model": "m-one",
     "key_env": "ASK_K_ONE"},
    {"name": "m2", "backend": "anthropic", "model": "m-two",
     "key_env": "ASK_K_TWO"},
    {"name": "local", "backend": "openai-compatible",
     "base_url": "http://localhost:11434/v1", "model": "qwen"},
]


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setitem(config._FILE, "ask", {"models": PRESETS})
    monkeypatch.setenv("ASK_K_ONE", "sk-one")
    monkeypatch.setenv("ASK_K_TWO", "sk-two")


def fake_provider(monkeypatch, responses):
    """Scripted requests.post: records every call, replays `responses`."""
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        # deep-copy: the adapters keep mutating their message lists
        calls.append({"url": url, "headers": headers or {},
                      "json": copy.deepcopy(json)})
        body = responses[min(len(calls), len(responses)) - 1]
        return types.SimpleNamespace(status_code=200, text="",
                                     json=lambda: body)

    monkeypatch.setattr(ask.requests, "post", post)
    return calls


def test_presets_filter_on_key_presence(cfg, monkeypatch):
    assert [p["name"] for p in ask.presets()] == ["m1", "m2", "local"]
    monkeypatch.delenv("ASK_K_TWO")
    assert [p["name"] for p in ask.presets()] == ["m1", "local"]  # keyless stays


def test_tool_defs_derive_from_registry():
    defs = {t["name"]: t for t in ask.tool_defs()}
    assert set(defs) == {"search_history", "history_stats", "list_window",
                         "expand"}
    assert "query" in defs["search_history"]["schema"]["properties"]
    assert defs["expand"]["description"]


def test_openai_roundtrip(cfg, monkeypatch):
    searched = {}

    def fake_search(**kw):
        searched.update(kw)
        return json.dumps({"query": kw.get("query"), "count": 0,
                           "results": []})

    monkeypatch.setattr(server, "search_history", fake_search)
    calls = fake_provider(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "search_history",
                                         "arguments": '{"query":"x","k":2}'}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"role": "assistant",
                                  "content": "Answer [id:abc]."}}],
         "usage": {"prompt_tokens": 30, "completion_tokens": 9}},
    ])
    out = ask.ask("what was x?", "m1")
    assert out["answer"] == "Answer [id:abc]."
    assert out["citations"] == ["abc"]
    assert out["usage"] == {"in": 40, "out": 14, "turns": 2, "model": "m1"}
    assert searched == {"query": "x", "k": 2}
    first, second = calls
    assert first["url"] == "https://prov.example/v1/chat/completions"
    assert first["headers"]["Authorization"] == "Bearer sk-one"
    assert first["json"]["tools"][0]["type"] == "function"
    assert first["json"]["messages"][0]["role"] == "system"
    assert first["json"]["max_tokens"] == 2048    # gateways credit-check
    # against the model max when unbounded
    tool_msg = second["json"]["messages"][-1]
    assert tool_msg == {"role": "tool", "tool_call_id": "c1",
                        "content": searched and json.dumps(
                            {"query": "x", "count": 0, "results": []})}


def test_anthropic_roundtrip(cfg, monkeypatch):
    expanded = {}

    def fake_expand(**kw):
        expanded.update(kw)
        return json.dumps({"chunk": {"id": kw.get("id")}, "context": None,
                           "context_source": None})

    monkeypatch.setattr(server, "expand", fake_expand)
    calls = fake_provider(monkeypatch, [
        {"content": [{"type": "tool_use", "id": "t1", "name": "expand",
                      "input": {"id": "z9"}}],
         "usage": {"input_tokens": 7, "output_tokens": 3}},
        {"content": [{"type": "text", "text": "Done [id:z9]"}],
         "usage": {"input_tokens": 20, "output_tokens": 6}},
    ])
    out = ask.ask("read z9", "m2")
    assert out["answer"] == "Done [id:z9]" and out["citations"] == ["z9"]
    assert expanded == {"id": "z9"}
    first, second = calls
    assert first["url"] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["x-api-key"] == "sk-two"
    assert "input_schema" in first["json"]["tools"][0]
    assert first["json"]["system"]
    result_msg = second["json"]["messages"][-1]
    assert result_msg["role"] == "user"
    assert result_msg["content"][0]["type"] == "tool_result"
    assert result_msg["content"][0]["tool_use_id"] == "t1"


def test_max_turns_cap(cfg, monkeypatch):
    monkeypatch.setenv("CLAUDE_RAG_ASK_MAX_TURNS", "2")
    monkeypatch.setattr(server, "history_stats",
                        lambda **kw: json.dumps({"total_chunks": 0}))
    fake_provider(monkeypatch, [
        {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c", "type": "function",
                            "function": {"name": "history_stats",
                                         "arguments": "{}"}}]}}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}])
    out = ask.ask("loop forever", "m1")
    assert out["note"] == "stopped at max_turns"
    assert out["usage"]["turns"] == 2


def test_unconfigured_and_unknown_preset(cfg, monkeypatch):
    assert "unknown model preset" in ask.ask("q", "nope")["error"]
    assert "m1" in ask.ask("q", "nope")["error"]      # names the options
    monkeypatch.setitem(config._FILE, "ask", {})
    assert "isn't configured" in ask.ask("q")["error"]


def test_provider_error_maps_to_message(cfg, monkeypatch):
    monkeypatch.setattr(ask.requests, "post",
                        lambda *a, **k: types.SimpleNamespace(
                            status_code=401, text="bad key\nmore",
                            json=lambda: {}))
    out = ask.ask("q", "m1")
    assert out["error"] == "provider error 401: bad key"


def test_tool_result_truncation(monkeypatch):
    monkeypatch.setattr(server, "history_stats",
                        lambda **kw: "x" * (ask.TOOL_RESULT_MAX + 500))
    out = ask.run_tool("history_stats", {})
    assert out.endswith("… [truncated]")
    assert len(out) == ask.TOOL_RESULT_MAX + len("… [truncated]")


def test_run_tool_rejects_unknown_and_survives_errors(monkeypatch):
    assert "unknown tool" in ask.run_tool("os_system", {"cmd": "rm"})
    monkeypatch.setattr(server, "expand",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("db")))
    assert "expand failed" in ask.run_tool("expand", {"id": "x"})


def test_citations_dedupe_in_order():
    assert ask.citations("a [id:one] b [id:two] c [id:one]") == ["one", "two"]
    assert ask.citations("") == []


def test_presets_from_env_json(monkeypatch):
    monkeypatch.setenv("CLAUDE_RAG_ASK_MODELS", json.dumps(
        [{"name": "env-model", "backend": "anthropic", "model": "m",
          "key_env": ""}]))
    assert [p["name"] for p in ask.presets()] == ["env-model"]
    monkeypatch.setenv("CLAUDE_RAG_ASK_MODELS", "not json")
    assert ask.presets() == []
