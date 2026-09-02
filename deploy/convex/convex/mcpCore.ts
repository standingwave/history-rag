/* MCP protocol core (wip/SPEC-convex-mcp.md): stateless JSON-RPC over
   one POST — the same wire contract the Lambda's FastMCP spoke
   (stateless_http + json_response). Pure module so the framing is
   node-testable; convex/mcp.ts supplies the tool dispatch. */

export const PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

export type ToolDef = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations?: { readOnlyHint?: boolean; destructiveHint?: boolean };
};

const RO = { readOnlyHint: true };
const WRITE = { readOnlyHint: false, destructiveHint: false };
const DESTRUCTIVE = { readOnlyHint: false, destructiveHint: true };
/* Rides along in every write tool's description so the model can set the
   user's expectation honestly. */
const QUEUED = " The change queues as a pending intent; the user's Mac " +
  "applies it to their vault within minutes and the app shows it immediately.";

/* Descriptions follow server.py's (claude.ai's tool choice depends on
   them), adjusted where this backend honestly differs: similarity score
   instead of L2 distance, cursor paging instead of offset, local-day
   bounds only. */
export const TOOLS: ToolDef[] = [
  {
    name: "search_history",
    description: `Semantic search over the user's own history. Prefer this over guessing when a question refers to something they did, decided, ran, or used before. One shared index spans: claude (past Claude Code conversation turns), shell (working-session chunks: cwd, time span, command sequence), appusage (daily per-app time on their Mac), browser (pages visited; search-engine queries are chunks reading 'Searched <site> for "<terms>"'), git (commit messages across local repos), obsidian (vault notes chunked by heading), tasks (daily-note checkbox tasks), calendar (events, all past plus ~90 days ahead), email (envelopes + bodies), digest (precomputed daily rollups — for "what did I do <day/week>" questions list these first via list_window source='digest').

Args: query (natural-language description of what to recall); k = max results (default 5); source restricts to one source name; location is a case-sensitive prefix filter on each chunk's location (e.g. 'chrome:' for a browser profile, a repo name for git, a folder for obsidian); since/until are the user's LOCAL calendar days as bare dates ('2026-07-02'), inclusive.

Returns JSON {query, count, results[]} ranked best-first; each result has rank (1=best), id, source, score (similarity, HIGHER = better; strong matches run ~0.7+), text, and — when present — timestamp, location, meta. Results are pointers: pass an id to expand() for the full chunk plus surrounding context. For an exhaustive chronological listing of a window use list_window — this tool ranks by relevance, not completeness. If ~/.claude/history-rag-instructions.md exists in your context, follow it when presenting.`,
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string" },
        k: { type: "integer", default: 5 },
        source: { type: "string", default: "" },
        location: { type: "string", default: "" },
        since: { type: "string", default: "" },
        until: { type: "string", default: "" },
      },
      required: ["query"],
    },
    annotations: RO,
  },
  {
    name: "list_window",
    description: `Exhaustive listing of everything in a time window — no semantic ranking, no sampling. Newest local day first; within each day summary chunks lead (appusage day-shape, then digests), then raw chunks newest-first. The right tool for "everything from <day/week>"; use search_history when relevance matters more than completeness. since/until are the user's local calendar days (bare dates, inclusive); at least one is required. Results are compact pointers {id, source, timestamp, location, text (truncated)} — pass an id to expand() to read one in full. Responses page by cursor: pass the returned cursor back to continue; done=true means the window is exhausted. summaries=true keeps only the summary tier (digests and appusage day-shape) — the "what happened, day by day" diary view. For day/week activity summaries read source='digest' first and drill into raw chunks only where a digest points.`,
    inputSchema: {
      type: "object",
      properties: {
        since: { type: "string", default: "" },
        until: { type: "string", default: "" },
        source: { type: "string", default: "" },
        limit: { type: "integer", default: 50 },
        cursor: { type: "string", default: "" },
        summaries: { type: "boolean", default: false },
      },
    },
    annotations: RO,
  },
  {
    name: "expand",
    description: `The reading view for one search_history / list_window result: the full chunk plus source-aware surroundings reconstructed from the index (context_source: "index"). Per source: claude → the ±context conversation turns around the hit; browser → that profile's other visits the same local day; obsidian → the whole note; appusage → the day's full per-app seconds; calendar → that local day's full agenda; tasks → that day's whole task list; digest → the full rollup meta. git and shell return the chunk alone (their live stores are on the Mac). context caps at 25.`,
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string" },
        context: { type: "integer", default: 5 },
      },
      required: ["id"],
    },
    annotations: RO,
  },
  {
    name: "history_stats",
    description: `Show what search_history can search: per-source chunk counts and the date range each covers. Call this first to orient — e.g. to confirm a source is indexed, or how far back the record goes — before searching. Returns JSON {total_chunks, sources: {name: {chunks, earliest, latest}}, freshest_sync}. freshest_sync is when the Mac last pushed changes here — mention it in one line before answering if it's more than a few hours old (the remote copy may lag the Mac).`,
    inputSchema: { type: "object", properties: {} },
    annotations: RO,
  },

  /* ── action tools (wip/SPEC-llm-actions.md): the user's task/list/timer
     verbs, same mutations the app taps. All are gated by the app's
     "claude.ai actions" setting. ── */
  {
    name: "list_tasks",
    description: `The user's daily-note task list: {day, tasks: [{id, text, done, pending}]}. day is a local YYYY-MM-DD; default is the latest day that has tasks. The ids are what set_task / edit_task / delete_task / start_timer(task_id) take — read here before mutating a task.`,
    inputSchema: { type: "object", properties: { day: { type: "string", default: "" } } },
    annotations: RO,
  },
  {
    name: "create_task",
    description: `Add a task to a day's daily note. day is a local YYYY-MM-DD, default today, allowed from yesterday to a year ahead. A task for a future day appears in the app on that day, not before (undone tasks carry forward day to day). Duplicate of an existing task's text is rejected.` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { text: { type: "string" }, day: { type: "string", default: "" } },
      required: ["text"],
    },
    annotations: WRITE,
  },
  {
    name: "set_task",
    description: `Mark a task done (done:true) or open again (done:false) by id from list_tasks. Already in that state = no-op.` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { id: { type: "string" }, done: { type: "boolean" } },
      required: ["id", "done"],
    },
    annotations: WRITE,
  },
  {
    name: "edit_task",
    description: `Rewrite a task's text by id from list_tasks; its subtasks and notes stay.` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { id: { type: "string" }, new_text: { type: "string" } },
      required: ["id", "new_text"],
    },
    annotations: DESTRUCTIVE,
  },
  {
    name: "delete_task",
    description: `Remove a task line from its daily note, by id from list_tasks. Prefer set_task done:true unless the user clearly wants the line gone.` + QUEUED,
    inputSchema: {
      type: "object", properties: { id: { type: "string" } }, required: ["id"],
    },
    annotations: DESTRUCTIVE,
  },
  {
    name: "capture_note",
    description: `Append a timestamped thought under today's "## Notes" in the daily note — for things worth keeping that aren't tasks.` + QUEUED,
    inputSchema: {
      type: "object", properties: { text: { type: "string" } }, required: ["text"],
    },
    annotations: WRITE,
  },
  {
    name: "list_items",
    description: `A reusable checklist's contents, addressed by name (e.g. "grocery"): {name, path, words, items: [{id, text, state}]}. state: "need" = actively on the list, "got" = checked off this run, "cat" = in the catalog (known item, not currently needed). An unknown or ambiguous name errors with the available list names.`,
    inputSchema: {
      type: "object", properties: { list: { type: "string" } }, required: ["list"],
    },
    annotations: RO,
  },
  {
    name: "add_list_items",
    description: `Add items to a named checklist. An item already in the list's catalog is moved to "need" instead of duplicated. Returns per-item status.` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { list: { type: "string" }, items: { type: "array", items: { type: "string" } } },
      required: ["list", "items"],
    },
    annotations: WRITE,
  },
  {
    name: "create_list",
    description: `Create a new reusable checklist (a note in the vault's Lists/), optionally seeded with items. Only when no existing list fits — check the names an add_list_items/list_items error reports first. The list's verbs ("to pack" / "packed" …) are generated from its name.` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { name: { type: "string" }, items: { type: "array", items: { type: "string" } } },
      required: ["name"],
    },
    annotations: WRITE,
  },
  {
    name: "set_list_item",
    description: `Move a checklist item (id from list_items) between "need" and "got".` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { id: { type: "string" }, state: { type: "string", enum: ["need", "got"] } },
      required: ["id", "state"],
    },
    annotations: WRITE,
  },
  {
    name: "edit_list_item",
    description: `Rewrite a checklist item's text by id from list_items.` + QUEUED,
    inputSchema: {
      type: "object",
      properties: { id: { type: "string" }, new_text: { type: "string" } },
      required: ["id", "new_text"],
    },
    annotations: DESTRUCTIVE,
  },
  {
    name: "remove_list_item",
    description: `Delete an item from a checklist entirely, including its catalog, by id from list_items. Usually set_list_item state:"got" is what the user means.` + QUEUED,
    inputSchema: {
      type: "object", properties: { id: { type: "string" } }, required: ["id"],
    },
    annotations: DESTRUCTIVE,
  },
  {
    name: "list_timers",
    description: `The user's timers: [{id, label, state, remaining_ms, up, repeat}]. state is running/paused/done; up=true is a stopwatch (remaining_ms is elapsed). Ids feed control_timer.`,
    inputSchema: { type: "object", properties: {} },
    annotations: RO,
  },
  {
    name: "start_timer",
    description: `Start a timer, effective immediately (no queue). A countdown needs seconds (5–86400); up:true starts a stopwatch instead. task_id (an id from list_tasks) makes the stopwatch a focus session that logs "⏱ focused N min" to that task when dismissed. A finished countdown alerts the user's lock screen even if the app is closed; repeat:true re-alerts every cycle.`,
    inputSchema: {
      type: "object",
      properties: {
        label: { type: "string" }, seconds: { type: "integer", default: 0 },
        repeat: { type: "boolean", default: false }, up: { type: "boolean", default: false },
        task_id: { type: "string", default: "" },
      },
      required: ["label"],
    },
    annotations: WRITE,
  },
  {
    name: "control_timer",
    description: `pause / resume / dismiss a timer by id from list_timers. dismiss removes it (a focus stopwatch logs its minutes to its task first).`,
    inputSchema: {
      type: "object",
      properties: { id: { type: "string" }, op: { type: "string", enum: ["pause", "resume", "dismiss"] } },
      required: ["id", "op"],
    },
    annotations: WRITE,
  },
];

/* Names whose dispatch mutates; the endpoint gates these on the
   mcpWrites pref. */
export const WRITE_TOOLS = new Set(TOOLS.filter((t) => t.annotations?.readOnlyHint === false)
  .map((t) => t.name));

/* A local-day arg for a write: well-formed, a real date, and within
   [today − 1, today + 366]. Returns the day or null. */
export function checkDay(day: string, today: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const d = Date.parse(day + "T12:00:00Z"), t = Date.parse(today + "T12:00:00Z");
  if (!Number.isFinite(d) || !Number.isFinite(t)) return null;
  const diff = Math.round((d - t) / 864e5);
  return diff >= -1 && diff <= 366 ? day : null;
}

/* Resolve a spoken list name against the lists index: exact normalized
   match, else exact after light plural-stemming (groceries → grocery),
   else prefix, else substring — a unique hit at the strongest tier wins;
   anything else is an error naming the candidates. */
const normName = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
export const stemName = (s: string) => normName(s).split(" ")
  .map((w) => w.replace(/ies$/, "y").replace(/([^su])s$/, "$1")).join(" ");
export function resolveList<T extends { name: string }>(
  want: string, lists: T[],
): { hit: T } | { error: string } {
  const avail = () => lists.map((l) => l.name).sort().join(", ") || "(no lists yet)";
  const w = normName(want), ws = stemName(want);
  if (!w) return { error: `which list? available: ${avail()}` };
  const tiers: ((n: string, ns: string) => boolean)[] = [
    (n) => n === w,
    (_, ns) => ns === ws,
    (n, ns) => n.startsWith(w) || w.startsWith(n) || ns.startsWith(ws) || ws.startsWith(ns),
    (n, ns) => n.includes(w) || w.includes(n) || ns.includes(ws) || ws.includes(ns),
  ];
  for (const match of tiers) {
    const hits = lists.filter((l) => match(normName(l.name), stemName(l.name)));
    if (hits.length === 1) return { hit: hits[0] };
    if (hits.length > 1) {
      return { error: `"${want}" is ambiguous: ${hits.map((l) => l.name).sort().join(", ")}` };
    }
  }
  return { error: `no list matches "${want}"; available: ${avail()}` };
}

type Rpc = { jsonrpc?: string; id?: unknown; method?: string; params?: Record<string, unknown> };
export type RpcOut = { status: number; body: unknown | null };

const err = (id: unknown, code: number, message: string): RpcOut =>
  ({ status: 200, body: { jsonrpc: "2.0", id: id ?? null, error: { code, message } } });
const ok = (id: unknown, result: unknown): RpcOut =>
  ({ status: 200, body: { jsonrpc: "2.0", id, result } });

export async function handleRpc(
  rpc: Rpc,
  call: (name: string, args: Record<string, unknown>) => Promise<unknown>,
): Promise<RpcOut> {
  if (!rpc || rpc.jsonrpc !== "2.0" || typeof rpc.method !== "string") {
    return err(rpc?.id, -32600, "invalid request");
  }
  // Notifications (no id) are acknowledged and dropped — stateless server.
  if (rpc.id === undefined) return { status: 202, body: null };
  switch (rpc.method) {
    case "initialize": {
      const want = String(rpc.params?.protocolVersion ?? "");
      return ok(rpc.id, {
        protocolVersion: PROTOCOL_VERSIONS.includes(want) ? want : PROTOCOL_VERSIONS[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "history-rag", version: "2.0.0" },
      });
    }
    case "ping":
      return ok(rpc.id, {});
    case "tools/list":
      return ok(rpc.id, { tools: TOOLS });
    case "tools/call": {
      const name = String(rpc.params?.name ?? "");
      const tool = TOOLS.find((t) => t.name === name);
      if (!tool) return err(rpc.id, -32602, `unknown tool ${name}`);
      const args = (rpc.params?.arguments ?? {}) as Record<string, unknown>;
      try {
        const result = await call(name, args);
        return ok(rpc.id, {
          content: [{ type: "text", text: JSON.stringify(result) }],
          isError: false,
        });
      } catch (e) {
        return ok(rpc.id, {
          content: [{ type: "text", text: String(e instanceof Error ? e.message : e) }],
          isError: true,
        });
      }
    }
    default:
      return err(rpc.id, -32601, `method ${rpc.method} not found`);
  }
}

/* ── OAuth helpers (Web Crypto; node ≥19 has these globals too) ──────── */

export function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randToken(bytes = 32): string {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return b64url(a);
}

export async function sha256b64url(text: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return b64url(new Uint8Array(d));
}

/* PKCE S256: challenge must equal b64url(sha256(verifier)). */
export async function pkceOk(verifier: string, challenge: string): Promise<boolean> {
  if (!verifier || !challenge) return false;
  return (await sha256b64url(verifier)) === challenge;
}
