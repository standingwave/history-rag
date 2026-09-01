/* Morning digest (wip/SPEC-morning-digest.md): the day ahead — events,
   first meeting, tasks to carry — pushed once per local morning and shown
   on the dashboard's Digest tile. Facts are composed here; the tick that
   decides "is it morning in the pref's zone" lives in pushNode (full ICU). */
import { v } from "convex/values";
import { internalQuery, internalMutation } from "./_generated/server";
import { pref } from "./reminders";
import { eventTitle, DEFAULT_TZ } from "./reminderMath";

export const state = internalQuery({
  args: {},
  handler: async (ctx) => ({
    on: (await pref(ctx, "digestPush")) !== false,
    timezone: ((await pref(ctx, "timezone")) ?? DEFAULT_TZ) as string,
    sentDay: ((await pref(ctx, "digestSentDay")) ?? "") as string,
    subscribed: !!(await ctx.db.query("pushSubs").first()),
  }),
});

export const facts = internalQuery({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    const evs = (await ctx.db.query("items")
      .withIndex("by_source_day", (q) => q.eq("source", "calendar").eq("day", day))
      .collect()).filter((r) => !r.hidden);
    const allDay = evs.filter((r) => r.meta?.all_day);
    const timed = evs.filter((r) => !r.meta?.all_day)
      .sort((a, b) => String(a.meta?.start ?? a.timestamp)
        .localeCompare(String(b.meta?.start ?? b.timestamp)));
    const first = timed[0];

    // Today's note if it exists; otherwise the latest earlier day's open
    // tasks are exactly what "start day" would carry.
    let rows = (await ctx.db.query("items")
      .withIndex("by_source_day", (q) => q.eq("source", "tasks").eq("day", day))
      .collect()).filter((r) => !r.hidden);
    let tasksDay: string | undefined;
    if (!rows.length) {
      const latest = await ctx.db.query("items")
        .withIndex("by_source_timestamp", (q) => q.eq("source", "tasks"))
        .filter((q) => q.neq(q.field("day"), "routine"))
        .order("desc").first();
      if (latest && latest.day !== day) {
        tasksDay = latest.day;
        rows = (await ctx.db.query("items")
          .withIndex("by_source_day", (q) => q.eq("source", "tasks").eq("day", latest.day))
          .collect()).filter((r) => !r.hidden);
      }
    }
    const open = rows.filter((r) => !r.meta?.done && r.meta?.section !== "Routine");
    return {
      events: timed.length,
      allDay: allDay.length,
      firstStart: first ? String(first.meta?.start ?? first.timestamp) : undefined,
      firstTitle: first ? eventTitle(first.text) : undefined,
      tasksOpen: open.length,
      tasksDay,
    };
  },
});

export const markSent = internalMutation({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    const row = await ctx.db.query("prefs")
      .withIndex("by_name", (q) => q.eq("name", "digestSentDay")).unique();
    if (row) await ctx.db.patch(row._id, { value: day });
    else await ctx.db.insert("prefs", { name: "digestSentDay", value: day });
  },
});
