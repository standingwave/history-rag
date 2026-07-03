"""Shell dedup: atuin supersedes live histfiles, archived files always count,
latest run wins location/cwd/exit."""
import os
from sources import shell as sh

def test_atuin_supersedes_live_but_not_archived(monkeypatch, tmp_path):
    live = tmp_path / "zsh_history"
    live.write_text("git status\ngit status\nrare command here\n")
    arch = tmp_path / "archived_history"
    arch.write_text("git status\n")
    monkeypatch.setattr(sh, "_history_files",
                        lambda: ([str(live)], [str(arch)]))
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter([
        (1751000000.0, "git status", "/Users/u/dev/x", 0),
        (1751000100.0, "git status", "/Users/u/dev/y", 1),
    ]))
    chunks = {text: rec for _, text, rec in sh.iter_chunks()}

    gs = chunks["git status"]
    # 2 atuin runs + 1 archived; the 2 live-histfile occurrences are skipped
    assert gs["meta"]["count"] == 3
    assert gs["meta"]["cwd"] == "/Users/u/dev/y"     # latest run wins
    assert gs["meta"]["exit"] == 1
    assert gs["location"] == "/Users/u/dev/y"
    assert gs["timestamp"] == sh._iso(1751000100.0)

    rare = chunks["rare command here"]               # live-only command survives
    assert rare["meta"]["count"] == 1
    assert rare["location"] == os.path.basename(str(live))
    assert rare["timestamp"] == ""                   # plain file: undated
    assert "cwd" not in rare["meta"]

def test_histfile_epoch_never_displaces_atuin_cwd_location(monkeypatch, tmp_path):
    arch = tmp_path / "old"
    arch.write_text(": 1799999999:0;git status\n")   # future-dated archive entry
    monkeypatch.setattr(sh, "_history_files", lambda: ([], [str(arch)]))
    monkeypatch.setattr(sh, "_read_atuin", lambda: iter([
        (1751000000.0, "git status", "/Users/u/dev/x", 0),
    ]))
    (_, _, rec), = sh.iter_chunks()
    assert rec["timestamp"] == sh._iso(1799999999)   # newer epoch wins the date
    assert rec["location"] == "/Users/u/dev/x"       # but cwd-location is kept
