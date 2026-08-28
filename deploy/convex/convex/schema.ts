/* Spike schema (wip/SPEC-convex-spike.md). `items` mirrors the SQLite
   chunks the Mac pushes — one row per chunk, the RAG component holds the
   vector under the same key. `taskIntents` is the write queue the Mac
   drains. Nothing here is a source of truth: the vault and the Mac's index
   are; a wiped deployment is rebuilt by `tools/sync-convex.py --full`. */
import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";
import { authTables } from "@convex-dev/auth/server";

export const want = v.union(v.literal("done"), v.literal("open"));

export default defineSchema({
  ...authTables,
  items: defineTable({
    chunkId: v.string(),
    source: v.string(),
    timestamp: v.string(),      // UTC ISO, as indexed
    day: v.string(),            // local YYYY-MM-DD (the Mac's TZ)
    month: v.string(),          // local YYYY-MM
    location: v.string(),
    text: v.string(),
    meta: v.any(),
    entryId: v.string(),        // RAG entry holding this chunk's vector
    contentHash: v.string(),    // text + filter values; the sync diff key
    pending: v.optional(v.boolean()),  // optimistic state awaiting the Mac
  })
    .index("by_chunkId", ["chunkId"])
    .index("by_source_day", ["source", "day"])
    .index("by_source_timestamp", ["source", "timestamp"]),
  taskIntents: defineTable({
    chunkId: v.string(),
    day: v.string(),
    vault: v.string(),
    text: v.string(),           // the task line; the Mac matches on this
    want,
    requestedAt: v.number(),
    appliedAt: v.optional(v.number()),
    error: v.optional(v.string()),
  }).index("by_appliedAt", ["appliedAt"]),
  syncRuns: defineTable({
    source: v.string(),
    startedAt: v.number(),
    finishedAt: v.number(),
    upserted: v.number(),
    removed: v.number(),
  }),
});
