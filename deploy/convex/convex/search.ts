/* search_history over Convex. The range-filter compromise under test:
   `since`/`until` become an OR over local months at the vector index, then
   a post-filter on the item's local day. `dropped` counts candidates the
   post-filter discarded — measurement #2 in the spec. */
import { v } from "convex/values";
import { action, internalAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { embed } from "ai";
import { rag, QUERY_PROMPT, queryModel } from "./rag";
import { requireUserAction } from "./auth";
import type { Doc } from "./_generated/dataModel";

export type SearchHit = {
  id: string; source: string; score: number; timestamp: string; day: string;
  location: string; text: string; meta: unknown;
};
export type SearchResult = {
  results: SearchHit[]; candidates: number; dropped: number; months: string[];
  timing: { embedMs: number; searchMs: number; joinMs: number };
};

export const DEFAULT_SOURCES = ["tasks", "obsidian", "calendar"];

export function monthsBetween(since?: string, until?: string): string[] {
  if (!since && !until) return [];
  const lo = (since ?? until)!.slice(0, 7);
  const hi = (until ?? since)!.slice(0, 7);
  const out: string[] = [];
  let [y, m] = lo.split("-").map(Number);
  for (let guard = 0; guard < 240; guard++) {
    const cur = `${y}-${String(m).padStart(2, "0")}`;
    out.push(cur);
    if (cur >= hi) break;
    m++;
    if (m > 12) { m = 1; y++; }
  }
  return out;
}

export const search = action({
  args: {
    query: v.string(),
    sources: v.optional(v.array(v.string())),
    since: v.optional(v.string()),   // local YYYY-MM-DD, inclusive
    until: v.optional(v.string()),
    limit: v.optional(v.number()),
  },
  handler: async (ctx, a): Promise<SearchResult> => {
    await requireUserAction(ctx);
    const sources = a.sources?.length ? a.sources : DEFAULT_SOURCES;
    const limit = Math.min(a.limit ?? 10, 50);
    const months = monthsBetween(a.since, a.until);
    const filters = months.map((m) => ({ name: "month" as const, value: m }));
    // Over-fetch when windowed: the month filter is coarser than the window.
    const perNs = filters.length ? limit * 3 : limit;

    // Embed once; rag.search would otherwise call Mixedbread per namespace.
    let t = Date.now();
    const { embedding } = await embed({ model: queryModel, value: QUERY_PROMPT + a.query });
    const embedMs = Date.now() - t; t = Date.now();
    console.log(`search embed ${embedMs}ms len=${a.query.length}`);

    const perSource = await Promise.all(sources.map(async (ns) => {
      const { results, entries } = await rag.search(ctx, {
        namespace: ns,
        query: embedding,
        limit: perNs,
        filters: filters.length ? filters : undefined,
      });
      const key = new Map<string, string>();
      for (const e of entries) {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const ee = e as any;
        key.set(String(e.entryId), ee.key ?? ee.metadata?.chunkId ?? "");
      }
      const out: { chunkId: string; score: number; source: string }[] = [];
      for (const r of results) {
        const chunkId = key.get(String(r.entryId));
        if (chunkId) out.push({ chunkId, score: r.score, source: ns });
      }
      return out;
    }));
    const found = perSource.flat();
    const searchMs = Date.now() - t; t = Date.now();

    const items: Doc<"items">[] = await ctx.runQuery(
      internal.sync.itemsByChunkIds, { chunkIds: found.map((f) => f.chunkId) });
    const joinMs = Date.now() - t;
    const byId = new Map(items.map((i) => [i.chunkId, i]));
    let dropped = 0;
    const kept: SearchHit[] = [];
    for (const f of found) {
      const it = byId.get(f.chunkId);
      if (!it) continue;
      if ((a.since && it.day < a.since) || (a.until && it.day > a.until)) {
        dropped++;
        continue;
      }
      kept.push({
        id: it.chunkId, source: it.source, score: f.score,
        timestamp: it.timestamp, day: it.day, location: it.location,
        text: it.text, meta: it.meta,
      });
    }
    kept.sort((p, q) => q.score - p.score);
    return {
      results: kept.slice(0, limit),
      candidates: found.length,
      dropped,
      months,
      timing: { embedMs, searchMs, joinMs },
    };
  },
});

/* Cron target (see crons.ts). Logs the round-trip so the dashboard shows
   the embedding endpoint's latency distribution over time. */
export const warmEmbed = internalAction({
  args: {},
  handler: async (): Promise<number> => {
    const t = Date.now();
    // A novel string each time, so a response cache can't mask a slow model.
    await embed({ model: queryModel, value: `warm ${Date.now()}` });
    const ms = Date.now() - t;
    console.log(`warmEmbed ${ms}ms`);
    return ms;
  },
});
