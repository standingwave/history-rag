/* The archive's non-vector reads, native (wip/SPEC-convex-app.md stage 3):
   `window` is list_window over `items` day ranges, `expand` is the
   reading view — the chunk plus neighbours reconstructed from `items`
   the way the Mac's expand falls back when a live store is absent —
   and `config` lists the Ask presets. Public variants require the user;
   `*Internal` ones serve the Ask action's tool calls. */
import { v } from "convex/values";
import { query, internalQuery, type QueryCtx } from "./_generated/server";
import { requireUser } from "./auth";
import { dayArg } from "./dates";
import type { Doc } from "./_generated/dataModel";

export function pub(row: Doc<"items">) {
  const { chunkId, source, timestamp, day, location, text, meta, pending } = row;
  return { id: chunkId, source, timestamp, day, location, text, meta, pending: !!pending };
}
type Pub = ReturnType<typeof pub>;

/* ── window ───────────────────────────────────────────────────────────── */

const SUMMARY_SOURCES = ["digest", "appusage"];

/* Newest local day first; within a day the summary tier (appusage
   day-shape, then digests) leads, then raw chunks newest-first. */
function tier(r: Doc<"items">) {
  if (r.source === "appusage" && r.meta?.first != null) return 0;
  if (r.source === "digest") return 1;
  return 2;
}
function orderPage(rows: Doc<"items">[]) {
  rows.sort((a, b) => b.day.localeCompare(a.day) || tier(a) - tier(b)
    || b.timestamp.localeCompare(a.timestamp));
  return rows;
}

export type WindowArgs = {
  since?: string; until?: string; source?: string; summaries?: boolean;
  limit?: number; cursor?: string | null;
};
export type WindowResult = {
  results: Pub[]; cursor: string | null; done: boolean;
  window: { since: string | null; until: string | null };
};

export async function windowCore(ctx: QueryCtx, a: WindowArgs): Promise<WindowResult> {
  const sinceDay = dayArg(a.since), untilDay = dayArg(a.until);
  if (!sinceDay && !untilDay) throw new Error("window requires since and/or until");
  const since = sinceDay ?? "0000-00-00", until = untilDay ?? "9999-99-99";
  const limit = Math.max(1, Math.min(a.limit ?? 50, 200));
  const window = { since: sinceDay ?? null, until: untilDay ?? null };
  if (a.summaries) {
    // The summary tier is a few rows per day; collect it whole.
    const rows: Doc<"items">[] = [];
    for (const src of a.source ? [a.source] : SUMMARY_SOURCES) {
      const got = await ctx.db.query("items")
        .withIndex("by_source_day", (q) => q.eq("source", src).gte("day", since).lte("day", until))
        .order("desc").take(2000);
      rows.push(...got.filter((r) => r.source !== "appusage" || r.meta?.first != null));
    }
    return { results: orderPage(rows).slice(0, limit).map(pub), cursor: null, done: true, window };
  }
  const q = a.source
    ? ctx.db.query("items").withIndex("by_source_day", (x) => x.eq("source", a.source!).gte("day", since).lte("day", until))
    : ctx.db.query("items").withIndex("by_day", (x) => x.gte("day", since).lte("day", until));
  const page = await q.order("desc").paginate({ cursor: a.cursor ?? null, numItems: limit });
  return { results: orderPage(page.page).map(pub), cursor: page.isDone ? null : page.continueCursor,
           done: page.isDone, window };
}

const windowArgs = {
  since: v.optional(v.string()), until: v.optional(v.string()),
  source: v.optional(v.string()), summaries: v.optional(v.boolean()),
  limit: v.optional(v.number()), cursor: v.optional(v.union(v.string(), v.null())),
};
export const window = query({
  args: windowArgs,
  handler: async (ctx, a): Promise<WindowResult> => { await requireUser(ctx); return windowCore(ctx, a); },
});
export const windowInternal = internalQuery({
  args: windowArgs,
  handler: async (ctx, a): Promise<WindowResult> => windowCore(ctx, a),
});

/* ── expand ───────────────────────────────────────────────────────────── */

export type ExpandResult = {
  chunk: Pub | null; context: unknown; context_source: "index" | null; error?: string;
};

async function sameDay(ctx: QueryCtx, source: string, day: string, cap = 3000) {
  return ctx.db.query("items")
    .withIndex("by_source_day", (q) => q.eq("source", source).eq("day", day))
    .take(cap);
}

export async function expandCore(ctx: QueryCtx, id: string, n: number): Promise<ExpandResult> {
  const row = await ctx.db.query("items").withIndex("by_chunkId", (q) => q.eq("chunkId", id)).unique();
  if (!row) return { chunk: null, context: null, context_source: null, error: `no chunk with id ${id}` };
  const chunk = pub(row);
  const m = row.meta ?? {};
  const mark = (r: Doc<"items">) => ({ ...pub(r), target: r.chunkId === id });
  let context: unknown = null;
  switch (row.source) {
    case "claude": {
      const rows = (await sameDay(ctx, "claude", row.day))
        .filter((r) => r.meta?.session_id === m.session_id)
        .sort((a, b) => (a.meta?.lineno ?? 0) - (b.meta?.lineno ?? 0));
      const i = rows.findIndex((r) => r.chunkId === id);
      context = { turns: rows.slice(Math.max(0, i - n), i + n + 1).map((r) => ({
        id: r.chunkId, lineno: r.meta?.lineno, role: r.meta?.role, timestamp: r.timestamp,
        text: r.text.slice(0, 2000), target: r.chunkId === id })) };
      break;
    }
    case "browser": {
      const rows = (await sameDay(ctx, "browser", row.day))
        .filter((r) => r.location === row.location)
        .sort((a, b) => a.timestamp.localeCompare(b.timestamp));
      context = { day: row.day, profile: row.location,
        visits: rows.map((r) => ({ id: r.chunkId, timestamp: r.timestamp, text: r.text.slice(0, 160), target: r.chunkId === id })) };
      break;
    }
    case "calendar": {
      const rows = (await sameDay(ctx, "calendar", row.day)).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
      context = { day: row.day, agenda: rows.map((r) => ({ id: r.chunkId, timestamp: r.timestamp, text: r.text.slice(0, 300), target: r.chunkId === id })) };
      break;
    }
    case "obsidian": {
      const path = String(m.path ?? row.location.split("#")[0]);
      const rows = await ctx.db.query("items")
        .withIndex("by_source_location", (q) => q.eq("source", "obsidian").gte("location", path).lt("location", path + "￿"))
        .take(500);
      context = { sections: rows.map((r) => ({ id: r.chunkId, location: r.location, text: r.text, target: r.chunkId === id })) };
      break;
    }
    case "tasks": {
      const day = String(m.last_seen ?? row.day);
      const rows = (await sameDay(ctx, "tasks", day)).sort((a, b) => (a.meta?.order ?? 0) - (b.meta?.order ?? 0));
      context = { day, tasks: rows.map((r) => ({ id: r.chunkId, text: r.text.split("\n", 1)[0].replace(/^Task: /, ""),
        done: r.meta?.done, section: r.meta?.section ?? "", subtasks: r.meta?.subtasks ?? [],
        attachments: r.meta?.attachments ?? [], target: r.chunkId === id })) };
      break;
    }
    case "appusage": {
      const rows = await sameDay(ctx, "appusage", row.day);
      const by: Record<string, number> = {};
      for (const r of rows) if (r.meta?.app && r.meta?.first == null) by[r.meta.app] = r.meta.seconds ?? 0;
      context = { date: row.day, seconds_by_app: by };
      break;
    }
    case "email": {
      const rows = (await sameDay(ctx, "email", row.day)).filter((r) => r.meta?.msgid && r.meta.msgid === m.msgid);
      context = rows.length > 1 ? { message: rows.map(mark) } : null;
      break;
    }
    case "digest":
      context = { day: m.date ?? row.day, digest_of: m.digest_of ?? "", rollup: m };
      break;
    default:
      context = null;   // git, shell: the live stores aren't here
  }
  return { chunk, context, context_source: context ? "index" : null };
}

export const expand = query({
  args: { id: v.string(), context: v.optional(v.number()) },
  handler: async (ctx, a): Promise<ExpandResult> => {
    await requireUser(ctx);
    return expandCore(ctx, a.id, Math.max(0, Math.min(a.context ?? 5, 25)));
  },
});
export const expandInternal = internalQuery({
  args: { id: v.string(), context: v.optional(v.number()) },
  handler: async (ctx, a): Promise<ExpandResult> => expandCore(ctx, a.id, Math.max(0, Math.min(a.context ?? 5, 25))),
});

/* ── ask presets ──────────────────────────────────────────────────────── */

export type Preset = {
  name: string; model: string; backend?: string; base_url?: string;
  key_env?: string; max_tokens?: number; latency?: string; est_cost?: string;
  parse?: boolean;   // the command parser's preset (wip/SPEC-llm-actions.md)
};

/* ASK_MODELS is the same JSON the Lambda takes in CLAUDE_RAG_ASK_MODELS;
   a preset whose key_env isn't set on this deployment is left out. */
export function presets(): (Preset & { _key: string })[] {
  let raw: unknown = [];
  try { raw = JSON.parse(process.env.ASK_MODELS ?? "[]"); } catch { return []; }
  const out: (Preset & { _key: string })[] = [];
  for (const m of (raw as Preset[]) ?? []) {
    if (!m?.name || !m?.model) continue;
    const key = m.key_env ? (process.env[m.key_env] ?? "") : "";
    if (m.key_env && !key) continue;
    out.push({ ...m, _key: key });
  }
  return out;
}

export const config = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    return { models: presets().map(({ name, latency, est_cost }) =>
      ({ name, ...(latency ? { latency } : {}), ...(est_cost ? { est_cost } : {}) })) };
  },
});

/* ── history_stats ────────────────────────────────────────────────────── */

async function statsCore(ctx: QueryCtx) {
  const rows = await ctx.db.query("stats").collect();
  rows.sort((a, b) => b.count - a.count);
  return { total: rows.reduce((n, r) => n + r.count, 0),
           sources: rows.map(({ source, count, earliestDay, latestDay, updatedAt }) =>
             ({ source, count, earliestDay, latestDay, updatedAt })) };
}

export const stats = query({
  args: {},
  handler: async (ctx) => { await requireUser(ctx); return statsCore(ctx); },
});
export const statsInternal = internalQuery({
  args: {},
  handler: (ctx) => statsCore(ctx),
});
