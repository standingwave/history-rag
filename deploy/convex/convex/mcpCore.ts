/* MCP protocol core (wip/SPEC-convex-mcp.md): stateless JSON-RPC over
   one POST — the same wire contract the Lambda's FastMCP spoke
   (stateless_http + json_response). Pure module so the framing is
   node-testable; convex/mcp.ts supplies the tool dispatch. */

export const PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"];

export type ToolDef = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
};

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
  },
  {
    name: "history_stats",
    description: `Show what search_history can search: per-source chunk counts and the date range each covers. Call this first to orient — e.g. to confirm a source is indexed, or how far back the record goes — before searching. Returns JSON {total_chunks, sources: {name: {chunks, earliest, latest}}, freshest_sync}. freshest_sync is when the Mac last pushed changes here — mention it in one line before answering if it's more than a few hours old (the remote copy may lag the Mac).`,
    inputSchema: { type: "object", properties: {} },
  },
];

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
