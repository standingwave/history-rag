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
import { rag, EMBED_DIM } from "./rag";
import { components } from "./_generated/api";
import { ALL_SOURCES } from "./search";
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

/* Per-source counters behind archive.stats. A delete can't shrink the
   day range exactly; rebuildStats fixes that whenever it matters. */
export async function bump(ctx: { db: any }, source: string, delta: number, day?: string) {
  if (day && !/^\d{4}-/.test(day)) day = undefined;   // sentinel days ("routine") aren't dates
  const row = await ctx.db.query("stats").withIndex("by_source", (q: any) => q.eq("source", source)).unique();
  if (!row) {
    if (delta > 0) await ctx.db.insert("stats", { source, count: delta, earliestDay: day ?? "", latestDay: day ?? "", updatedAt: Date.now() });
    return;
  }
  const patch: Record<string, unknown> = { count: Math.max(0, row.count + delta), updatedAt: Date.now() };
  if (day && delta > 0) {
    if (!row.earliestDay || day < row.earliestDay) patch.earliestDay = day;
    if (day > row.latestDay) patch.latestDay = day;
  }
  await ctx.db.patch(row._id, patch);
}

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
      if (existing) await ctx.db.patch(existing._id, { ...it, pending: false, hidden: false });
      else { await ctx.db.insert("items", { ...it, pending: false }); await bump(ctx, it.source, 1, it.day); }
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
    for (const id of ids) {
      const row = await ctx.db.get(id);
      if (!row) continue;
      await ctx.db.delete(id);
      await bump(ctx, row.source, -1);
    }
  },
});

/* Recount every source from `items` (paginated) and rewrite `stats`. */
export const statsPage = internalQuery({
  args: { cursor: v.union(v.string(), v.null()) },
  handler: async (ctx, { cursor }) => {
    const page = await ctx.db.query("items").paginate({ cursor, numItems: 4000 });
    const acc: Record<string, { n: number; lo: string; hi: string }> = {};
    for (const r of page.page) {
      const a = acc[r.source] ?? (acc[r.source] = { n: 0, lo: "", hi: "" });
      a.n++;
      if (!/^\d{4}-/.test(r.day)) continue;           // sentinel days aren't dates
      if (r.day && (!a.lo || r.day < a.lo)) a.lo = r.day;
      if (r.day > a.hi) a.hi = r.day;
    }
    return { acc, cursor: page.continueCursor, done: page.isDone };
  },
});
export const writeStats = internalMutation({
  args: { rows: v.array(v.object({ source: v.string(), count: v.number(), earliestDay: v.string(), latestDay: v.string() })) },
  handler: async (ctx, { rows }) => {
    for (const old of await ctx.db.query("stats").collect()) await ctx.db.delete(old._id);
    for (const r of rows) await ctx.db.insert("stats", { ...r, updatedAt: Date.now() });
  },
});
export const rebuildStats = internalAction({
  args: {},
  handler: async (ctx): Promise<Record<string, number>> => {
    const acc: Record<string, { n: number; lo: string; hi: string }> = {};
    for (let cursor: string | null = null; ;) {
      const p: { acc: typeof acc; cursor: string; done: boolean } = await ctx.runQuery(internal.sync.statsPage, { cursor });
      for (const [src, a] of Object.entries(p.acc)) {
        const t = acc[src] ?? (acc[src] = { n: 0, lo: "", hi: "" });
        t.n += a.n;
        if (a.lo && (!t.lo || a.lo < t.lo)) t.lo = a.lo;
        if (a.hi > t.hi) t.hi = a.hi;
      }
      if (p.done) break;
      cursor = p.cursor;
    }
    await ctx.runMutation(internal.sync.writeStats, { rows: Object.entries(acc).map(([source, a]) =>
      ({ source, count: a.n, earliestDay: a.lo, latestDay: a.hi })) });
    return Object.fromEntries(Object.entries(acc).map(([s, a]) => [s, a.n]));
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
export const itemsPage = internalQuery({
  args: { cursor: v.union(v.string(), v.null()) },
  handler: async (ctx, { cursor }) => {
    const page = await ctx.db.query("items")
      .paginate({ cursor, numItems: 2000 });
    return { n: page.page.length, cursor: page.continueCursor, done: page.isDone };
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
    let items = 0;
    for (let cursor: string | null = null; ;) {
      const p: { n: number; cursor: string; done: boolean } =
        await ctx.runQuery(internal.sync.itemsPage, { cursor });
      items += p.n;
      if (p.done) break;
      cursor = p.cursor;
    }
    out.items = items;
    return out;
  },
});

/* ── namespace versions ───────────────────────────────────────────────── */

/* A dimension change (EMBED_DIM) makes the component create a new
   namespace version per source; the old version's vectors stay until its
   entries are deleted. `namespaces` lists the
   versions; `gcReplacedNamespaces` deletes entries of replaced versions a
   page at a time, rescheduling itself until nothing is left. */
export const namespaces = internalAction({
  args: {},
  handler: async (ctx): Promise<any[]> => {
    const out: any[] = [];
    for (const ns of ALL_SOURCES) {
      const page: any = await ctx.runQuery(components.rag.namespaces.listNamespaceVersions,
        { namespace: ns, paginationOpts: { cursor: null, numItems: 20 } });
      for (const n of page.page) out.push({ namespace: ns, version: n.version, dimension: n.dimension,
        live: n.dimension === EMBED_DIM, namespaceId: n.namespaceId });
    }
    return out;
  },
});

export const gcReplacedNamespaces = internalAction({
  args: { deleted: v.optional(v.number()) },
  handler: async (ctx, { deleted = 0 }): Promise<{ deleted: number; done: boolean }> => {
    let n = 0;
    for (const ns of ALL_SOURCES) {
      const page: any = await ctx.runQuery(components.rag.namespaces.listNamespaceVersions,
        { namespace: ns, paginationOpts: { cursor: null, numItems: 20 } });
      // Old = any version whose dimension isn't the one the instance uses now.
      for (const old of page.page.filter((x: any) => x.dimension !== EMBED_DIM)) {
        for (const status of ["ready", "pending", "replaced"] as const) {
          const entries = await rag.list(ctx, { namespaceId: old.namespaceId, status, limit: 200 });
          for (const e of entries.page) { await rag.deleteAsync(ctx, { entryId: e.entryId }); n++; }
          if (n >= 400) break;
        }
        if (n >= 400) break;
      }
      if (n >= 400) break;
    }
    if (n > 0) {
      await ctx.scheduler.runAfter(2000, internal.sync.gcReplacedNamespaces, { deleted: deleted + n });
      return { deleted: deleted + n, done: false };
    }
    console.log(`gcReplacedNamespaces done: ${deleted} entries deleted`);
    return { deleted, done: true };
  },
});
