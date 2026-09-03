/* The capture panel's ✨ mode (wip/SPEC-llm-actions.md): one sentence in,
   a validated action proposal out. Read-only — the model sees only the
   user's own open tasks, list names (a list's items only when its name
   is in the sentence), and timers; the client queues confirmed chips
   through the normal authed mutations. Any failure or non-command
   degrades to a plain-note proposal. */
import { v } from "convex/values";
import { action, type ActionCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import { requireUserAction } from "./auth";
import { chatOnce, parsePreset } from "./ask";
import { parseSystem, parseUser, validateActions, type ParseCtx, type ParsedAction } from "./parseCore";
import { stemName } from "./mcpCore";
import { derive } from "./timerMath";

const TIMEOUT_MS = 8_000;
const ITEM_CAP = 150;

export type ParseResult = {
  actions: ParsedAction[]; fallback: boolean; model: string | null; ms: number;
};

export async function gather(ctx: ActionCtx, today: string, sentence: string): Promise<ParseCtx> {
  // Open tasks live on the latest task day (carry moves them forward).
  const day = (await ctx.runQuery(internal.today.latestTaskDayInternal, {})) ?? today;
  const rows = await ctx.runQuery(internal.today.tasksInternal, { day });
  const tasks = rows
    .filter((r) => !(r.meta as { done?: boolean } | null)?.done)
    .map((r) => ({ id: r.id, text: r.text.split("\n", 1)[0].replace(/^Task: /, "") }));
  const idx = await ctx.runQuery(internal.lists.listsInternal, {});
  const words = new Set(stemName(sentence).split(" "));
  const lists: ParseCtx["lists"] = [];
  for (const l of idx) {
    const referenced = stemName(l.name).split(" ").some((w) => words.has(w));
    if (referenced) {
      const got = await ctx.runQuery(internal.lists.itemsInternal, { path: l.path });
      lists.push({ path: l.path, name: l.name,
        items: got.items.slice(0, ITEM_CAP).map(({ id, text, state }) => ({ id, text, state })) });
    } else {
      lists.push({ path: l.path, name: l.name });
    }
  }
  const trows = await ctx.runQuery(internal.timers.listInternal, {});
  const now = Date.now();
  const timers = trows.map((r) => ({ id: String(r.id), label: r.label, state: derive(r, now).st }));
  return { today, tasks, lists, timers };
}

/* Shared by the typed ✨ path and voice.command: a nonempty sentence in,
   a validated proposal out; any failure degrades to a plain note. */
export async function parseText(ctx: ActionCtx, sentence: string, today: string): Promise<ParseResult> {
  const t0 = Date.now();
  const asNote = (model: string | null): ParseResult => ({
    actions: [{ kind: "note", text: sentence }], fallback: true, model, ms: Date.now() - t0 });
  const preset = parsePreset();
  if (!preset) return asNote(null);
  const pctx = await gather(ctx, today, sentence);
  try {
    const got = await Promise.race([
      chatOnce(preset, parseSystem(today), parseUser(sentence, pctx), 400),
      new Promise<never>((_, rej) =>
        setTimeout(() => rej(new Error("timeout")), TIMEOUT_MS)),
    ]);
    return { ...validateActions(got.text, sentence, pctx), model: preset.name, ms: Date.now() - t0 };
  } catch {
    return asNote(preset.name);
  }
}

export const parse = action({
  args: { text: v.string(), today: v.string() },
  handler: async (ctx, { text, today }): Promise<ParseResult> => {
    await requireUserAction(ctx);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(today)) throw new Error("bad day");
    const sentence = text.trim().slice(0, 500);
    if (!sentence) throw new Error("empty");
    return parseText(ctx, sentence, today);
  },
});
