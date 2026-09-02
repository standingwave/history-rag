/* Countdown timers and stopwatches (wip/SPEC-timers.md): pure Convex
   state, nothing on the Mac. Mutations compute times server-side so a
   skewed phone clock can't create a weird timer; the client derives the
   countdown locally. Every running countdown carries a scheduled
   push.timerFired at its cycle end (alarmId) so lock-screen alerts fire
   without anything polling. */
import { v } from "convex/values";
import {
  query, mutation, internalQuery, internalMutation,
  type QueryCtx, type MutationCtx,
} from "./_generated/server";
import { internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { requireUser } from "./auth";
import { derive, MIN_MS, MAX_MS } from "./timerMath";
import { attachNote } from "./today";

async function arm(ctx: MutationCtx, id: Id<"timers">, endsAt: number) {
  return await ctx.scheduler.runAt(endsAt, internal.push.timerFired, { id, expected: endsAt });
}
async function disarm(ctx: MutationCtx, t: Doc<"timers">) {
  if (t.alarmId) await ctx.scheduler.cancel(t.alarmId).catch(() => {});
}

async function listH(ctx: QueryCtx) {
  const rows = await ctx.db.query("timers").order("desc").collect();
  return rows.map((r) => ({
    id: r._id, label: r.label, durationMs: r.durationMs, repeat: r.repeat,
    up: r.up, endsAt: r.endsAt, remainingMs: r.remainingMs, startedAt: r.startedAt,
    taskChunkId: r.taskChunkId,
  }));
}
export const list = query({
  args: {},
  handler: async (ctx) => { await requireUser(ctx); return listH(ctx); },
});
export const listInternal = internalQuery({ args: {}, handler: (ctx) => listH(ctx) });

const startArgs = {
  label: v.string(), durationMs: v.number(),
  repeat: v.optional(v.boolean()), up: v.optional(v.boolean()),
  taskChunkId: v.optional(v.string()),
};
type StartArgs = {
  label: string; durationMs: number; repeat?: boolean; up?: boolean; taskChunkId?: string;
};
async function startH(ctx: MutationCtx, { label, durationMs, repeat, up, taskChunkId }: StartArgs) {
    const now = Date.now();
    const clean = label.trim().slice(0, 40);
    if (up) {
      await ctx.db.insert("timers", {
        label: clean, durationMs: 0, up: true, endsAt: now, startedAt: now,
        taskChunkId: taskChunkId || undefined,
      });
      return;
    }
    const dur = Math.round(Math.min(MAX_MS, Math.max(MIN_MS, durationMs)));
    const id = await ctx.db.insert("timers", {
      label: clean, durationMs: dur, repeat: repeat || undefined,
      endsAt: now + dur, startedAt: now,
    });
    await ctx.db.patch(id, { alarmId: await arm(ctx, id, now + dur) });
}
export const start = mutation({
  args: startArgs,
  handler: async (ctx, a) => { await requireUser(ctx); return startH(ctx, a); },
});
export const startInternal = internalMutation({
  args: startArgs, handler: (ctx, a) => startH(ctx, a),
});

async function pauseH(ctx: MutationCtx, id: Id<"timers">) {
  const t = await ctx.db.get(id);
  if (!t || t.endsAt === undefined) return;
  const s = derive(t, Date.now());
  if (s.st === "done") return;   // a ringing one-shot is dismissed, not paused
  await disarm(ctx, t);
  await ctx.db.patch(id, { endsAt: undefined, remainingMs: s.left, alarmId: undefined });
}
export const pause = mutation({
  args: { id: v.id("timers") },
  handler: async (ctx, { id }) => { await requireUser(ctx); return pauseH(ctx, id); },
});
export const pauseInternal = internalMutation({
  args: { id: v.id("timers") }, handler: (ctx, { id }) => pauseH(ctx, id),
});

async function resumeH(ctx: MutationCtx, id: Id<"timers">) {
  const t = await ctx.db.get(id);
  if (!t || t.remainingMs === undefined) return;
  const now = Date.now();
  if (t.up) {
    await ctx.db.patch(id, { endsAt: now - t.remainingMs, remainingMs: undefined });
    return;
  }
  const endsAt = now + t.remainingMs;
  await ctx.db.patch(id, {
    endsAt, remainingMs: undefined, alarmId: await arm(ctx, id, endsAt),
  });
}
export const resume = mutation({
  args: { id: v.id("timers") },
  handler: async (ctx, { id }) => { await requireUser(ctx); return resumeH(ctx, id); },
});
export const resumeInternal = internalMutation({
  args: { id: v.id("timers") }, handler: (ctx, { id }) => resumeH(ctx, id),
});

async function dismissH(ctx: MutationCtx, id: Id<"timers">) {
  const t = await ctx.db.get(id);
  if (!t) return;
  // A focus stopwatch logs its time to the task before it goes; a lost
  // task or a still-pending row skips the log rather than blocking stop.
  if (t.up && t.taskChunkId) {
    const mins = Math.round(derive(t, Date.now()).left / 60_000);
    if (mins >= 1) {
      await attachNote(ctx, t.taskChunkId, `⏱ focused ${mins} min`).catch(() => {});
    }
  }
  await disarm(ctx, t);
  await ctx.db.delete(id);
}
export const dismiss = mutation({
  args: { id: v.id("timers") },
  handler: async (ctx, { id }) => { await requireUser(ctx); return dismissH(ctx, id); },
});
export const dismissInternal = internalMutation({
  args: { id: v.id("timers") }, handler: (ctx, { id }) => dismissH(ctx, id),
});
