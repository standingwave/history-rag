"""Tasks source: block grammar, one-chunk-per-task identity across carried
days, completion-day timestamps, routine gating, secret drop, and the
expand() day view (live and index-backed)."""
import json
from datetime import datetime, timezone
import pytest
from sources import tasks
from tests.helpers import run_index, open_db

D1 = """- [ ] treat hoya for mealybugs (alcohol wipe)
\t![[2026-08-26 hoya-stems.png]]
\t- [ ] isolate from other plants
\t- [x] prune dead leaves
- [x] send Briar an email
- [ ] workout
- [ ] export API_KEY=sk-abc123def456 to the server

## Routine
- [x] brush teeth and shower
"""
D2 = """- [ ] treat hoya for mealybugs (alcohol wipe)
\t![[2026-08-26 hoya-stems.png]]
\t- [x] isolate from other plants
\t- [x] prune dead leaves
- [x] workout
- [ ] work on a personal mobile dashboard

## Routine
- [ ] brush teeth and shower
"""

@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "Documents"
    v.mkdir()
    (v / "2026-08-27.md").write_text(D1)
    (v / "2026-08-28.md").write_text(D2)
    (v / "Not a daily note.md").write_text("- [ ] a task in a project note\n")
    (v / "1-Projects").mkdir()
    (v / "1-Projects" / "2026-08-28.md").write_text("- [ ] nested daily lookalike\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    monkeypatch.delenv("CLAUDE_RAG_TASKS_ROUTINE", raising=False)
    return v

# ── grammar ──────────────────────────────────────────────────────────────────

def test_parse_note_blocks_sections_and_attachments():
    ts = tasks.parse_note(D1)
    assert [t["text"][:10] for t in ts] == ["treat hoya", "send Briar", "workout",
                                            "export API", "brush teet"]
    hoya = ts[0]
    assert hoya["attachments"] == ["2026-08-26 hoya-stems.png"]
    assert [(s["text"][:7], s["done"]) for s in hoya["subtasks"]] == \
        [("isolate", False), ("prune d", True)]
    assert hoya["done"] is False and ts[1]["done"] is True
    assert ts[-1]["section"] == "Routine" and hoya["section"] == ""
    assert hoya["line"] == 0 and ts[1]["line"] == 4

def test_parse_note_ignores_non_checkbox_lines_and_deeper_headings():
    body = "# Title\nsome prose\n- plain bullet\n- [ ] real\n\tnote under it\n### Sub\n- [ ] other\n"
    ts = tasks.parse_note(body)
    assert [t["text"] for t in ts] == ["real", "other"]
    assert ts[0]["section"] == "Title" and ts[1]["section"] == "Sub"

# ── identity and lifetime ────────────────────────────────────────────────────

def _by_text(chunks):
    return {c[1].split("\n", 1)[0].removeprefix("Task: "): c for c in chunks}

def test_one_chunk_per_task_across_carried_days(vault):
    chunks = list(tasks.iter_chunks())
    by = _by_text(chunks)
    hoya = by["treat hoya for mealybugs (alcohol wipe)"]
    m = hoya[2]["meta"]
    assert m["first_seen"] == "2026-08-27" and m["last_seen"] == "2026-08-28"
    assert m["days"] == 2 and m["done"] is False and m["done_on"] is None
    assert [s["done"] for s in m["subtasks"]] == [True, True]    # latest note wins
    assert hoya[2]["location"] == "2026-08-28.md#0"
    assert hoya[1] == ("Task: treat hoya for mealybugs (alcohol wipe)\n"
                       "\tisolate from other plants\n\tprune dead leaves")

def test_timestamp_is_completion_day_for_done_tasks(vault):
    by = _by_text(tasks.iter_chunks())
    briar = by["send Briar an email"][2]
    assert briar["meta"]["done"] and briar["meta"]["done_on"] == "2026-08-27"
    assert briar["meta"]["last_seen"] == "2026-08-27"
    workout = by["workout"][2]
    assert workout["meta"]["done_on"] == "2026-08-28" and workout["meta"]["days"] == 2
    # bare local date -> UTC ISO, same convention as obsidian frontmatter
    dt = datetime.fromisoformat(briar["timestamp"])
    assert dt.tzinfo == timezone.utc
    assert dt.astimezone().date().isoformat() == "2026-08-27"

def test_only_root_daily_notes_count(vault):
    texts = list(_by_text(tasks.iter_chunks()))
    assert "a task in a project note" not in texts
    assert "nested daily lookalike" not in texts

def test_routine_indexed_unless_turned_off(vault, monkeypatch):
    by = _by_text(tasks.iter_chunks())
    assert by["brush teeth and shower"][2]["meta"]["section"] == "Routine"
    monkeypatch.setenv("CLAUDE_RAG_TASKS_ROUTINE", "false")
    assert "brush teeth and shower" not in _by_text(tasks.iter_chunks())

def test_secret_bearing_task_dropped(vault):
    assert not any("API_KEY" in c[1] for c in tasks.iter_chunks())

def test_ids_are_stable_and_text_keyed(vault):
    a = [c[0] for c in tasks.iter_chunks()]
    assert len(set(a)) == len(a)
    assert all(i.startswith("tasks:") for i in a)
    (vault / "2026-08-29.md").write_text("- [ ] treat hoya for mealybugs (alcohol wipe)\n")
    b = [c[0] for c in tasks.iter_chunks()]
    assert set(a) == set(b)                       # carrying forward adds no ids

# ── expand ───────────────────────────────────────────────────────────────────

def test_expand_tasks_live_and_index(vault, monkeypatch, scratch_db, fake_embed):
    import server
    run_index(monkeypatch, [tasks])
    db = open_db(scratch_db)
    cid = db.execute("SELECT id FROM chunks WHERE source='tasks' AND "
                     "json_extract(meta,'$.last_seen')='2026-08-28' LIMIT 1").fetchone()[0]
    live = json.loads(server.expand(cid))
    assert live["context_source"] == "live"
    assert [t["text"][:10] for t in live["context"]["tasks"]] == \
        ["treat hoya", "workout", "work on a ", "brush teet"]
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", "/nonexistent")
    idx = json.loads(server.expand(cid))
    assert idx["context_source"] == "index"
    got = [t["text"][:10] for t in idx["context"]["tasks"]]
    assert got == ["treat hoya", "workout", "work on a "]    # routine not indexed
    assert idx["context"]["tasks"][0]["subtasks"][0]["done"] is True
