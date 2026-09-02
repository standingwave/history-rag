/* search_history over Convex. The range-filter compromise under test:
   `since`/`until` become an OR over local months at the vector index, then
   a post-filter on the item's local day. `dropped` counts candidates the
   post-filter discarded — measurement #2 in the spec. */
import { v } from "convex/values";
import { action, internalAction, type ActionCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import { rag, QUERY_PROMPT, embedQuery, EMBED_PROVIDER } from "./rag";
import { dayArg } from "./dates";
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

/* Every namespace the Mac pushes (index.py ALL_SOURCES). */
export const ALL_SOURCES = ["tasks", "obsidian", "calendar", "email", "browser",
  "claude", "git", "shell", "appusage", "digest"];

/* Exact days for a bounded window of ≤ MAX_DAY_FILTERS days; the vector
   index ORs them and nothing needs post-filtering. Longer or open-ended
   windows fall back to months plus a post-filter on the item's day. */
export const MAX_DAY_FILTERS = 31;
export function daysBetween(since?: string, until?: string): string[] {
  if (!since || !until) return [];
  const lo = new Date(since + "T12:00:00Z"), hi = new Date(until + "T12:00:00Z");
  const n = Math.round((hi.getTime() - lo.getTime()) / 864e5) + 1;
  if (n < 1 || n > MAX_DAY_FILTERS) return [];
  const out: string[] = [];
  for (let i = 0; i < n; i++) {
    out.push(new Date(lo.getTime() + i * 864e5).toISOString().slice(0, 10));
  }
  return out;
}

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

export type SearchArgs = {
  query: string; sources?: string[]; since?: string; until?: string; limit?: number;
};

export async function runSearch(ctx: ActionCtx, a: SearchArgs): Promise<SearchResult> {
    const sources = a.sources?.length ? a.sources : ALL_SOURCES;
    const limit = Math.min(a.limit ?? 10, 50);
    const since = dayArg(a.since), until = dayArg(a.until);
    const days = daysBetween(since, until);
    const months = days.length ? [] : monthsBetween(since, until);
    const filters = days.length
      ? days.map((d) => ({ name: "day" as const, value: d }))
      : months.map((m) => ({ name: "month" as const, value: m }));
    // Over-fetch only when the filter is coarser than the window.
    const perNs = months.length ? limit * 5 : limit;

    // Embed once; rag.search would otherwise call Mixedbread per namespace.
    let t = Date.now();
    const embedding = await embedQuery(QUERY_PROMPT + a.query);
    const embedMs = Date.now() - t; t = Date.now();
    console.log(`search embed ${embedMs}ms ${EMBED_PROVIDER} len=${a.query.length}`);

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
      if ((since && it.day < since) || (until && it.day > until)) {
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
    return runSearch(ctx, a);
  },
});

/* Same search without user auth, for the Mac's parity eval (deploy key). */
export const searchInternal = internalAction({
  args: {
    query: v.string(),
    sources: v.optional(v.array(v.string())),
    since: v.optional(v.string()),   // local YYYY-MM-DD, inclusive
    until: v.optional(v.string()),
    limit: v.optional(v.number()),
  },
  handler: async (ctx, a): Promise<SearchResult> => runSearch(ctx, a),
});

/* Cron target (see crons.ts). Logs the round-trip so the dashboard shows
   the embedding endpoint's latency distribution over time. */
export const warmEmbed = internalAction({
  args: {},
  handler: async (): Promise<number> => {
    const t = Date.now();
    // A novel string each time, so a response cache can't mask a slow model.
    await embedQuery(`warm ${Date.now()}`);
    const ms = Date.now() - t;
    console.log(`warmEmbed ${ms}ms ${EMBED_PROVIDER}`);
    return ms;
  },
});
