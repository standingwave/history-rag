/* The dashboard's "Last day" tile: Ask's answer to a standing question,
   generated on a schedule (crons.ts) and on demand, stored so the tile
   reads a row instead of running a 5–40 s model loop on every open. */
import { v } from "convex/values";
import { query, action, internalAction, internalMutation, internalQuery } from "./_generated/server";
import { internal } from "./_generated/api";
import { requireUser, requireUserAction } from "./auth";
import { askCore } from "./ask";

export const QUESTION = "What did I do over the last day?";
const KEEP = 30;

export const generate = internalAction({
  args: { question: v.optional(v.string()) },
  handler: async (ctx, { question }) => {
    const q = question ?? QUESTION;
    const t0 = Date.now();
    const r = await askCore(ctx, { q, strict: true });
    await ctx.runMutation(internal.brief.put, {
      question: q, answer: r.answer, citations: r.citations, note: r.note, error: r.error,
      model: r.usage?.model, usage: r.usage, ms: Date.now() - t0,
    });
    return r.error ?? `ok ${Date.now() - t0} ms`;
  },
});

export const put = internalMutation({
  args: {
    question: v.string(), answer: v.optional(v.string()), citations: v.optional(v.array(v.string())),
    note: v.optional(v.string()), error: v.optional(v.string()), model: v.optional(v.string()),
    usage: v.optional(v.any()), ms: v.number(),
  },
  handler: async (ctx, a) => {
    await ctx.db.insert("briefs", { ...a, generatedAt: Date.now() });
    const old = await ctx.db.query("briefs").withIndex("by_question", (q) => q.eq("question", a.question))
      .order("desc").collect();
    for (const row of old.slice(KEEP)) await ctx.db.delete(row._id);
  },
});

/* Newest answered brief; the newest row's error rides along so the sheet
   can say a refresh failed without losing the last good answer. */
export const latest = query({
  args: { question: v.optional(v.string()) },
  handler: async (ctx, { question }) => {
    await requireUser(ctx);
    const q = question ?? QUESTION;
    const rows = await ctx.db.query("briefs").withIndex("by_question", (x) => x.eq("question", q))
      .order("desc").take(5);
    const good = rows.find((r) => r.answer);
    const newest = rows[0];
    return {
      question: q,
      answer: good?.answer ?? null, citations: good?.citations ?? [], note: good?.note ?? null,
      model: good?.model ?? null, generatedAt: good?.generatedAt ?? null, ms: good?.ms ?? null,
      lastError: newest && !newest.answer ? { error: newest.error ?? "no answer", at: newest.generatedAt } : null,
    };
  },
});

export const refresh = action({
  args: {},
  handler: async (ctx): Promise<string> => {
    await requireUserAction(ctx);
    return await ctx.runAction(internal.brief.generate, {});
  },
});

export const lastRun = internalQuery({
  args: {},
  handler: async (ctx) => (await ctx.db.query("briefs").order("desc").first())?.generatedAt ?? null,
});
