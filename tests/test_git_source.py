"""Git source: repo discovery, rebase-stable ids, message collapse, stash
exclusion, author handling. `_git` is monkeypatched with fabricated output."""
import os
import pytest
from sources import git as gitsrc

F = "\x01"

def entry(sha, date, subject, body=""):
    return f"{sha}{F}{date}{F}{subject}{F}{body}"

def fake_git(log_map, emails=None, calls=None):
    def _fake(repo, *args):
        if calls is not None:
            calls.append((repo, args))
        if args[0] == "config":
            email = (emails or {}).get(repo, "t@example.com")
            if email is None:
                raise RuntimeError("no email configured")
            return email + "\n"
        if args[0] == "log":
            return "\0".join(log_map.get(repo, []))
        raise AssertionError(f"unexpected git call: {args}")
    return _fake

@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "r"
    (r / ".git").mkdir(parents=True)
    monkeypatch.setattr(gitsrc, "_roots", lambda: [str(r)])
    return str(r)

def test_message_collapse_and_rebase_stable_ids(repo, monkeypatch):
    original = entry("a" * 40, "2026-07-01T10:00:00-07:00", "fix thing")
    rebased = entry("b" * 40, "2026-07-02T10:00:00-07:00", "fix thing")
    other = entry("c" * 40, "2026-07-01T09:00:00-07:00", "other msg")
    monkeypatch.setattr(gitsrc, "_git", fake_git({repo: [original, rebased, other]}))
    chunks = list(gitsrc.iter_chunks())
    assert len(chunks) == 2                          # copies collapsed
    cid, text, rec = next(c for c in chunks if c[1] == "fix thing")
    assert rec["meta"]["count"] == 2
    assert rec["meta"]["sha"] == "b" * 40            # latest copy wins
    assert rec["timestamp"] == "2026-07-02T17:00:00+00:00"   # %aI -> UTC
    assert rec["location"].startswith("r@bbbbbbbb")

    # id must not change when the sha does (that's the whole point)
    monkeypatch.setattr(gitsrc, "_git", fake_git({repo: [original]}))
    (cid2, _, _), = gitsrc.iter_chunks()
    assert cid2 == cid

def test_stash_excluded_and_missing_email_skips(repo, tmp_path, monkeypatch, capsys):
    calls = []
    r2 = tmp_path / "r2"
    (r2 / ".git").mkdir(parents=True)
    monkeypatch.setattr(gitsrc, "_roots", lambda: [repo, str(r2)])
    monkeypatch.setattr(gitsrc, "_git", fake_git(
        {repo: [entry("a" * 40, "2026-07-01T10:00:00+00:00", "msg")]},
        emails={str(r2): None}, calls=calls))
    chunks = list(gitsrc.iter_chunks())
    assert len(chunks) == 1
    log_args = next(a for _, a in calls if a[0] == "log")
    assert "--exclude=refs/stash" in log_args and "--no-merges" in log_args
    assert "no user.email" in capsys.readouterr().err

def test_find_repos_bounds_and_worktrees(tmp_path):
    (tmp_path / "a" / ".git").mkdir(parents=True)                 # normal repo
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / ".git").write_text("gitdir: elsewhere")     # worktree file
    (tmp_path / "node_modules" / "c" / ".git").mkdir(parents=True)  # skipped dir
    (tmp_path / ".hidden" / "d" / ".git").mkdir(parents=True)       # hidden skipped
    (tmp_path / "deep" / "x" / "y" / "z" / ".git").mkdir(parents=True)  # too deep
    found = {os.path.basename(p) for p in gitsrc._find_repos(str(tmp_path))}
    assert found == {"a", "b"}
