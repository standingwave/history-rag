"""hist CLI: flag-to-arguments mapping for all four subcommands, endpoint
resolution precedence (env beats lpass; lpass never forked when env is set),
tool-error -> exit 1, human vs --json rendering. No network — urlopen is
faked at the module boundary."""
import importlib.util, json, pathlib, subprocess, urllib.request

import pytest

_spec = importlib.util.spec_from_file_location(
    "hist", pathlib.Path(__file__).resolve().parent.parent
    / "tools" / "hist.py")
hist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hist)

URL = "https://host.example/s3cr3t/mcp"


class _FakeResponse:
    def __init__(self, envelope):
        self._data = json.dumps(envelope).encode()

    def read(self, *a):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def transport(monkeypatch):
    """Fake urlopen: records the request, returns a canned tool result."""
    calls = {}

    def set_result(result):
        calls["result"] = result

    def fake_urlopen(req, timeout=None):
        calls["url"] = req.full_url
        calls["body"] = json.loads(req.data)
        envelope = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": json.dumps(calls["result"])}]}}
        return _FakeResponse(envelope)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("HISTORY_RAG_URL", URL)
    calls["set_result"] = set_result
    set_result({"results": []})
    return calls


def test_search_flag_mapping(transport):
    hist.main(["search", "proxy bug", "-k", "3", "--source", "claude",
               "--since", "2026-07-01", "--include-undated",
               "--max-distance", "1.2"])
    assert transport["url"] == URL
    body = transport["body"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "search_history"
    assert body["params"]["arguments"] == {
        "query": "proxy bug", "k": 3, "source": "claude",
        "since": "2026-07-01", "include_undated": True, "max_distance": 1.2}


def test_search_defaults_stay_minimal(transport):
    hist.main(["search", "q"])
    assert transport["body"]["params"]["arguments"] == {"query": "q", "k": 5}


def test_window_flag_mapping(transport):
    transport["set_result"]({"count": 0, "total": 0, "results": []})
    hist.main(["window", "--since", "2026-07-01", "--group-by", "day",
               "--limit", "10", "--offset", "20"])
    assert transport["body"]["params"]["name"] == "list_window"
    assert transport["body"]["params"]["arguments"] == {
        "since": "2026-07-01", "group_by": "day", "limit": 10, "offset": 20}


def test_expand_flag_mapping(transport):
    transport["set_result"]({"chunk": {"id": "abc", "source": "shell",
                                       "text": "ls"}, "context": None,
                             "context_source": None})
    hist.main(["expand", "abc", "--context", "10"])
    assert transport["body"]["params"]["name"] == "expand"
    assert transport["body"]["params"]["arguments"] == {"id": "abc",
                                                        "context": 10}


def test_stats_flag_mapping(transport):
    transport["set_result"]({"total_chunks": 0, "sources": {}})
    hist.main(["stats", "--locations"])
    assert transport["body"]["params"]["name"] == "history_stats"
    assert transport["body"]["params"]["arguments"] == {"locations": True}
    hist.main(["stats"])
    assert transport["body"]["params"]["arguments"] == {}


def test_env_beats_lpass(transport, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("lpass must not be forked when env is set")
    monkeypatch.setattr(subprocess, "run", boom)
    hist.main(["search", "q"])
    assert transport["url"] == URL


def test_lpass_fallback_appends_mcp(transport, monkeypatch):
    monkeypatch.delenv("HISTORY_RAG_URL")
    forked = []

    def fake_run(cmd, **kw):
        forked.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="https://host.example/other\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    hist.main(["search", "q"])
    assert forked[0][:2] == ["lpass", "show"]
    assert transport["url"] == "https://host.example/other/mcp"


def test_no_endpoint_is_a_clear_error(transport, monkeypatch, capsys):
    monkeypatch.delenv("HISTORY_RAG_URL")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(SystemExit) as e:
        hist.main(["search", "q"])
    assert "HISTORY_RAG_URL" in str(e.value.code)


def test_tool_error_exits_1(transport, capsys):
    transport["set_result"]({"error": "no chunk with id 'zzz'"})
    with pytest.raises(SystemExit) as e:
        hist.main(["expand", "zzz"])
    assert e.value.code == 1
    assert "no chunk" in capsys.readouterr().err


def test_human_vs_json_rendering(transport, capsys):
    result = {"query": "q", "count": 1, "results": [
        {"rank": 1, "id": "abc123", "source": "browser", "distance": 0.8123,
         "text": "Some page\nabout things", "timestamp": "2026-07-02T10:00:00+00:00",
         "location": "chrome:Default"}]}
    transport["set_result"](result)

    hist.main(["search", "q"])
    out = capsys.readouterr().out
    assert " 1. [browser] 2026-07-02  chrome:Default" in out
    assert "Some page about things" in out          # newlines flattened
    assert "id abc123" in out

    hist.main(["search", "q", "--json"])
    assert json.loads(capsys.readouterr().out) == result
