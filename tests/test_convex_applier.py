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
    monkeypatch.setattr(ap, "_kick", lambda *a: kicked.append(a or (("tasks",),)))
    fake = FakeClient()
    n = ap.drain(fake, [
        {"id": "i1", "vault": "Documents", "day": "2026-08-28",
         "text": "treat hoya for mealybugs", "want": "done"},
        {"id": "i2", "vault": "Documents", "day": "2026-08-28",
         "text": "nope", "want": "done"}], kick=True)
    assert n == 1
    assert fake.calls == [("today:applyIntent", {"id": "i1"}),
                          ("today:applyIntent", {"id": "i2", "error": "task not found in that day's note"})]
    assert kicked == [(("tasks",),)]


def test_drain_kicks_obsidian_when_a_note_landed(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-28.md").write_text("\n".join(NOTE) + "\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    kicked = []
    monkeypatch.setattr(ap, "_kick", lambda srcs=("tasks",): kicked.append(srcs))
    ap.drain(FakeClient(), [
        {"id": "i1", "kind": "note", "vault": "Documents", "day": "2026-08-28",
         "text": "an idea", "at": "14:32"}], kick=True)
    assert kicked == [("tasks", "obsidian")]


def test_note_appends_creates_section_and_starts_day(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    # no ## Notes yet: created at the end
    (v / "2026-08-28.md").write_text("\n".join(NOTE) + "\n")
    err = ap.apply_intent({"kind": "note", "vault": "Documents", "day": "2026-08-28",
                           "text": "  check   the attic  quote ", "at": "14:32"})
    assert err is None
    body = (v / "2026-08-28.md").read_text()
    assert body.endswith("## Notes\n- 14:32 check the attic quote\n")
    # existing section with trailing blank + next heading: insert inside it
    (v / "2026-08-29.md").write_text(
        "- [ ] a task\n\n## Notes\n- 09:00 first\n\n## Log\nstuff\n")
    err = ap.apply_intent({"kind": "note", "vault": "Documents", "day": "2026-08-29",
                           "text": "second", "at": "10:15"})
    assert err is None
    assert ("## Notes\n- 09:00 first\n- 10:15 second\n\n## Log"
            in (v / "2026-08-29.md").read_text())
    # missing note: created via the start-day path first
    err = ap.apply_intent({"kind": "note", "vault": "Documents", "day": "2026-08-30",
                           "text": "early thought", "at": "06:12"})
    assert err is None
    body = (v / "2026-08-30.md").read_text()
    assert "- 06:12 early thought" in body and "## Notes" in body
    assert ap.apply_intent({"kind": "note", "vault": "Documents",
                            "day": "2026-08-30", "text": "   ", "at": ""}) == "empty note"


# ── add / edit / delete (need the daily-tasks skill for the note grammar) ──

skill_present = pytest.mark.skipif(
    not __import__("os").path.isfile(__import__("config").CONVEX_TASKS_SCRIPT),
    reason="daily-tasks skill not installed")

INSTALLED_SKILL = __import__("os").path.expanduser(
    "~/.claude/skills/daily-tasks/tasks.py")

@pytest.mark.skipif(not __import__("os").path.isfile(INSTALLED_SKILL),
                    reason="daily-tasks skill not installed")
def test_vendored_skill_matches_installed():
    """tests/fixtures/daily_tasks.py is a copy of the installed skill with only
    DEFAULT_VAULT sanitized. Re-vendor when the skill changes."""
    import config, re
    strip = lambda p: re.sub(r"(?m)^DEFAULT_VAULT = .*$", "", open(p).read())
    assert strip(config.CONVEX_TASKS_SCRIPT) == strip(INSTALLED_SKILL)

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

def test_applies_day_tags():
    assert ap._applies([], "2026-08-31")
    assert ap._applies(["mon"], "2026-08-31")            # a Monday
    assert not ap._applies(["tue"], "2026-08-31")
    assert ap._applies(["weekday"], "2026-08-31")
    assert not ap._applies(["weekend"], "2026-08-31")
    assert ap._applies(["weekend"], "2026-09-05")        # a Saturday

@skill_present
def test_routine_template_add_edit_delete():
    tpl = ["- [ ] brush teeth", "- [ ] gym #mon #wed", "\t- [ ] stretch"]
    new, err = ap.routine_add_tpl(tpl, "meditate", ["weekday"])
    assert err is None and new[-1] == "- [ ] meditate #weekday"
    new, err = ap.routine_add_tpl(tpl, "  GYM ", [])
    assert new is None and "already" in err
    new, err = ap.routine_edit_tpl(tpl, "gym", "lift weights", None)
    assert err is None and new[1] == "- [ ] lift weights #mon #wed"   # tags kept
    assert new[2] == "\t- [ ] stretch"                                # block kept
    new, err = ap.routine_edit_tpl(tpl, "gym", None, ["sat"])
    assert err is None and new[1] == "- [ ] gym #sat"
    new, err = ap.routine_edit_tpl(tpl, "gym", None, [])
    assert err is None and new[1] == "- [ ] gym"                      # [] = every day
    new, err = ap.routine_edit_tpl(tpl, "gym", "brush teeth", None)
    assert new is None and "already" in err
    new, err = ap.routine_delete_tpl(tpl, "gym")
    assert err is None and new == ["- [ ] brush teeth"]
    new, err = ap.routine_delete_tpl(tpl, "nope")
    assert new is None and "not found" in err

@skill_present
def test_routine_add_intent_hits_template_and_today(tmp_path, monkeypatch):
    v = tmp_path / "Documents"; v.mkdir()
    (v / "2026-08-31.md").write_text("- [ ] existing\n")             # a Monday
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    err = ap.apply_intent({"kind": "routineAdd", "vault": "Documents",
                           "day": "2026-08-31", "text": "meditate", "days": ["mon"]})
    assert err is None
    tpl = v / "Templates" / "Daily Tasks Template.md"
    assert tpl.read_text().strip() == "- [ ] meditate #mon"
    note = (v / "2026-08-31.md").read_text().split("\n")
    assert "## Routine" in note and "- [ ] meditate" in note
    # a schedule that skips today stays out of the note
    err = ap.apply_intent({"kind": "routineAdd", "vault": "Documents",
                           "day": "2026-08-31", "text": "mow lawn", "days": ["sat"]})
    assert err is None
    assert "mow lawn" not in (v / "2026-08-31.md").read_text()
    assert "- [ ] mow lawn #sat" in tpl.read_text()
    # edit and delete route through apply_intent too
    assert ap.apply_intent({"kind": "routineEdit", "vault": "Documents", "day": "routine",
                            "text": "mow lawn", "newText": "mow the lawn"}) is None
    assert "- [ ] mow the lawn #sat" in tpl.read_text()
    assert ap.apply_intent({"kind": "routineDelete", "vault": "Documents", "day": "routine",
                            "text": "meditate"}) is None
    assert "meditate" not in tpl.read_text()

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


# ── vault lists (wip/SPEC-vault-lists.md) ──

GROC = ["---", "words: to get / got / done shopping", "---",
        "- [ ] milk", "- [x] butter", "",
        "## Catalog", "- rice", "- hot sauce"]

def _mklist(tmp_path, monkeypatch, lines=GROC):
    v = tmp_path / "Documents"; (v / "Lists").mkdir(parents=True)
    (v / "Lists" / "Groceries.md").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("CLAUDE_RAG_OBSIDIAN_VAULTS", str(v))
    return v

def _lst(v):
    return (v / "Lists" / "Groceries.md").read_text().split("\n")

def _go(v, **kw):
    return ap.apply_intent({"vault": "Documents", "day": "list",
                            "path": "Lists/Groceries.md", **kw})

def test_list_set_transitions(tmp_path, monkeypatch):
    v = _mklist(tmp_path, monkeypatch)
    assert _go(v, kind="listSet", text="milk", want="got") is None
    assert "- [x] milk" in _lst(v)
    assert _go(v, kind="listSet", text="rice", want="need") is None
    body = _lst(v)
    assert "- [ ] rice" in body and body.index("- [ ] rice") < body.index("## Catalog")
    # got -> cat lands at the catalog head (frecency)
    assert _go(v, kind="listSet", text="butter", want="cat") is None
    body = _lst(v)
    assert body[body.index("## Catalog") + 1] == "- butter"
    # already in state is a quiet no-op; unknown item errors
    assert _go(v, kind="listSet", text="hot sauce", want="cat") is None
    assert "not found" in _go(v, kind="listSet", text="quinoa", want="need")

def test_list_add_dedupes_and_reset_shelves(tmp_path, monkeypatch):
    v = _mklist(tmp_path, monkeypatch)
    assert _go(v, kind="listAdd", text="  frozen   peas ") is None
    body = _lst(v)
    assert "- [ ] frozen peas" in body
    assert body.index("- [ ] frozen peas") < body.index("## Catalog")
    assert _go(v, kind="listAdd", text="RICE") == "already on the list"
    # reset: got items move to the catalog head, needed stay put
    assert _go(v, kind="listSet", text="frozen peas", want="got") is None
    assert _go(v, kind="listReset") is None
    body = _lst(v)
    cat = body.index("## Catalog")
    assert body[cat + 1:cat + 3] == ["- butter", "- frozen peas"]
    assert "- [ ] milk" in body[:cat] and not any(x.startswith("- [x]") for x in body[:cat])

def test_list_edit_remove_and_create(tmp_path, monkeypatch):
    v = _mklist(tmp_path, monkeypatch)
    assert _go(v, kind="listEdit", text="milk", newText="oat milk") is None
    assert "- [ ] oat milk" in _lst(v)
    assert _go(v, kind="listEdit", text="oat milk", newText="rice") == \
        "an item with that text already exists"
    assert _go(v, kind="listRemove", text="hot sauce") is None
    assert not any("hot sauce" in x for x in _lst(v))
    err = ap.apply_intent({"vault": "Documents", "day": "list", "kind": "listCreate",
                           "text": "Camping", "words": {"need": "to pack", "got": "packed",
                                                        "done": "trip done"}})
    assert err is None
    body = (v / "Lists" / "Camping.md").read_text()
    assert body.startswith("---\nwords: to pack / packed / trip done\n---")
    assert "## Catalog" in body
    assert "already exists" in ap.apply_intent(
        {"vault": "Documents", "day": "list", "kind": "listCreate", "text": "Camping"})

def test_list_path_traversal_refused(tmp_path, monkeypatch):
    v = _mklist(tmp_path, monkeypatch)
    for bad in ("Lists/../2026-08-28.md", "../outside.md", "Templates/x.md",
                "Lists/sub/dir.md", ""):
        err = _go(v, kind="listSet", text="milk", want="got", path=bad)
        assert err and "bad list path" in err, bad
    # creation sanitizes hostile names into Lists/ instead of escaping it
    assert ap.apply_intent({"vault": "Documents", "day": "list",
                            "kind": "listCreate", "text": "../evil"}) is None
    assert not (tmp_path / "Documents" / "evil.md").exists()
    assert (tmp_path / "Documents" / "Lists" / "-evil.md").exists()
    assert "bad list name" in ap.apply_intent(
        {"vault": "Documents", "day": "list", "kind": "listCreate", "text": "  "})
