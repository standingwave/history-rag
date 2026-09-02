/* Spike schema (wip/SPEC-convex-spike.md). `items` mirrors the SQLite
   chunks the Mac pushes — one row per chunk, the RAG component holds the
   vector under the same key. `taskIntents` is the write queue the Mac
   drains. Nothing here is a source of truth: the vault and the Mac's index
   are; a wiped deployment is rebuilt by `tools/sync-convex.py --full`. */
import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";
import { authTables } from "@convex-dev/auth/server";

export const want = v.union(
  v.literal("done"), v.literal("open"),
  // vault-list states (listSet)
  v.literal("need"), v.literal("got"), v.literal("cat"));
export const intentKind = v.union(
  v.literal("toggle"), v.literal("add"), v.literal("edit"), v.literal("delete"), v.literal("attach"),
  v.literal("start"), v.literal("note"),
  v.literal("routineAdd"), v.literal("routineEdit"), v.literal("routineDelete"),
  v.literal("listSet"), v.literal("listAdd"), v.literal("listEdit"),
  v.literal("listRemove"), v.literal("listReset"), v.literal("listCreate"));

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
    hidden: v.optional(v.boolean()),   // optimistic delete/edit; readers skip it
  })
    .index("by_chunkId", ["chunkId"])
    .index("by_source_day", ["source", "day"])
    .index("by_source_timestamp", ["source", "timestamp"])
    .index("by_day", ["day", "timestamp"])                 // cross-source windows
    .index("by_source_location", ["source", "location"]),  // a note's sections, a repo
  taskIntents: defineTable({
    chunkId: v.string(),
    day: v.string(),
    vault: v.string(),
    text: v.string(),           // the task line; the Mac matches on this
    kind: v.optional(intentKind),   // absent = toggle (rows from before kinds)
    want: v.optional(want),         // toggle only
    newText: v.optional(v.string()),  // edit only
    days: v.optional(v.array(v.string())),  // routineAdd/Edit: schedule tags; [] = daily
    at: v.optional(v.string()),       // note: the phone's local HH:MM at capture
    path: v.optional(v.string()),     // list*: the note inside Lists/
    words: v.optional(v.object({      // listCreate: the generated wording
      need: v.string(), got: v.string(), done: v.string() })),
    parent: v.optional(v.string()),   // set = the intent is about a subtask of this task
    prior: v.optional(v.any()),       // {subtasks, notes, attachments} before the change (revert)
    storageId: v.optional(v.id("_storage")),  // attach: the uploaded file, deleted once applied
    requestedAt: v.number(),
    appliedAt: v.optional(v.number()),
    error: v.optional(v.string()),
  }).index("by_appliedAt", ["appliedAt"]),
  /* history_stats: one row per source, kept by sync:putItems /
     deleteItems and rebuilt by sync:rebuildStats. */
  stats: defineTable({
    source: v.string(),
    count: v.number(),
    earliestDay: v.string(),
    latestDay: v.string(),
    updatedAt: v.number(),
  }).index("by_source", ["source"]),
  /* Countdown timers and stopwatches (wip/SPEC-timers.md) — pure phone
     state, no Mac involvement. Running = endsAt set; paused = remainingMs
     set; done and repeat cycles are derived from the clock, never stored.
     A stopwatch (up) reuses the fields inverted: endsAt is the adjusted
     start, remainingMs the paused elapsed. Dismiss deletes the row.
     alarmId is the scheduled push for the next cycle end. */
  timers: defineTable({
    label: v.string(),
    durationMs: v.number(),
    repeat: v.optional(v.boolean()),
    up: v.optional(v.boolean()),
    endsAt: v.optional(v.number()),
    remainingMs: v.optional(v.number()),
    startedAt: v.number(),
    alarmId: v.optional(v.id("_scheduled_functions")),
  }),
  /* Web Push subscriptions (timers' lock-screen alerts), one per
     browser/PWA install; dead endpoints are pruned on send failure. */
  pushSubs: defineTable({
    endpoint: v.string(),
    keys: v.object({ p256dh: v.string(), auth: v.string() }),
    ua: v.optional(v.string()),
  }).index("by_endpoint", ["endpoint"]),
  /* Event reminders (wip/SPEC-event-reminders.md): one row per reminder
     sent, keyed by event chunk + the start it was sent for, so a moved
     event reminds again at its new time. GC'd by the sweep after 48 h. */
  eventReminders: defineTable({
    chunkId: v.string(),
    start: v.string(),
    sentAt: v.number(),
  }).index("by_chunk_start", ["chunkId", "start"]),
  /* Per-feature switches: remindEvents (bool), timezone (IANA name).
     Absent row = the feature's default. */
  prefs: defineTable({
    name: v.string(),
    value: v.any(),
  }).index("by_name", ["name"]),
  /* The MCP connector's OAuth (wip/SPEC-convex-mcp.md): claude.ai
     self-registers (DCR), the grant is approved with a code minted by the
     signed-in app, and bearers are stored hashed. Revoke = delete rows. */
  oauthClients: defineTable({
    clientId: v.string(),
    redirectUris: v.array(v.string()),
    name: v.optional(v.string()),
  }).index("by_clientId", ["clientId"]),
  oauthCodes: defineTable({
    codeHash: v.string(),
    clientId: v.string(),
    redirectUri: v.string(),
    challenge: v.string(),        // PKCE S256 challenge
    expiresAt: v.number(),
    used: v.boolean(),
  }).index("by_codeHash", ["codeHash"]),
  oauthTokens: defineTable({
    tokenHash: v.string(),
    clientId: v.string(),
    expiresAt: v.number(),
  }).index("by_tokenHash", ["tokenHash"]),
  /* 8-digit approval codes the app mints; entering one on the authorize
     page is what turns a claude.ai auth request into a grant. */
  mcpApprovals: defineTable({
    code: v.string(),
    expiresAt: v.number(),
    used: v.boolean(),
  }).index("by_code", ["code"]),
  /* The dashboard's standing question, answered by Ask on a schedule
     (brief.ts); the newest row is the tile. */
  briefs: defineTable({
    question: v.string(),
    answer: v.optional(v.string()),
    citations: v.optional(v.array(v.string())),
    note: v.optional(v.string()),
    error: v.optional(v.string()),
    model: v.optional(v.string()),
    usage: v.optional(v.any()),
    generatedAt: v.number(),
    ms: v.number(),
  }).index("by_question", ["question", "generatedAt"]),
  syncRuns: defineTable({
    source: v.string(),
    startedAt: v.number(),
    finishedAt: v.number(),
    upserted: v.number(),
    removed: v.number(),
  }),
});
