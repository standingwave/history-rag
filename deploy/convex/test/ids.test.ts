/* npm test — ids must equal what sources/tasks.py
   produces; the fixture was generated from the Python source. */
import test from "node:test";
import assert from "node:assert/strict";
import { sha256Hex, taskChunkId, routineChunkId } from "../convex/ids.ts";

test("sha256 known answers", () => {
  assert.equal(sha256Hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  assert.equal(sha256Hex("a".repeat(1000)).length, 64);
});

test("task ids match the Python source", () => {
  assert.equal(taskChunkId("Documents", "treat hoya for mealybugs"), "tasks:e9282a930f290f7c5fddf61221");
  assert.equal(taskChunkId("Documents", "  Book   Dentist "), "tasks:ff8ffc86a3d54b660cffbb81f4");
  assert.equal(taskChunkId("Vault", "ünïcödé — café ☕"), "tasks:7f91912e3fbdabdcff3a8155da");
});

test("routine ids match the Python source and differ from task ids", () => {
  assert.equal(routineChunkId("Documents", "brush teeth and shower"), "tasks:6a5f7f95575d20cc7a0127d637");
  assert.equal(routineChunkId("Documents", "  Workout "), "tasks:d333a7a9bb4fbb578757378412");
  assert.equal(routineChunkId("Vault", "ünïcödé — café ☕"), "tasks:c42c572c87a7298a520a4ef902");
  assert.notEqual(routineChunkId("Documents", "workout"), taskChunkId("Documents", "workout"));
});
