/* The dashboard's reads and the one write. `tasks` is the subscribed
   query behind the Tasks widget; `toggle` flips a task optimistically and
   queues an intent for the Mac, which confirms through `applyIntent`
   (internal, deploy-key only) and, eventually, a re-push of the real
   state that clears `pending`. */
import { v } from "convex/values";
import {
  query, mutation, internalQuery, internalMutation,
} from "./_generated/server";
import { requireUser } from "./auth";
import { want } from "./schema";

function pub(row: {
  chunkId: string; source: string; timestamp: string; day: string;
  location: string; text: string; meta: unknown; pending?: boolean;
}) {
  const { chunkId, source, timestamp, day, location, text, meta, pending } = row;
  return { id: chunkId, source, timestamp, day, location, text, meta, pending: !!pending };
}

export const tasks = query({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    await requireUser(ctx);
    const rows = await ctx.db
      .query("items")
      .withIndex("by_source_day", (q) => q.eq("source", "tasks").eq("day", day))
      .collect();
    rows.sort((a, b) => (a.meta?.order ?? 0) - (b.meta?.order ?? 0));
    return rows.map(pub);
  },
});

/* The latest day that has any task — the empty state's label. */
export const latestTaskDay = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    const row = await ctx.db
      .query("items")
      .withIndex("by_source_timestamp", (q) => q.eq("source", "tasks"))
      .order("desc")
      .first();
    return row?.day ?? null;
  },
});

export const agenda = query({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    await requireUser(ctx);
    const rows = await ctx.db
      .query("items")
      .withIndex("by_source_day", (q) => q.eq("source", "calendar").eq("day", day))
      .collect();
    rows.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    return rows.map(pub);
  },
});

export const notes = query({
  args: { since: v.string(), limit: v.optional(v.number()) },
  handler: async (ctx, { since, limit }) => {
    await requireUser(ctx);
    const rows = await ctx.db
      .query("items")
      .withIndex("by_source_timestamp", (q) =>
        q.eq("source", "obsidian").gte("timestamp", since))
      .order("desc")
      .take(limit ?? 10);
    return rows.map(pub);
  },
});

export const counts = query({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    await requireUser(ctx);
    const out: Record<string, number> = {};
    for (const source of ["tasks", "calendar", "obsidian"]) {
      const rows = await ctx.db
        .query("items")
        .withIndex("by_source_day", (q) => q.eq("source", source).eq("day", day))
        .collect();
      out[source] = rows.length;
    }
    return out;
  },
});

export const toggle = mutation({
  args: { id: v.string() },
  handler: async (ctx, { id }) => {
    await requireUser(ctx);
    const row = await ctx.db
      .query("items")
      .withIndex("by_chunkId", (q) => q.eq("chunkId", id))
      .unique();
    if (!row || row.source !== "tasks") throw new Error("not a task");
    const done = !!row.meta?.done;
    await ctx.db.patch(row._id, {
      meta: { ...row.meta, done: !done },
      pending: true,
    });
    const text = row.text.split("\n", 1)[0].replace(/^Task: /, "");
    return await ctx.db.insert("taskIntents", {
      chunkId: id,
      day: row.day,
      vault: String(row.meta?.vault ?? ""),
      text,
      want: done ? "open" : "done",
      requestedAt: Date.now(),
    });
  },
});

/* Recent intents for the UI: pending ones and errors worth showing. */
export const intents = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    const rows = await ctx.db.query("taskIntents").order("desc").take(20);
    return rows.map((r) => ({
      id: r._id, chunkId: r.chunkId, want: r.want, requestedAt: r.requestedAt,
      appliedAt: r.appliedAt ?? null, error: r.error ?? null,
    }));
  },
});

/* ── the Mac's side (deploy key) ── */

export const pendingIntents = internalQuery({
  args: {},
  handler: async (ctx) => {
    const rows = await ctx.db
      .query("taskIntents")
      .withIndex("by_appliedAt", (q) => q.eq("appliedAt", undefined))
      .collect();
    return rows.map((r) => ({
      id: r._id, chunkId: r.chunkId, day: r.day, vault: r.vault,
      text: r.text, want: r.want, requestedAt: r.requestedAt,
    }));
  },
});

export const applyIntent = internalMutation({
  args: { id: v.id("taskIntents"), error: v.optional(v.string()) },
  handler: async (ctx, { id, error }) => {
    const intent = await ctx.db.get(id);
    if (!intent) return;
    await ctx.db.patch(id, { appliedAt: Date.now(), error });
    if (error) {
      // Revert the optimistic flip; the vault didn't change.
      const row = await ctx.db
        .query("items")
        .withIndex("by_chunkId", (q) => q.eq("chunkId", intent.chunkId))
        .unique();
      if (row) {
        await ctx.db.patch(row._id, {
          meta: { ...row.meta, done: intent.want === "open" },
          pending: false,
        });
      }
    }
  },
});

export { want };
