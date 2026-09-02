/* The MCP endpoint (wip/SPEC-convex-mcp.md): POST /mcp, bearer-checked
   against oauthTokens, JSON-RPC framed by mcpCore, four read tools mapped
   onto the same internals the app uses. Reads live items — fresher than
   the Lambda's S3 snapshot ever was. */
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { handleRpc, sha256b64url, WRITE_TOOLS, checkDay, resolveList } from "./mcpCore";
import { localDayHour, DEFAULT_TZ } from "./reminderMath";
import { derive } from "./timerMath";
import { vocabWords } from "./lists";

const asInt = (x: unknown, dflt: number) =>
  Number.isFinite(Number(x)) && Number(x) > 0 ? Math.floor(Number(x)) : dflt;
const s = (x: unknown) => (typeof x === "string" ? x : "");
const firstLine = (t: string) => t.split("\n", 1)[0].replace(/^Task: /, "");
const hhmm = (tz: string) => new Intl.DateTimeFormat("en-GB",
  { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date());

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
    // The timezone pref, fetched at most once per call.
    let tzP: Promise<string> | null = null;
    const tz = () => (tzP ??= ctx.runQuery(internal.reminders.prefInternal, { name: "timezone" })
      .then((v) => String(v ?? DEFAULT_TZ)));

    if (WRITE_TOOLS.has(name)) {
      const on = await ctx.runQuery(internal.reminders.prefInternal, { name: "mcpWrites" });
      if (on === false) throw new Error("actions are switched off in Oriel's settings");
    }

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
      /* ── action tools (wip/SPEC-llm-actions.md) ── */
      case "list_tasks": {
        const day = s(a.day)
          || (await ctx.runQuery(internal.today.latestTaskDayInternal, {}))
          || localDayHour(Date.now(), await tz()).day;
        const rows = await ctx.runQuery(internal.today.tasksInternal, { day });
        return { day, tasks: rows.map((r) => ({
          id: r.id, text: firstLine(r.text),
          done: !!(r.meta as { done?: boolean } | null)?.done, pending: r.pending })) };
      }
      case "create_task": {
        const today = localDayHour(Date.now(), await tz()).day;
        const day = s(a.day) ? checkDay(s(a.day), today) : today;
        if (!day) throw new Error(`bad day "${s(a.day)}" — local YYYY-MM-DD, yesterday to a year ahead`);
        await ctx.runMutation(internal.today.addInternal, { day, text: s(a.text) });
        return { queued: true, day, text: s(a.text).trim(),
                 ...(day !== today ? { note: "appears in the app on that day" } : {}) };
      }
      case "set_task": {
        if (typeof a.done !== "boolean") throw new Error("done must be true or false");
        const r = await ctx.runMutation(internal.today.toggleInternal,
          { id: s(a.id), want: a.done ? "done" : "open" });
        return r === null ? { queued: false, note: "already in that state" } : { queued: true };
      }
      case "edit_task": {
        const r = await ctx.runMutation(internal.today.editInternal,
          { id: s(a.id), newText: s(a.new_text) });
        return r === null ? { queued: false, note: "text unchanged" } : { queued: true };
      }
      case "delete_task":
        await ctx.runMutation(internal.today.removeInternal, { id: s(a.id) });
        return { queued: true };
      case "capture_note": {
        const zone = await tz();
        await ctx.runMutation(internal.today.captureInternal, {
          day: localDayHour(Date.now(), zone).day, text: s(a.text), at: hhmm(zone),
        });
        return { queued: true };
      }
      case "list_items": {
        const all = await ctx.runQuery(internal.lists.listsInternal, {});
        const r = resolveList(s(a.list), all);
        if ("error" in r) throw new Error(r.error);
        const got = await ctx.runQuery(internal.lists.itemsInternal, { path: r.hit.path });
        return { name: got.name, path: r.hit.path, words: got.words, items: got.items };
      }
      case "add_list_items": {
        const items = Array.isArray(a.items) ? a.items.map((x) => String(x)) : [];
        if (!items.length) throw new Error("items required");
        const all = await ctx.runQuery(internal.lists.listsInternal, {});
        const r = resolveList(s(a.list), all);
        if ("error" in r) throw new Error(r.error);
        const results = [];
        for (const text of items) {
          try {
            await ctx.runMutation(internal.lists.addItemInternal, { path: r.hit.path, text });
            results.push({ text, status: "queued" });
          } catch (e) {
            results.push({ text, status: String(e instanceof Error ? e.message : e) });
          }
        }
        return { list: r.hit.name, results };
      }
      case "create_list": {
        const name_ = s(a.name).trim();
        if (!name_) throw new Error("name required");
        const words = await vocabWords(name_);
        const path = await ctx.runMutation(internal.lists.createInternal, { name: name_, words });
        const items = Array.isArray(a.items) ? a.items.map((x) => String(x)) : [];
        const results = [];
        for (const text of items) {
          try {
            await ctx.runMutation(internal.lists.addItemInternal, { path, text });
            results.push({ text, status: "queued" });
          } catch (e) {
            results.push({ text, status: String(e instanceof Error ? e.message : e) });
          }
        }
        return { queued: true, path, words, ...(results.length ? { results } : {}) };
      }
      case "set_list_item": {
        const state = s(a.state);
        if (state !== "need" && state !== "got") throw new Error('state must be "need" or "got"');
        await ctx.runMutation(internal.lists.setStateInternal, { id: s(a.id), want: state });
        return { queued: true };
      }
      case "edit_list_item":
        await ctx.runMutation(internal.lists.editItemInternal,
          { id: s(a.id), newText: s(a.new_text) });
        return { queued: true };
      case "remove_list_item":
        await ctx.runMutation(internal.lists.removeItemInternal, { id: s(a.id) });
        return { queued: true };
      case "list_timers": {
        const rows = await ctx.runQuery(internal.timers.listInternal, {});
        const now = Date.now();
        return { timers: rows.map((r) => {
          const d = derive(r, now);
          return { id: r.id, label: r.label, state: d.st, remaining_ms: d.left,
                   up: !!r.up, repeat: !!r.repeat };
        }) };
      }
      case "start_timer": {
        const label = s(a.label).trim();
        if (!label) throw new Error("label required");
        const taskId = s(a.task_id);
        const up = a.up === true || !!taskId;
        const seconds = asInt(a.seconds, 0);
        if (!up && (seconds < 5 || seconds > 86400)) {
          throw new Error("a countdown needs seconds between 5 and 86400");
        }
        await ctx.runMutation(internal.timers.startInternal, {
          label, durationMs: up ? 0 : seconds * 1000,
          repeat: a.repeat === true || undefined, up: up || undefined,
          taskChunkId: taskId || undefined,
        });
        return { started: true, label, ...(up ? { stopwatch: true } : { seconds }) };
      }
      case "control_timer": {
        const op = s(a.op);
        const id = s(a.id) as Id<"timers">;
        if (op === "pause") await ctx.runMutation(internal.timers.pauseInternal, { id });
        else if (op === "resume") await ctx.runMutation(internal.timers.resumeInternal, { id });
        else if (op === "dismiss") await ctx.runMutation(internal.timers.dismissInternal, { id });
        else throw new Error('op must be "pause", "resume" or "dismiss"');
        return { ok: true, op };
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
