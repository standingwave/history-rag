/* Web Push for the timers' lock-screen alerts (wip/SPEC-timers.md v2).
   The phone subscribes once (pushSubs); timers:start schedules timerFired
   at each cycle end, which verifies the timer is still on that schedule
   and fans the notification out through pushNode:send. */
import { v } from "convex/values";
import { query, mutation, internalQuery, internalMutation } from "./_generated/server";
import { internal } from "./_generated/api";
import { requireUser } from "./auth";
import { durLabel } from "./timerMath";

export const vapidPublicKey = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    return process.env.VAPID_PUBLIC_KEY ?? "";
  },
});

export const subscribe = mutation({
  args: {
    endpoint: v.string(),
    keys: v.object({ p256dh: v.string(), auth: v.string() }),
    ua: v.optional(v.string()),
  },
  handler: async (ctx, { endpoint, keys, ua }) => {
    await requireUser(ctx);
    const old = await ctx.db.query("pushSubs")
      .withIndex("by_endpoint", (q) => q.eq("endpoint", endpoint)).unique();
    if (old) await ctx.db.patch(old._id, { keys, ua });
    else await ctx.db.insert("pushSubs", { endpoint, keys, ua });
  },
});

export const subs = internalQuery({
  args: {},
  handler: (ctx) => ctx.db.query("pushSubs").collect(),
});

/* Push services answer 404/410 for a subscription that no longer exists
   (app removed from the home screen, permission revoked). */
export const prune = internalMutation({
  args: { endpoint: v.string() },
  handler: async (ctx, { endpoint }) => {
    const row = await ctx.db.query("pushSubs")
      .withIndex("by_endpoint", (q) => q.eq("endpoint", endpoint)).unique();
    if (row) await ctx.db.delete(row._id);
  },
});

/* Scheduled at a cycle end. Pause/dismiss cancel the alarm and resume
   re-arms it, but a stale run can still race in — so it only fires when
   `expected` matches the timer's current schedule. Repeats re-arm
   themselves for the next cycle. */
export const timerFired = internalMutation({
  args: { id: v.id("timers"), expected: v.number() },
  handler: async (ctx, { id, expected }) => {
    const t = await ctx.db.get(id);
    if (!t || t.up || t.endsAt === undefined) return;
    const onSchedule = expected === t.endsAt ||
      (!!t.repeat && expected > t.endsAt && (expected - t.endsAt) % t.durationMs === 0);
    if (!onSchedule) return;
    const what = t.label || durLabel(t.durationMs);
    await ctx.scheduler.runAfter(0, internal.pushNode.send, {
      title: t.repeat ? `↻ ${what}` : `⏰ ${what} — done`,
      body: t.repeat ? `every ${durLabel(t.durationMs)}` : "tap to open Oriel",
      tag: String(id),
    });
    if (t.repeat) {
      const next = expected + t.durationMs;
      const alarmId = await ctx.scheduler.runAt(next, internal.push.timerFired, { id, expected: next });
      await ctx.db.patch(id, { alarmId });
    }
  },
});
