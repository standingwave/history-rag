/* npm test — timer status is derived, never stored; these pin the
   derivation (especially repeat rollover) and the display formats. */
import test from "node:test";
import assert from "node:assert/strict";
import { derive, fmt, durLabel } from "../convex/timerMath.ts";

const MIN = 60_000;

test("running, paused, done", () => {
  const now = 1_000_000_000;
  assert.deepEqual(derive({ durationMs: 5 * MIN, endsAt: now + 90_000 }, now),
    { st: "running", left: 90_000, cycle: 0 });
  assert.deepEqual(derive({ durationMs: 45 * MIN, remainingMs: 10 * MIN }, now),
    { st: "paused", left: 10 * MIN, cycle: 0 });
  assert.deepEqual(derive({ durationMs: 5 * MIN, endsAt: now }, now),
    { st: "done", left: 0, cycle: 0 });
  assert.deepEqual(derive({ durationMs: 5 * MIN, endsAt: now - 3600_000 }, now),
    { st: "done", left: 0, cycle: 0 });
});

test("repeat rolls into cycle k with left in (0, duration]", () => {
  const now = 1_000_000_000, t = { durationMs: 30_000, repeat: true };
  // just crossed the first end
  assert.deepEqual(derive({ ...t, endsAt: now }, now), { st: "running", left: 30_000, cycle: 1 });
  assert.deepEqual(derive({ ...t, endsAt: now - 5_000 }, now), { st: "running", left: 25_000, cycle: 1 });
  // slept through two full cycles: wakes up in cycle 3, correct remainder
  assert.deepEqual(derive({ ...t, endsAt: now - 65_000 }, now), { st: "running", left: 25_000, cycle: 3 });
  // exactly on a later boundary
  assert.deepEqual(derive({ ...t, endsAt: now - 60_000 }, now), { st: "running", left: 30_000, cycle: 3 });
  // still in the first cycle: no rollover
  assert.deepEqual(derive({ ...t, endsAt: now + 1 }, now), { st: "running", left: 1, cycle: 0 });
});

test("fmt", () => {
  assert.equal(fmt(161_000), "2:41");
  assert.equal(fmt(999), "0:01");        // ceil: never shows 0:00 while time remains
  assert.equal(fmt(0), "0:00");
  assert.equal(fmt(-5), "0:00");
  assert.equal(fmt(3600_000), "1:00:00");
  assert.equal(fmt(5 * 3600_000 + 62_000), "5:01:02");
});

test("durLabel", () => {
  assert.equal(durLabel(30_000), "30s");
  assert.equal(durLabel(5 * MIN), "5m");
  assert.equal(durLabel(90 * MIN), "90m");
  assert.equal(durLabel(3600_000), "1h");
  assert.equal(durLabel(2 * 3600_000), "2h");
});
