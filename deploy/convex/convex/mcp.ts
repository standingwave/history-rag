/* The MCP endpoint (wip/SPEC-convex-mcp.md): POST /mcp, bearer-checked
   against oauthTokens, JSON-RPC framed by mcpCore, four read tools mapped
   onto the same internals the app uses. Reads live items — fresher than
   the Lambda's S3 snapshot ever was. */
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { handleRpc, sha256b64url } from "./mcpCore";

const asInt = (x: unknown, dflt: number) =>
  Number.isFinite(Number(x)) && Number(x) > 0 ? Math.floor(Number(x)) : dflt;
const s = (x: unknown) => (typeof x === "string" ? x : "");

export const endpoint = httpAction(async (ctx, req) => {
  const auth = req.headers.get("Authorization") ?? "";
  const bearer = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  const okTok = bearer &&
    (await ctx.runQuery(internal.oauth.checkToken, { tokenHash: await sha256b64url(bearer) }));
  if (!okTok) {
    const o = new URL(req.url).origin;
    return new Response(JSON.stringify({ error: "unauthorized" }), {
      status: 401,
      headers: {
        "Content-Type": "application/json",
        "WWW-Authenticate":
          `Bearer resource_metadata="${o}/.well-known/oauth-protected-resource"`,
      },
    });
  }

  let rpc: unknown;
  try { rpc = await req.json(); } catch {
    return new Response(JSON.stringify(
      { jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }),
      { status: 200, headers: { "Content-Type": "application/json" } });
  }

  const out = await handleRpc(rpc as never, async (name, a) => {
    switch (name) {
      case "search_history": {
        const k = Math.min(asInt(a.k, 5), 50);
        const location = s(a.location);
        const r = await ctx.runAction(internal.search.searchInternal, {
          query: s(a.query),
          sources: s(a.source) ? [s(a.source)] : undefined,
          since: s(a.since) || undefined,
          until: s(a.until) || undefined,
          limit: location ? Math.min(k * 4, 100) : k,
        });
        const results = r.results
          .filter((h) => !location || (h.location ?? "").startsWith(location))
          .slice(0, k)
          .map((h, i) => ({
            rank: i + 1, id: h.id, source: h.source, score: Math.round(h.score * 1e4) / 1e4,
            text: h.text,
            ...(h.timestamp ? { timestamp: h.timestamp } : {}),
            ...(h.location ? { location: h.location } : {}),
            ...(h.meta && Object.keys(h.meta).length ? { meta: h.meta } : {}),
          }));
        return { query: s(a.query), count: results.length, results,
                 ...(s(a.since) || s(a.until)
                   ? { window: { since: s(a.since) || null, until: s(a.until) || null } } : {}) };
      }
      case "list_window": {
        const r = await ctx.runQuery(internal.archive.windowInternal, {
          since: s(a.since) || undefined,
          until: s(a.until) || undefined,
          source: s(a.source) || undefined,
          summaries: a.summaries === true,
          limit: Math.min(asInt(a.limit, 50), 200),
          cursor: s(a.cursor) || null,
        });
        return {
          count: r.results.length,
          window: r.window,
          results: r.results.map((x) => ({
            id: x.id, source: x.source, timestamp: x.timestamp, location: x.location,
            text: x.text.slice(0, 160) + (x.text.length > 160 ? "…" : ""),
          })),
          cursor: r.cursor, done: r.done,
        };
      }
      case "expand":
        return await ctx.runQuery(internal.archive.expandInternal, {
          id: s(a.id), context: asInt(a.context, 5),
        });
      case "history_stats": {
        const st = await ctx.runQuery(internal.archive.statsInternal, {});
        const sources: Record<string, { chunks: number; earliest: string; latest: string }> = {};
        let freshest = 0;
        for (const row of st.sources) {
          sources[row.source] = { chunks: row.count, earliest: row.earliestDay, latest: row.latestDay };
          freshest = Math.max(freshest, row.updatedAt);
        }
        return { total_chunks: st.total, sources,
                 ...(freshest ? { freshest_sync: new Date(freshest).toISOString() } : {}) };
      }
      default:
        throw new Error(`unhandled tool ${name}`);
    }
  });

  return out.body === null
    ? new Response(null, { status: out.status })
    : new Response(JSON.stringify(out.body),
        { status: out.status, headers: { "Content-Type": "application/json" } });
});

export const methodNotAllowed = httpAction(async () =>
  new Response(null, { status: 405, headers: { Allow: "POST" } }));
