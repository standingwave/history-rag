/* Countdown timers (wip/SPEC-timers.md): pure Convex state, nothing on
   the Mac. Mutations compute times server-side so a skewed phone clock
   can't create a weird timer; the client derives the countdown locally. */
import { v } from "convex/values";
import { query, mutation } from "./_generated/server";
import { requireUser } from "./auth";
import { derive, MIN_MS, MAX_MS } from "./timerMath";

export const list = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    const rows = await ctx.db.query("timers").order("desc").collect();
    return rows.map((r) => ({
      id: r._id, label: r.label, durationMs: r.durationMs, repeat: r.repeat,
      endsAt: r.endsAt, remainingMs: r.remainingMs, startedAt: r.startedAt,
    }));
  },
});

export const start = mutation({
  args: { label: v.string(), durationMs: v.number(), repeat: v.optional(v.boolean()) },
  handler: async (ctx, { label, durationMs, repeat }) => {
    await requireUser(ctx);
    const dur = Math.round(Math.min(MAX_MS, Math.max(MIN_MS, durationMs)));
    await ctx.db.insert("timers", {
      label: label.trim().slice(0, 40),
      durationMs: dur,
      repeat: repeat || undefined,
      endsAt: Date.now() + dur,
      startedAt: Date.now(),
    });
  },
});

export const pause = mutation({
  args: { id: v.id("timers") },
  handler: async (ctx, { id }) => {
    await requireUser(ctx);
    const t = await ctx.db.get(id);
    if (!t || t.endsAt === undefined) return;
    const s = derive(t, Date.now());
    if (s.st === "done") return;   // a ringing one-shot is dismissed, not paused
    await ctx.db.patch(id, { endsAt: undefined, remainingMs: s.left });
  },
});

export const resume = mutation({
  args: { id: v.id("timers") },
  handler: async (ctx, { id }) => {
    await requireUser(ctx);
    const t = await ctx.db.get(id);
    if (!t || t.remainingMs === undefined) return;
    await ctx.db.patch(id, { endsAt: Date.now() + t.remainingMs, remainingMs: undefined });
  },
});

export const dismiss = mutation({
  args: { id: v.id("timers") },
  handler: async (ctx, { id }) => {
    await requireUser(ctx);
    if (await ctx.db.get(id)) await ctx.db.delete(id);
  },
});
