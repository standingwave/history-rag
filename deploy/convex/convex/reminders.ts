/* Event reminders (wip/SPEC-event-reminders.md): a 5-minute cron sweeps
   upcoming calendar items and pushes "⏰ 13:30 Dentist / in 10 min"
   through the timers' Web Push pipeline. Fires entirely from Convex — an
   asleep Mac only delays new events reaching the index, never reminders
   for events already synced. prefs gates the feature (D3) and holds the
   timezone the wall-clock text renders in (D4). */
import { v } from "convex/values";
import { query, mutation, internalQuery, internalMutation, type QueryCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import { requireUser } from "./auth";
import { due, eventTitle, LEAD_MS, SWEEP_MS, DEFAULT_TZ } from "./reminderMath";

export async function pref(ctx: QueryCtx, name: string) {
  const row = await ctx.db.query("prefs")
    .withIndex("by_name", (q) => q.eq("name", name)).unique();
  return row?.value;
}

export const prefInternal = internalQuery({
  args: { name: v.string() },
  handler: (ctx, { name }) => pref(ctx, name),
});

export const status = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    return {
      remindEvents: (await pref(ctx, "remindEvents")) !== false,
      digestPush: (await pref(ctx, "digestPush")) !== false,
      mcpWrites: (await pref(ctx, "mcpWrites")) !== false,
      timezone: (await pref(ctx, "timezone")) ?? DEFAULT_TZ,
      subscribed: !!(await ctx.db.query("pushSubs").first()),
    };
  },
});

export const setPref = mutation({
  args: {
    name: v.union(v.literal("remindEvents"), v.literal("timezone"), v.literal("digestPush"),
      v.literal("mcpWrites")),
    value: v.any(),
  },
  handler: async (ctx, { name, value }) => {
    await requireUser(ctx);
    const row = await ctx.db.query("prefs")
      .withIndex("by_name", (q) => q.eq("name", name)).unique();
    if (row) await ctx.db.patch(row._id, { value });
    else await ctx.db.insert("prefs", { name, value });
  },
});

export const sweep = internalMutation({
  args: {},
  handler: async (ctx) => {
    if ((await pref(ctx, "remindEvents")) === false) return;
    if (!(await ctx.db.query("pushSubs").first())) return;
    const now = Date.now();
    /* UTC ISO range on the same index the dashboard's `upcoming` uses;
       leading slack catches meta.start drifting from the indexed
       timestamp, trailing slack covers the cron interval. */
    const lo = new Date(now - 2 * SWEEP_MS).toISOString();
    const hi = new Date(now + LEAD_MS + SWEEP_MS + 60_000).toISOString();
    const rows = await ctx.db.query("items")
      .withIndex("by_source_timestamp",
        (q) => q.eq("source", "calendar").gt("timestamp", lo).lt("timestamp", hi))
      .collect();
    const tz = (await pref(ctx, "timezone")) ?? DEFAULT_TZ;
    for (const r of rows) {
      if (r.hidden || r.meta?.all_day) continue;
      const start = String(r.meta?.start ?? r.timestamp);
      const startMs = Date.parse(start);
      if (!Number.isFinite(startMs) || !due(startMs, now)) continue;
      // One reminder per (event, start): a moved event reminds again.
      const sent = await ctx.db.query("eventReminders")
        .withIndex("by_chunk_start",
          (q) => q.eq("chunkId", r.chunkId).eq("start", start)).unique();
      if (sent) continue;
      await ctx.db.insert("eventReminders", { chunkId: r.chunkId, start, sentAt: now });
      await ctx.scheduler.runAfter(0, internal.pushNode.sendEvent, {
        start, title: eventTitle(r.text), tz, tag: r.chunkId, url: "/#w=agenda",
      });
    }
    for (const o of await ctx.db.query("eventReminders").collect()) {
      if (now - o.sentAt > 48 * 3600_000) await ctx.db.delete(o._id);
    }
  },
});
