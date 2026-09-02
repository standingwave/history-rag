import { test } from "node:test";
import assert from "node:assert/strict";

import { dayArg } from "../convex/dates.ts";

test("bare day passes through", () => {
  assert.equal(dayArg("2026-09-01"), "2026-09-01");
});

test("ISO timestamp truncates to its day", () => {
  assert.equal(dayArg("2026-09-01T15:47:00.000Z"), "2026-09-01");
  assert.equal(dayArg("2026-09-02T00:00:00Z"), "2026-09-02");
});

test("non-date strings and absent values pass through", () => {
  assert.equal(dayArg("yesterday"), "yesterday");
  assert.equal(dayArg(""), "");
  assert.equal(dayArg(undefined), undefined);
});
