/* npm test — the command parser's pure half (convex/parseCore.ts):
   prompt shape and, mainly, the validator that gates model JSON. */
import test from "node:test";
import assert from "node:assert/strict";
import { parseSystem, parseUser, validateActions, type ParseCtx } from "../convex/parseCore.ts";

const CTX: ParseCtx = {
  today: "2026-09-02",
  tasks: [{ id: "t1", text: "Call mom" }],
  lists: [{ path: "Lists/Grocery.md", name: "Grocery",
            items: [{ id: "g1", text: "milk", state: "cat" }] }],
  timers: [{ id: "w1", label: "tea", state: "running" }],
};
const wrap = (actions: unknown[]) => JSON.stringify({ actions });

test("prompt pins the day and offers ids", () => {
  assert.ok(parseSystem("2026-09-02").includes("2026-09-02 (Wednesday)"));
  const u = parseUser("got the milk", CTX);
  assert.ok(u.includes('"t1"') && u.includes("Lists/Grocery.md") && u.includes('"g1"'));
  assert.ok(u.includes('SENTENCE: "got the milk"'));
});

test("valid actions pass through with labels attached", () => {
  const r = validateActions(wrap([
    { kind: "task", text: "Call dad", day: "2026-09-03" },
    { kind: "toggle", id: "t1", done: true },
    { kind: "listSet", id: "g1", state: "need" },
    { kind: "timerCtl", id: "w1", op: "pause" },
  ]), "x", CTX);
  assert.equal(r.fallback, false);
  assert.equal(r.actions.length, 4);
  assert.deepEqual(r.actions[0], { kind: "task", text: "Call dad", day: "2026-09-03" });
  assert.deepEqual(r.actions[1], { kind: "toggle", id: "t1", done: true, label: "Call mom" });
  assert.deepEqual(r.actions[3], { kind: "timerCtl", id: "w1", op: "pause", label: "tea" });
});

test("ids the prompt never offered are dropped, not queued", () => {
  const r = validateActions(wrap([
    { kind: "delete", id: "t999" },
    { kind: "listRemove", id: "gx" },
    { kind: "timerCtl", id: "wx", op: "dismiss" },
  ]), "delete the gym task", CTX);
  assert.deepEqual(r, { actions: [{ kind: "note", text: "delete the gym task" }], fallback: true });
});

test("day defaults to today; out-of-range and malformed days drop the action", () => {
  const r = validateActions(wrap([
    { kind: "task", text: "a" },
    { kind: "task", text: "b", day: "2031-01-01" },
    { kind: "task", text: "c", day: "tomorrow" },
  ]), "x", CTX);
  assert.deepEqual(r.actions, [{ kind: "task", text: "a", day: "2026-09-02" }]);
});

test("timer starts: countdown needs sane ms, focus implies stopwatch", () => {
  const r = validateActions(wrap([
    { kind: "timerStart", label: "tea", ms: 600000 },
    { kind: "timerStart", label: "bad", ms: -5 },
    { kind: "timerStart", label: "focus", taskId: "t1" },
    { kind: "timerStart", label: "orphan focus", taskId: "t999", ms: 60000 },
  ]), "x", CTX);
  assert.deepEqual(r.actions, [
    { kind: "timerStart", label: "tea", ms: 600000 },
    { kind: "timerStart", label: "focus", ms: 0, up: true, taskId: "t1" },
    { kind: "timerStart", label: "orphan focus", ms: 60000 },   // bogus taskId stripped
  ]);
});

test("unparseable output and empty results fall back to a note", () => {
  assert.deepEqual(validateActions("Sure! I can help.", "buy milk", CTX),
    { actions: [{ kind: "note", text: "buy milk" }], fallback: true });
  assert.deepEqual(validateActions(wrap([]), "hmm", CTX).fallback, true);
  const chatty = 'Here you go:\n```json\n{"actions":[{"kind":"note","text":"hi"}]}\n```';
  assert.deepEqual(validateActions(chatty, "x", CTX).actions, [{ kind: "note", text: "hi" }]);
});

test("caps: 10 actions, 20 list items, 500-char text", () => {
  const many = Array.from({ length: 15 }, (_, i) => ({ kind: "note", text: `n${i}` }));
  assert.equal(validateActions(wrap(many), "x", CTX).actions.length, 10);
  const items = Array.from({ length: 30 }, (_, i) => `i${i}`);
  const r = validateActions(wrap([{ kind: "listAdd", path: "Lists/Grocery.md", items }]), "x", CTX);
  assert.equal((r.actions[0] as { items: string[] }).items.length, 20);
  const long = validateActions(wrap([{ kind: "note", text: "y".repeat(900) }]), "x", CTX);
  assert.equal((long.actions[0] as { text: string }).text.length, 500);
});
