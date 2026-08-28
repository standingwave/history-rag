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
