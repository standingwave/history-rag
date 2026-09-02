/* Ask mode, native: a model works the history tools in-process and
   synthesizes an answer — the same loop as ask.py on the Mac, with the
   tools bound to Convex functions. Provider adapters speak raw fetch:
   "openai-compatible" (OpenRouter, OpenAI, Groq …) and "anthropic".
   Presets come from ASK_MODELS; the client only ever names a preset, so
   no argument can point the loop (and a key) at an arbitrary endpoint.
   Tools are read-only, so injected text in indexed content can at worst
   produce a bad answer. */
import { v } from "convex/values";
import { action, internalAction, type ActionCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import { requireUserAction } from "./auth";
import { runSearch, ALL_SOURCES } from "./search";
import { presets, type Preset } from "./archive";

const TOOL_RESULT_MAX = 20_000;
const MAX_TURNS = 8;
const CITE_RE = /\[id:([^\]\s]+)\]/g;

type Tool = { name: string; description: string; schema: Record<string, unknown> };
const TOOLS: Tool[] = [
  {
    name: "search_history",
    description: "Semantic search over the user's own history. Sources: " + ALL_SOURCES.join(", ") +
      ". Returns ranked chunks with id, source, day, text; cite ids as [id:<id>]. " +
      "Use since/until (local YYYY-MM-DD) to window; 'digest' chunks summarise a day per stream.",
    schema: { type: "object", properties: {
      query: { type: "string" }, k: { type: "integer", default: 8 },
      source: { type: "string", description: "restrict to one source" },
      since: { type: "string" }, until: { type: "string" } }, required: ["query"] },
  },
  {
    name: "list_window",
    description: "Everything in a time window, newest first, no ranking — for 'what happened on <day>' " +
      "questions. summaries=true returns only the day digests. At least one bound is required.",
    schema: { type: "object", properties: {
      since: { type: "string" }, until: { type: "string" }, source: { type: "string" },
      summaries: { type: "boolean" }, limit: { type: "integer", default: 50 } } },
  },
  {
    name: "expand",
    description: "Read one chunk in full with its surroundings (conversation turns, the whole note, " +
      "the day's agenda …). Use before leaning on a search hit.",
    schema: { type: "object", properties: { id: { type: "string" }, context: { type: "integer", default: 5 } },
      required: ["id"] },
  },
];

function systemPrompt(strict: boolean) {
  const now = new Date();
  let p = "You answer questions from the user's own indexed history — their Claude Code sessions, " +
    "shell commands, browsing, git commits, notes, tasks, email, calendar, and app usage — using " +
    `the provided tools.\nNow: ${now.toISOString()} (UTC; the user's days are local). Resolve relative dates against that.\n` +
    "Work the disclosure ladder: search_history / list_window to find (read digest chunks first " +
    "for day/week questions); expand to read a hit in full before leaning on it.\n" +
    "Cite the chunks your answer rests on inline as [id:<chunk id>]. Answer concisely in plain " +
    "text. If the history doesn't contain the answer, say so plainly.";
  if (strict) p += "\nDo not answer from general knowledge: search the history first, and every claim " +
    "must cite a chunk it rests on as [id:<chunk id>]. If the history has nothing relevant, say " +
    "exactly that instead of answering.";
  return p;
}

type Call = { id: string; name: string; args: Record<string, unknown> };
type Step = { text: string; calls: Call[]; usage: { in: number; out: number } };

class AskError extends Error {}

async function http(url: string, headers: Record<string, string>, payload: unknown): Promise<any> {
  let r: Response;
  try {
    r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", ...headers },
                           body: JSON.stringify(payload) });
  } catch (e) { throw new AskError(`provider unreachable: ${e}`); }
  if (r.status !== 200) {
    const first = (await r.text()).trim().split("\n")[0] ?? "";
    throw new AskError(`provider error ${r.status}${first ? `: ${first.slice(0, 200)}` : ""}`);
  }
  return r.json();
}

interface Adapter {
  start(system: string, question: string): void;
  step(): Promise<Step>;
  addResults(results: { id: string; result: string }[]): void;
}

class OpenAI implements Adapter {
  private messages: any[] = [];
  private url: string;
  constructor(private preset: Preset & { _key: string }, private tools: Tool[] = TOOLS) {
    this.url = (preset.base_url ?? "https://api.openai.com/v1").replace(/\/$/, "") + "/chat/completions";
  }
  start(system: string, question: string) {
    this.messages = [{ role: "system", content: system }, { role: "user", content: question }];
  }
  async step(): Promise<Step> {
    const headers: Record<string, string> = this.preset._key ? { Authorization: `Bearer ${this.preset._key}` } : {};
    const data = await http(this.url, headers, {
      model: this.preset.model, messages: this.messages,
      max_tokens: Number(this.preset.max_tokens ?? 2048),
      ...(this.tools.length ? { tools: this.tools.map((t) =>
        ({ type: "function", function: { name: t.name, description: t.description, parameters: t.schema } })) } : {}),
    });
    const msg = data.choices[0].message;
    this.messages.push(msg);
    const calls: Call[] = [];
    for (const c of msg.tool_calls ?? []) {
      let args = {};
      try { args = JSON.parse(c.function.arguments || "{}"); } catch { /* malformed → no args */ }
      calls.push({ id: c.id, name: c.function.name, args });
    }
    const u = data.usage ?? {};
    return { text: msg.content ?? "", calls, usage: { in: u.prompt_tokens ?? 0, out: u.completion_tokens ?? 0 } };
  }
  addResults(results: { id: string; result: string }[]) {
    for (const r of results) this.messages.push({ role: "tool", tool_call_id: r.id, content: r.result });
  }
}

class Anthropic implements Adapter {
  private messages: any[] = [];
  private system = "";
  private url: string;
  constructor(private preset: Preset & { _key: string }, private tools: Tool[] = TOOLS) {
    this.url = (preset.base_url ?? "https://api.anthropic.com").replace(/\/$/, "") + "/v1/messages";
  }
  start(system: string, question: string) {
    this.system = system;
    this.messages = [{ role: "user", content: question }];
  }
  async step(): Promise<Step> {
    const data = await http(this.url, { "x-api-key": this.preset._key, "anthropic-version": "2023-06-01" }, {
      model: this.preset.model, max_tokens: Number(this.preset.max_tokens ?? 2048),
      system: this.system, messages: this.messages,
      ...(this.tools.length ? { tools: this.tools.map((t) =>
        ({ name: t.name, description: t.description, input_schema: t.schema })) } : {}),
    });
    this.messages.push({ role: "assistant", content: data.content });
    let text = ""; const calls: Call[] = [];
    for (const b of data.content) {
      if (b.type === "text") text += b.text;
      else if (b.type === "tool_use") calls.push({ id: b.id, name: b.name, args: b.input ?? {} });
    }
    const u = data.usage ?? {};
    return { text, calls, usage: { in: u.input_tokens ?? 0, out: u.output_tokens ?? 0 } };
  }
  addResults(results: { id: string; result: string }[]) {
    this.messages.push({ role: "user", content: results.map((r) => ({ type: "tool_result", tool_use_id: r.id, content: r.result })) });
  }
}

const ADAPTERS: Record<string, new (p: Preset & { _key: string }, tools?: Tool[]) => Adapter> = {
  "openai-compatible": OpenAI, anthropic: Anthropic,
};

/* One completion, no tool loop — the command parser's and bench's path
   (wip/SPEC-llm-actions.md). */
export async function chatOnce(
  preset: Preset & { _key: string }, system: string, user: string, maxTokens = 1000,
): Promise<{ text: string; usage: { in: number; out: number } }> {
  const Adapter = ADAPTERS[preset.backend ?? "openai-compatible"];
  if (!Adapter) throw new AskError(`unknown backend '${preset.backend}' in preset '${preset.name}'`);
  const adapter = new Adapter({ ...preset, max_tokens: maxTokens }, []);
  adapter.start(system, user);
  const step = await adapter.step();
  return { text: step.text, usage: step.usage };
}

/* The parser's preset: the one flagged "parse" (set by the bench's
   winner), else fastest by advertised latency. */
export function parsePreset(): (Preset & { _key: string }) | null {
  const ps = presets();
  if (!ps.length) return null;
  const flagged = ps.find((p) => p.parse);
  if (flagged) return flagged;
  const lat = (m: { latency?: string }) => Number((m.latency ?? "").replace(/\D/g, "")) || 99;
  return [...ps].sort((a, b) => lat(a) - lat(b))[0];
}

export function citations(text: string): string[] {
  const seen = new Set<string>(); const out: string[] = [];
  for (const m of text.matchAll(CITE_RE)) if (!seen.has(m[1])) { seen.add(m[1]); out.push(m[1]); }
  return out;
}

export type AskArgs = { q: string; model?: string; strict?: boolean };
export type AskResult = {
  answer?: string; citations?: string[]; note?: string; error?: string;
  usage?: { in: number; out: number; turns: number; model: string };
};

const askArgs = { q: v.string(), model: v.optional(v.string()), strict: v.optional(v.boolean()) };
export const ask = action({
  args: askArgs,
  handler: async (ctx, a): Promise<AskResult> => { await requireUserAction(ctx); return askCore(ctx, a); },
});
export const askInternal = internalAction({
  args: askArgs,
  handler: async (ctx, a): Promise<AskResult> => askCore(ctx, a),
});

export async function askCore(ctx: ActionCtx, a: AskArgs): Promise<AskResult> {
  {
    const ps = presets();
    if (!ps.length) return { error: "ask mode isn't configured — set ASK_MODELS and the key env vars" };
    const preset = a.model ? ps.find((p) => p.name === a.model) : ps[0];
    if (!preset) return { error: `unknown model preset '${a.model}'; available: ${ps.map((p) => p.name).join(", ")}` };
    const Adapter = ADAPTERS[preset.backend ?? "openai-compatible"];
    if (!Adapter) return { error: `unknown backend '${preset.backend}' in preset '${preset.name}'` };

    const runTool = async (name: string, args: Record<string, any>): Promise<string> => {
      try {
        let result: unknown;
        if (name === "search_history") {
          const r = await runSearch(ctx, { query: String(args.query ?? ""), limit: Number(args.k ?? 8),
            sources: args.source ? [String(args.source)] : undefined,
            since: args.since ? String(args.since) : undefined, until: args.until ? String(args.until) : undefined });
          result = { count: r.results.length, results: r.results.map((x, i) => ({
            rank: i + 1, id: x.id, source: x.source, score: x.score, day: x.day, location: x.location,
            text: x.text, meta: x.meta })) };
        } else if (name === "list_window") {
          result = await ctx.runQuery(internal.archive.windowInternal, {
            since: args.since ? String(args.since) : undefined, until: args.until ? String(args.until) : undefined,
            source: args.source ? String(args.source) : undefined, summaries: !!args.summaries,
            limit: Number(args.limit ?? 50) });
        } else if (name === "expand") {
          result = await ctx.runQuery(internal.archive.expandInternal, { id: String(args.id ?? ""), context: Number(args.context ?? 5) });
        } else {
          result = { error: `unknown tool '${name}'` };
        }
        const s = JSON.stringify(result);
        return s.length > TOOL_RESULT_MAX ? s.slice(0, TOOL_RESULT_MAX) + "… [truncated]" : s;
      } catch (e) {
        return JSON.stringify({ error: `${name} failed: ${e}` });
      }
    };

    const adapter = new Adapter(preset);
    adapter.start(systemPrompt(!!a.strict), a.q);
    const usage = { in: 0, out: 0, turns: 0, model: preset.name };
    let text = "";
    for (let turn = 0; turn < MAX_TURNS; turn++) {
      usage.turns++;
      let step: Step;
      try { step = await adapter.step(); }
      catch (e) { return { error: e instanceof AskError ? e.message : String(e), usage }; }
      usage.in += step.usage.in; usage.out += step.usage.out;
      text = step.text || text;
      if (!step.calls.length) return { answer: step.text, citations: citations(step.text), usage };
      const results = [];
      for (const c of step.calls) results.push({ id: c.id, result: await runTool(c.name, c.args) });
      adapter.addResults(results);
    }
    return { answer: text, note: "stopped at max_turns", citations: citations(text), usage };
  }
}
