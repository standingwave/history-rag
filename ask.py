"""Ask mode: a model works the history tools in-process and synthesizes
an answer (wip/SPEC-ask-mode.md). Provider-agnostic behind two adapters —
"openai-compatible" (OpenAI, OpenRouter, Groq, Ollama's /v1, …) and
"anthropic" — selected per named preset in [ask.models]; the client only
ever picks a preset name, so no query param can point the loop (and a
key) at an arbitrary endpoint.

Raw HTTP via requests, no provider SDKs — the embed-backend pattern.
Tools are read-only, so injected instructions in indexed content can at
worst produce a bad answer.
"""
import json, os, re
from datetime import datetime

import requests

import config
import server

TOOL_RESULT_MAX = 20_000     # chars per tool result fed back to the model
PROVIDER_TIMEOUT = 90        # seconds per provider call

CITE_RE = re.compile(r"\[id:([^\]\s]+)\]")


class AskError(RuntimeError):
    pass


def presets() -> list:
    """[ask.models] entries whose key is actually present (an empty
    key_env means keyless — always available). On the Lambda there is no
    TOML, so the list may arrive as JSON in CLAUDE_RAG_ASK_MODELS."""
    raw = config.get("ask", "models", "CLAUDE_RAG_ASK_MODELS", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out = []
    for m in raw or []:
        if not m.get("name") or not m.get("model"):
            continue
        key_env = m.get("key_env", "")
        key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not key:
            continue
        out.append({**m, "_key": key})
    return out


def tool_defs() -> list:
    """Neutral tool definitions derived from the MCP registry — the
    docstrings already teach the disclosure ladder, and derived schemas
    can't drift from the tools."""
    return [{"name": t.name, "description": t.description,
             "schema": t.parameters}
            for t in server.mcp._tool_manager.list_tools()]


def run_tool(name: str, args: dict) -> str:
    fn = getattr(server, name, None)
    if fn is None or not any(t["name"] == name for t in tool_defs()):
        return json.dumps({"error": f"unknown tool {name!r}"})
    try:
        result = fn(**(args or {}))
    except Exception as e:
        return json.dumps({"error": f"{name} failed: {e}"})
    if len(result) > TOOL_RESULT_MAX:
        result = result[:TOOL_RESULT_MAX] + "… [truncated]"
    return result


def _system_prompt() -> str:
    now = datetime.now().astimezone()
    return (
        "You answer questions from the user's own indexed history — their "
        "Claude Code sessions, shell commands, browsing, git commits, "
        "notes, calendar, and app usage — using the provided tools.\n"
        f"Now: {now.isoformat(timespec='minutes')} "
        f"({now.tzname()}). Resolve relative dates against that.\n"
        "Work the disclosure ladder: history_stats to orient when unsure "
        "what's indexed; search_history / list_window to find (read digest "
        "chunks first for day/week questions); expand to read a hit in "
        "full before leaning on it.\n"
        "Cite the chunks your answer rests on inline as [id:<chunk id>]. "
        "Answer concisely in plain text. If the history doesn't contain "
        "the answer, say so plainly."
    )


def _http(url: str, headers: dict, payload: dict) -> dict:
    try:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=PROVIDER_TIMEOUT)
    except requests.RequestException as e:
        raise AskError(f"provider unreachable: {e}") from e
    if r.status_code != 200:
        first = (r.text or "").strip().splitlines()
        raise AskError(f"provider error {r.status_code}"
                       + (f": {first[0][:200]}" if first else ""))
    return r.json()


class _OpenAI:
    """Chat Completions with tools — the shape most providers speak."""

    def __init__(self, preset):
        self.preset = preset
        base = (preset.get("base_url") or "https://api.openai.com/v1")
        self.url = base.rstrip("/") + "/chat/completions"

    def start(self, system, question):
        self.messages = [{"role": "system", "content": system},
                         {"role": "user", "content": question}]

    def step(self, tools):
        headers = {}
        if self.preset["_key"]:
            headers["Authorization"] = f"Bearer {self.preset['_key']}"
        data = _http(self.url, headers, {
            "model": self.preset["model"], "messages": self.messages,
            # bound the output like the Anthropic adapter: gateways
            # (OpenRouter) credit-check against the model max otherwise
            "max_tokens": int(self.preset.get("max_tokens", 2048)),
            "tools": [{"type": "function",
                       "function": {"name": t["name"],
                                    "description": t["description"],
                                    "parameters": t["schema"]}}
                      for t in tools]})
        msg = data["choices"][0]["message"]
        self.messages.append(msg)
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": c["id"], "name": c["function"]["name"],
                          "args": args})
        u = data.get("usage") or {}
        return {"text": msg.get("content") or "", "tool_calls": calls,
                "usage": {"in": u.get("prompt_tokens", 0),
                          "out": u.get("completion_tokens", 0)}}

    def add_results(self, results):
        for r in results:
            self.messages.append({"role": "tool", "tool_call_id": r["id"],
                                  "content": r["result"]})


class _Anthropic:
    """The Messages API — different tool-call and result shapes."""

    def __init__(self, preset):
        self.preset = preset
        base = preset.get("base_url") or "https://api.anthropic.com"
        self.url = base.rstrip("/") + "/v1/messages"

    def start(self, system, question):
        self.system = system
        self.messages = [{"role": "user", "content": question}]

    def step(self, tools):
        data = _http(self.url, {"x-api-key": self.preset["_key"],
                                "anthropic-version": "2023-06-01"}, {
            "model": self.preset["model"],
            "max_tokens": int(self.preset.get("max_tokens", 2048)),
            "system": self.system, "messages": self.messages,
            "tools": [{"name": t["name"], "description": t["description"],
                       "input_schema": t["schema"]} for t in tools]})
        self.messages.append({"role": "assistant", "content": data["content"]})
        text, calls = "", []
        for block in data["content"]:
            if block["type"] == "text":
                text += block["text"]
            elif block["type"] == "tool_use":
                calls.append({"id": block["id"], "name": block["name"],
                              "args": block.get("input") or {}})
        u = data.get("usage") or {}
        return {"text": text, "tool_calls": calls,
                "usage": {"in": u.get("input_tokens", 0),
                          "out": u.get("output_tokens", 0)}}

    def add_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"],
             "content": r["result"]} for r in results]})


_ADAPTERS = {"openai-compatible": _OpenAI, "anthropic": _Anthropic}


def citations(text: str) -> list:
    seen, out = set(), []
    for cid in CITE_RE.findall(text or ""):
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def ask(question: str, preset_name: str = "") -> dict:
    """One question, one answer: {answer, citations, usage} or {error}."""
    ps = presets()
    if not ps:
        return {"error": "ask mode isn't configured — add [[ask.models]] "
                         "presets and set their key env vars"}
    if preset_name:
        preset = next((p for p in ps if p["name"] == preset_name), None)
        if preset is None:
            return {"error": f"unknown model preset {preset_name!r}; "
                    "available: " + ", ".join(p["name"] for p in ps)}
    else:
        preset = ps[0]
    backend = preset.get("backend", "openai-compatible")
    if backend not in _ADAPTERS:
        return {"error": f"unknown backend {backend!r} in preset "
                f"{preset['name']!r}"}
    adapter = _ADAPTERS[backend](preset)
    max_turns = int(config.get("ask", "max_turns",
                               "CLAUDE_RAG_ASK_MAX_TURNS", 8))
    tools = tool_defs()
    adapter.start(_system_prompt(), question)
    usage = {"in": 0, "out": 0, "turns": 0, "model": preset["name"]}
    text = ""
    for _ in range(max_turns):
        usage["turns"] += 1
        try:
            step = adapter.step(tools)
        except AskError as e:
            return {"error": str(e), "usage": usage}
        usage["in"] += step["usage"]["in"]
        usage["out"] += step["usage"]["out"]
        text = step["text"] or text
        if not step["tool_calls"]:
            return {"answer": step["text"], "citations":
                    citations(step["text"]), "usage": usage}
        adapter.add_results(
            [{"id": c["id"], "name": c["name"],
              "result": run_tool(c["name"], c["args"])}
             for c in step["tool_calls"]])
    return {"answer": text, "note": "stopped at max_turns",
            "citations": citations(text), "usage": usage}
