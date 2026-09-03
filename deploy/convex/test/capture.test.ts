import { test } from "node:test";
import assert from "node:assert/strict";

import { verbatim, autoApplies } from "../src/capture.ts";

test("note: prefix strips and returns the literal text", () => {
  assert.equal(verbatim("note: parked on level 2"), "parked on level 2");
  assert.equal(verbatim("Note, call dad tomorrow"), "call dad tomorrow");
  assert.equal(verbatim("NOTE:Create a 30 min timer"), "Create a 30 min timer");
});

test("non-prefixed and empty-rest inputs fall through to the parser", () => {
  assert.equal(verbatim("notes from the meeting"), null);
  assert.equal(verbatim("add a note about tea"), null);
  assert.equal(verbatim("note:"), null);
  assert.equal(verbatim("note:   "), null);
});

test("all-creation proposals auto-apply", () => {
  for (const kind of ["task", "note", "listAdd", "listCreate", "timerStart"]) {
    assert.ok(autoApplies([{ kind }]), kind);
  }
  assert.ok(autoApplies([{ kind: "task" }, { kind: "timerStart" }]));
});

test("any mutation in the set asks via chips", () => {
  for (const kind of ["toggle", "edit", "delete", "listSet", "listEdit", "listRemove", "timerCtl"]) {
    assert.ok(!autoApplies([{ kind }]), kind);
    assert.ok(!autoApplies([{ kind: "task" }, { kind }]), `mixed ${kind}`);
  }
  assert.ok(!autoApplies([]));
});
