/* The Mac's push surface. All internal: tools/sync-convex.py calls these
   with the deploy key, nothing else can. One RAG entry per chunk, keyed by
   chunk id — adding under an existing key replaces the entry, and
   contentHash lets the component short-circuit an identical re-push. The
   `items` row is written after the vector so a search can always resolve
   the entry it found. */
import { v } from "convex/values";
import { internal } from "./_generated/api";
import {
  internalAction, internalMutation, internalQuery,
} from "./_generated/server";
import { rag } from "./rag";
import type { Doc } from "./_generated/dataModel";

const itemArg = v.object({
  chunkId: v.string(),
  source: v.string(),
  timestamp: v.string(),
  day: v.string(),
  month: v.string(),
  location: v.string(),
  text: v.string(),
  meta: v.any(),
  contentHash: v.string(),
  embedding: v.array(v.float64()),
  filterValues: v.array(v.object({ name: v.string(), value: v.string() })),
});

export const upsert = internalAction({
  args: { items: v.array(itemArg) },
  handler: async (ctx, { items }): Promise<{ chunkId: string; entryId: string }[]> => {
    // rag.add is a round-trip per chunk; sequential it ran ~8 chunks/s.
    const rows = await Promise.all(items.map(async (it) => {
      const { entryId } = await rag.add(ctx, {
        namespace: it.source,
        key: it.chunkId,
        contentHash: it.contentHash,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        filterValues: it.filterValues as any,
        metadata: { chunkId: it.chunkId },
        chunks: [{ text: it.text, embedding: it.embedding }],
      });
      const { embedding: _e, filterValues: _f, ...rest } = it;
      return { ...rest, entryId: String(entryId) };
    }));
    await ctx.runMutation(internal.sync.putItems, { items: rows });
    return rows.map((r) => ({ chunkId: r.chunkId, entryId: r.entryId }));
  },
});

export const putItems = internalMutation({
  args: {
    items: v.array(v.object({
      chunkId: v.string(), source: v.string(), timestamp: v.string(),
      day: v.string(), month: v.string(), location: v.string(),
      text: v.string(), meta: v.any(), contentHash: v.string(),
      entryId: v.string(),
    })),
  },
  handler: async (ctx, { items }) => {
    for (const it of items) {
      const existing = await ctx.db
        .query("items")
        .withIndex("by_chunkId", (q) => q.eq("chunkId", it.chunkId))
        .unique();
      if (existing) await ctx.db.patch(existing._id, { ...it, pending: false });
      else await ctx.db.insert("items", { ...it, pending: false });
    }
  },
});

export const itemsByChunkIds = internalQuery({
  args: { chunkIds: v.array(v.string()) },
  handler: async (ctx, { chunkIds }) => {
    const out = [];
    for (const id of chunkIds) {
      const row = await ctx.db
        .query("items")
        .withIndex("by_chunkId", (q) => q.eq("chunkId", id))
        .unique();
      if (row) out.push(row);
    }
    return out;
  },
});

export const remove = internalAction({
  args: { chunkIds: v.array(v.string()) },
  handler: async (ctx, { chunkIds }): Promise<number> => {
    const rows: Doc<"items">[] = await ctx.runQuery(
      internal.sync.itemsByChunkIds, { chunkIds });
    for (const row of rows) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      await rag.delete(ctx, { entryId: row.entryId as any });
    }
    await ctx.runMutation(internal.sync.deleteItems, {
      ids: rows.map((r) => r._id),
    });
    return rows.length;
  },
});

export const deleteItems = internalMutation({
  args: { ids: v.array(v.id("items")) },
  handler: async (ctx, { ids }) => {
    for (const id of ids) await ctx.db.delete(id);
  },
});

/* Entries superseded by a same-key add linger as status "replaced" until
   something deletes them. Called once at the end of every sync run. */
export const gcReplaced = internalAction({
  args: {},
  handler: async (ctx) => {
    let cursor: string | null = null;
    let deleted = 0;
    for (;;) {
      const page = await rag.list(ctx, {
        status: "replaced",
        paginationOpts: { cursor, numItems: 100 },
      });
      for (const e of page.page) {
        await rag.delete(ctx, { entryId: e.entryId });
        deleted++;
      }
      if (page.isDone) break;
      cursor = page.continueCursor;
    }
    return deleted;
  },
});

export const recordRun = internalMutation({
  args: {
    source: v.string(), startedAt: v.number(), finishedAt: v.number(),
    upserted: v.number(), removed: v.number(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("syncRuns", args);
  },
});

/* Diagnostic: what the RAG component holds per namespace and status, and
   how many `items` rows — for reconciling the dashboard's storage figures. */
export const itemsCount = internalQuery({
  args: {},
  handler: async (ctx) => {
    let n = 0;
    for await (const _ of ctx.db.query("items")) n++;
    return n;
  },
});

export const census = internalAction({
  args: {},
  handler: async (ctx): Promise<Record<string, number>> => {
    const out: Record<string, number> = {};
    for (const status of ["ready", "replaced", "pending"] as const) {
      let cursor: string | null = null;
      for (;;) {
        const page = await rag.list(ctx, { status, paginationOpts: { cursor, numItems: 500 } });
        for (const e of page.page) {
          const ns = (e as any).namespaceId ?? "?";
          out[`${status}:${ns}`] = (out[`${status}:${ns}`] ?? 0) + 1;
        }
        if (page.isDone) break;
        cursor = page.continueCursor;
      }
    }
    out.items = await ctx.runQuery(internal.sync.itemsCount, {});
    return out;
  },
});
