"""Convex applier: the pure line-flip (match by text, top-level only,
already-in-state, ambiguity), the vault-path apply, and the drain loop's
confirmation calls against a fake client."""
import pytest
from tests.helpers import load_script

ap = load_script("tools/convex-applier.py", "convex_applier")

NOTE = ["- [ ] treat hoya for mealybugs",
        "\t- [ ] isolate from other plants",
        "- [x] workout",
        "- [ ] Workout",          # differs only by case: ambiguous with the above
        "",
        "## Routine",
        "- [ ] brush teeth"]

def test_flip_to_done():
    new, err = ap.apply_to_lines(NOTE, "treat hoya for mealybugs", "done")
    assert err is None and new[0] == "- [x] treat hoya for mealybugs"
    assert new[1:] == NOTE[1:]

def test_flip_to_open_and_already_in_state():
    new, err = ap.apply_to_lines(NOTE, "brush teeth", "open")
    assert (new, err) == (None, None)
    new, err = ap.apply_to_lines(NOTE, "brush teeth", "done")
    assert new[6] == "- [x] brush teeth"

def test_subtask_is_not_a_match():
    new, err = ap.apply_to_lines(NOTE, "isolate from other plants", "done")
    assert new is None and "not found" in err

def test_ambiguous_text_is_refused():
    new, err = ap.apply_to_lines(NOTE, "workout", "open")
    assert new is None and "ambiguous" in err

def test_apply_intent_writes_the_note(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(NOTE) + "\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    err = ap.apply_intent({"vault": "Documents", "day": "2026-08-28",
                           "text": "treat hoya for mealybugs", "want": "done"})
    assert err is None
    assert (v / "2026-08-28.md").read_text().startswith("- [x] treat hoya")
    assert ap.apply_intent({"vault": "Other", "day": "2026-08-28",
                            "text": "x", "want": "done"}).startswith("vault")
    assert ap.apply_intent({"vault": "Documents", "day": "2026-01-01",
                            "text": "x", "want": "done"}) == "no note for 2026-01-01"

class FakeClient:
    def __init__(self): self.calls = []
    def mutation(self, name, args): self.calls.append((name, args))

def test_drain_confirms_each_intent(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(NOTE) + "\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    kicked = []
    monkeypatch.setattr(ap, "_kick", lambda: kicked.append(1))
    fake = FakeClient()
    n = ap.drain(fake, [
        {"id": "i1", "vault": "Documents", "day": "2026-08-28",
         "text": "treat hoya for mealybugs", "want": "done"},
        {"id": "i2", "vault": "Documents", "day": "2026-08-28",
         "text": "nope", "want": "done"}], kick=True)
    assert n == 1
    assert fake.calls == [("today:applyIntent", {"id": "i1"}),
                          ("today:applyIntent", {"id": "i2", "error": "task not found in that day's note"})]
    assert kicked == [1]


# ── add / edit / delete (need the daily-tasks skill for the note grammar) ──

skill_present = pytest.mark.skipif(
    not __import__("os").path.isfile(__import__("config").CONVEX_TASKS_SCRIPT),
    reason="daily-tasks skill not installed")

BLOCK = ["- [ ] treat hoya for mealybugs",
         "\t- [ ] isolate from other plants",
         "\t![[2026-08-28 hoya.jpeg]]",
         "- [x] workout",
         "",
         "## Routine",
         "- [ ] brush teeth"]

@skill_present
def test_add_goes_before_routine_and_refuses_duplicates():
    new, err = ap.add_to_lines(BLOCK, "book dentist")
    assert err is None
    assert new[:5] == BLOCK[:4] + ["- [ ] book dentist"]
    assert new[-2:] == ["## Routine", "- [ ] brush teeth"]
    new, err = ap.add_to_lines(BLOCK, "  Workout ")
    assert new is None and "already" in err

@skill_present
def test_edit_keeps_checkbox_and_block():
    new, err = ap.edit_in_lines(BLOCK, "workout", "workout (legs)")
    assert err is None and new[3] == "- [x] workout (legs)" and new[:3] == BLOCK[:3]
    assert ap.edit_in_lines(BLOCK, "workout", "WORKOUT") == (None, None)
    new, err = ap.edit_in_lines(BLOCK, "workout", "treat hoya for mealybugs")
    assert new is None and "already" in err
    new, err = ap.edit_in_lines(BLOCK, "nope", "x")
    assert new is None and "not found" in err

@skill_present
def test_delete_removes_the_whole_block():
    new, err = ap.delete_from_lines(BLOCK, "treat hoya for mealybugs")
    assert err is None and new == BLOCK[3:]
    new, err = ap.delete_from_lines(BLOCK, "isolate from other plants")
    assert new is None and "not found" in err

@skill_present
def test_add_to_missing_note_starts_the_day(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(BLOCK) + "\n")
    (v / "Templates").mkdir()
    (v / "Templates" / "Daily Tasks Template.md").write_text("- [ ] brush teeth\n- [ ] gym #mon\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    err = ap.apply_intent({"kind": "add", "vault": "Documents", "day": "2026-08-29",
                           "text": "book dentist"})
    assert err is None
    got = (v / "2026-08-29.md").read_text().split("\n")
    assert got[0] == "- [ ] treat hoya for mealybugs"        # carried, with its block
    assert "\t- [ ] isolate from other plants" in got
    assert "- [ ] book dentist" in got and "- [x] workout" not in got
    assert got.index("- [ ] book dentist") < got.index("## Routine")
    assert "- [ ] brush teeth" in got and "- [ ] gym" not in got  # 2026-08-29 is a Saturday

@skill_present
def test_start_creates_the_note_without_adding(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(BLOCK) + "\n")
    (v / "Templates").mkdir()
    (v / "Templates" / "Daily Tasks Template.md").write_text("- [ ] brush teeth\n- [ ] gym #mon\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    err = ap.apply_intent({"kind": "start", "vault": "Documents", "day": "2026-08-29", "text": ""})
    assert err is None
    got = (v / "2026-08-29.md").read_text().split("\n")
    assert got[0] == "- [ ] treat hoya for mealybugs"        # carried, with its block
    assert "\t- [ ] isolate from other plants" in got
    assert "- [x] workout" not in got                        # done stays behind
    assert "- [ ] brush teeth" in got and "- [ ] gym" not in got  # 2026-08-29 is a Saturday
    # a start on a day that already has a note leaves it alone
    before = (v / "2026-08-28.md").read_text()
    assert ap.apply_intent({"kind": "start", "vault": "Documents", "day": "2026-08-28", "text": ""}) is None
    assert (v / "2026-08-28.md").read_text() == before

@skill_present
def test_other_kinds_need_an_existing_note(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    assert ap.apply_intent({"kind": "delete", "vault": "Documents", "day": "2026-08-29",
                            "text": "x"}) == "no note for 2026-08-29"
    (v / "2026-08-29.md").write_text("- [ ] x\n")
    assert "unknown" in ap.apply_intent({"kind": "zap", "vault": "Documents", "day": "2026-08-29",
                                         "text": "x"})


@skill_present
def test_subtask_toggle_add_edit_delete():
    new, err = ap.apply_sub(BLOCK, "treat hoya for mealybugs", "toggle", "isolate from other plants", want="done")
    assert err is None and new[1] == "\t- [x] isolate from other plants" and new[2] == BLOCK[2]
    assert ap.apply_sub(BLOCK, "treat hoya for mealybugs", "toggle", "isolate from other plants", want="open") == (None, None)
    new, err = ap.apply_sub(BLOCK, "treat hoya for mealybugs", "add", "order neem oil")
    assert err is None and new[3] == "\t- [ ] order neem oil" and new[4] == "- [x] workout"
    new, err = ap.apply_sub(BLOCK, "workout", "add", "legs")
    assert err is None and new[4] == "\t- [ ] legs"
    new, err = ap.apply_sub(BLOCK, "treat hoya for mealybugs", "edit", "isolate from other plants", new_text="quarantine")
    assert err is None and new[1] == "\t- [ ] quarantine"
    new, err = ap.apply_sub(BLOCK, "treat hoya for mealybugs", "delete", "isolate from other plants")
    assert err is None and new == [BLOCK[0], BLOCK[2]] + BLOCK[3:]
    assert "parent task not found" in ap.apply_sub(BLOCK, "nope", "toggle", "x", want="done")[1]
    assert "subtask not found" in ap.apply_sub(BLOCK, "workout", "toggle", "x", want="done")[1]
    assert "already" in ap.apply_sub(BLOCK, "treat hoya for mealybugs", "add", "Isolate from other plants")[1]

@skill_present
def test_apply_intent_routes_subtasks(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(BLOCK) + "\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    err = ap.apply_intent({"kind": "add", "parent": "workout", "vault": "Documents",
                           "day": "2026-08-28", "text": "legs"})
    assert err is None
    assert "- [x] workout\n\t- [ ] legs\n" in (v / "2026-08-28.md").read_text()


@skill_present
def test_attach_appends_a_line_at_the_block_end():
    fake = lambda url: "Mealybugs — RHS"
    new, err = ap.attach_to_lines(BLOCK, "treat hoya for mealybugs", "https://rhs.org/mealybugs", title=fake)
    assert err is None and new[3] == "\t- [Mealybugs — RHS](https://rhs.org/mealybugs)" and new[4] == "- [x] workout"
    new, err = ap.attach_to_lines(BLOCK, "workout", "felt strong today", title=fake)
    assert err is None and new[4] == "\t- felt strong today"
    assert ap.attach_line("https://x.y/z", title=lambda u: None) == "[https://x.y/z](https://x.y/z)"
    assert "parent task not found" in ap.attach_to_lines(BLOCK, "nope", "x", title=fake)[1]


@skill_present
def test_attach_file_copies_into_the_vault(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(BLOCK) + "\n")
    src = tmp_path / "IMG_1.JPG"; src.write_bytes(b"jpeg")
    err = ap.attach_file(str(v), str(v / "2026-08-28.md"), "2026-08-28", "workout", "IMG 1.JPG", str(src))
    assert err is None
    assert (v / "Attachments" / "2026-08-28 IMG-1.jpg").read_bytes() == b"jpeg"
    got = (v / "2026-08-28.md").read_text().split("\n")
    assert got[3:5] == ["- [x] workout", "\t![[2026-08-28 IMG-1.jpg]]"]
    err = ap.attach_file(str(v), str(v / "2026-08-28.md"), "2026-08-28", "workout", "IMG 1.JPG", str(src))
    assert err is None and (v / "Attachments" / "2026-08-28 IMG-1-2.jpg").exists()
    assert "parent task not found" in ap.attach_file(str(v), str(v / "2026-08-28.md"), "2026-08-28", "nope", "a.png", str(src))
