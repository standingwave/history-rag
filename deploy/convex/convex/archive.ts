/* Stage 1 of wip/SPEC-convex-app.md: the archive (every source, on the
   Lambda's SQLite replica) reached through Convex so the app is the only
   UI. Each action forwards to the Lambda's JSON surface with the URL
   secret from env; the client never sees the Lambda. Replaced by native
   Convex queries in stage 3. */
import { v } from "convex/values";
import { action } from "./_generated/server";
import { requireUserAction } from "./auth";

function base(): string {
  const url = (process.env.LAMBDA_URL ?? "").replace(/\/$/, "");
  const secret = process.env.LAMBDA_SECRET ?? "";
  if (!url || !secret) throw new Error("archive not configured: set LAMBDA_URL and LAMBDA_SECRET");
  return `${url}/${secret}`;
}

type Params = Record<string, string | number | boolean | undefined>;

async function get(path: string, params: Params): Promise<any> {
  const qs = new URLSearchParams();
  for (const [k, val] of Object.entries(params)) {
    if (val === undefined || val === "" || val === false) continue;
    qs.set(k, val === true ? "1" : String(val));
  }
  const r = await fetch(`${base()}${path}?${qs}`);
  if (!r.ok) throw new Error(`archive ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

export const search = action({
  args: {
    query: v.string(),
    k: v.optional(v.number()),
    source: v.optional(v.string()),
    location: v.optional(v.string()),
    since: v.optional(v.string()),
    until: v.optional(v.string()),
    maxDistance: v.optional(v.number()),
  },
  handler: async (ctx, a): Promise<any> => {
    await requireUserAction(ctx);
    return get("/api/search", {
      q: a.query, k: a.k, source: a.source, location: a.location,
      since: a.since, until: a.until, max_distance: a.maxDistance,
    });
  },
});

export const listWindow = action({
  args: {
    since: v.optional(v.string()),
    until: v.optional(v.string()),
    source: v.optional(v.string()),
    location: v.optional(v.string()),
    limit: v.optional(v.number()),
    offset: v.optional(v.number()),
    groupBy: v.optional(v.string()),
    includeMeta: v.optional(v.boolean()),
    summaries: v.optional(v.boolean()),
  },
  handler: async (ctx, a): Promise<any> => {
    await requireUserAction(ctx);
    return get("/api/window", {
      since: a.since, until: a.until, source: a.source, location: a.location,
      limit: a.limit, offset: a.offset, group_by: a.groupBy,
      include_meta: a.includeMeta, summaries: a.summaries,
    });
  },
});

export const expand = action({
  args: { id: v.string(), context: v.optional(v.number()) },
  handler: async (ctx, a): Promise<any> => {
    await requireUserAction(ctx);
    return get("/api/expand", { id: a.id, context: a.context });
  },
});

export const config = action({
  args: {},
  handler: async (ctx): Promise<any> => {
    await requireUserAction(ctx);
    return get("/config", {});
  },
});

export const ask = action({
  args: { q: v.string(), model: v.optional(v.string()), strict: v.optional(v.boolean()) },
  handler: async (ctx, a): Promise<any> => {
    await requireUserAction(ctx);
    const r = await fetch(`${base()}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q: a.q, model: a.model ?? "", strict: !!a.strict }),
    });
    if (!r.ok) throw new Error(`archive ${r.status}: ${(await r.text()).slice(0, 200)}`);
    return r.json();
  },
});
