"""Shell chunking: dated atuin runs group into cwd+gap working sessions;
histfile and imported commands stay flat and deduped; atuin supersedes live
histfiles but not archived ones."""
import os
from sources import shell as sh

T = 1751000000.0

def _no_files(monkeypatch):
    monkeypatch.setattr(sh, "_history_files", lambda: ([], []))

def test_sessions_group_by_cwd_and_gap(monkeypatch):
    _no_files(monkeypatch)
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter([
        (T, "pytest -q", "/u/dev/x", 1),
        (T + 60, "vim src/filter.py", "/u/dev/x", 0),
        (T + 120, "pytest -q", "/u/dev/x", 0),
        (T + 120 + sh.SESSION_GAP + 1, "git push", "/u/dev/x", 0),  # past gap
        (T + 90, "make docs", "/u/dev/y", 0),                       # other cwd
    ]))
    chunks = list(sh.iter_chunks())
    assert all(r["meta"]["kind"] == "session" for _, _, r in chunks)
    xs = [c for c in chunks if c[2]["location"] == "/u/dev/x"]
    assert len(xs) == 2                       # idle boundary split the cwd
    _, text, r = next(c for c in xs if c[2]["meta"]["commands"] == 3)
    assert text.splitlines()[0].startswith("Shell session in /u/dev/x — ")
    assert "(3 commands)" in text.splitlines()[0]
    assert "pytest -q (exit 1)" in text       # failure annotated
    assert text.splitlines()[3] == "pytest -q"          # success bare
    assert r["timestamp"] == sh._iso(T)
    assert r["meta"]["start"] == sh._iso(T)
    assert r["meta"]["end"] == sh._iso(T + 120)
    assert r["meta"]["cwd"] == "/u/dev/x"
    _, text2, r2 = next(c for c in xs if c[2]["meta"]["commands"] == 1)
    assert "git push" in text2 and "(1 command)" in text2

def test_open_session_grows_in_place(monkeypatch):
    _no_files(monkeypatch)
    runs = [(T, "pytest -q", "/u/dev/x", 0)]
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter(runs))
    (id1, text1, _), = sh.iter_chunks()
    runs.append((T + 300, "git commit -m fix", "/u/dev/x", 0))
    (id2, text2, _), = sh.iter_chunks()
    assert id1 == id2                         # same session start -> same id
    assert "git commit -m fix" in text2 and len(text2) > len(text1)

def test_session_collapses_repeats_and_elides_middle(monkeypatch):
    _no_files(monkeypatch)
    many = [(T + i, f"make target{i}", "/u/dev/x", 0) for i in range(50)]
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter(
        [(T - 20, "make lint", "/u/dev/y", 0),
         (T - 10, "make lint", "/u/dev/y", 0)] + many))
    chunks = {r["location"]: (text, r) for _, text, r in sh.iter_chunks()}
    text, r = chunks["/u/dev/y"]
    assert "make lint (×2)" in text and r["meta"]["commands"] == 2
    text, _ = chunks["/u/dev/x"]
    lines = text.splitlines()
    assert len(lines) == sh.SESSION_MAX_LINES + 1       # header + capped body
    assert any(l.startswith("… +") and l.endswith("more") for l in lines)

def test_imported_rows_are_undated_flat(monkeypatch):
    _no_files(monkeypatch)
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter([
        (T, "brew install ollama", "unknown", -1),
        (T + 1, "brew install ollama", "unknown", -1),
    ]))
    (cid, text, r), = sh.iter_chunks()
    assert text == "brew install ollama"
    assert r["timestamp"] == "" and r["location"] == ""
    assert r["meta"] == {"count": 2}

def test_atuin_supersedes_live_but_not_archived(monkeypatch, tmp_path):
    live = tmp_path / "zsh_history"
    live.write_text("git status --short\nrare command here\n")
    arch = tmp_path / "archived_history"
    arch.write_text("git status --short\n")
    monkeypatch.setattr(sh, "_history_files",
                        lambda: ([str(live)], [str(arch)]))
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter([
        (T, "git status --short", "/u/dev/x", 0),
    ]))
    chunks = {text: r for _, text, r in sh.iter_chunks()}
    # the atuin run became a session; the live histfile copy was skipped;
    # the archived copy still counts as its own flat chunk
    session = next(r for t, r in chunks.items() if t.startswith("Shell session"))
    assert session["meta"]["commands"] == 1
    assert chunks["git status --short"]["meta"] == {"count": 1}
    assert chunks["git status --short"]["location"] == "archived_history"
    assert chunks["rare command here"]["meta"] == {"count": 1}

def test_iter_dated_runs_skips_imported_rows(monkeypatch):
    _no_files(monkeypatch)
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter([
        (T, "brew install ollama", "unknown", -1),
        (T + 5, "pytest -q", "/u/dev/x", 0),
    ]))
    assert list(sh.iter_dated_runs(0)) == [(T + 5, "pytest -q", "/u/dev/x")]

def test_session_commands_reads_atuin_window(monkeypatch, tmp_path):
    import sqlite3
    p = tmp_path / "atuin.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE history(id INTEGER PRIMARY KEY, timestamp INTEGER,"
               " command TEXT, cwd TEXT, exit INTEGER, session TEXT,"
               " deleted_at TEXT)")
    ns = lambda s: int(s * 1e9)
    for t, cmd, cwd in [
            (T, "pytest -q", "/u/dev/x"),
            (T + 30, "export API_KEY=sk-abc123def456", "/u/dev/x"),  # secret
            (T + 45, "make docs", "/u/dev/y"),                # other cwd
            (T + 60, "git commit -m fix", "/u/dev/x"),
            (T + 7200, "later run", "/u/dev/x")]:             # outside window
        db.execute("INSERT INTO history(timestamp, command, cwd, exit, session)"
                   " VALUES (?,?,?,0,'s')", (ns(t), cmd, cwd))
    db.commit(); db.close()
    monkeypatch.setenv("CLAUDE_RAG_ATUIN_DB", str(p))
    out = sh.session_commands({"cwd": "/u/dev/x", "start": sh._iso(T),
                               "end": sh._iso(T + 60)})
    assert out["cwd"] == "/u/dev/x"
    assert [c["command"] for c in out["commands"]] == \
        ["pytest -q", "git commit -m fix"]

def test_session_commands_none_without_atuin():
    assert sh.session_commands({"cwd": "/u/dev/x", "start": sh._iso(T),
                                "end": sh._iso(T + 60)}) is None
