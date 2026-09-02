/* Profile candidate parse models (wip/SPEC-llm-actions.md): run the
   production prompt + validators over a golden utterance set against
   each ASK_MODELS preset and report accuracy, latency, and cost.
   Calls run sequentially so latency numbers are honest — bench slow
   models one at a time (an action has ~10 minutes):

     npx convex run parseBench:bench                        # every preset
     npx convex run parseBench:bench '{"models":["haiku"],"reps":3}'

   Qualify: accuracy within 2 of perfect, zero destructive misfires
   (a delete/edit aimed at an id the case didn't expect); among
   qualifiers pick lowest p50, set "parse": true on it in ASK_MODELS. */
import { v } from "convex/values";
import { internalAction } from "./_generated/server";
import { presets } from "./archive";
import { chatOnce } from "./ask";
import { parseSystem, parseUser, validateActions, type ParseCtx } from "./parseCore";

/* Per-1M-token prices for the cost column — training-data estimates;
   update from provider pages when adding keys. Unknown preset → "?". */
const PRICES: Record<string, { in: number; out: number }> = {
  haiku: { in: 1, out: 5 }, sonnet: { in: 3, out: 15 },
  "gpt-mini": { in: 0.25, out: 2 }, "gpt-nano": { in: 0.05, out: 0.4 },
  "gemini-flash": { in: 0.3, out: 2.5 }, "flash-lite": { in: 0.1, out: 0.4 },
  "groq-scout": { in: 0.11, out: 0.34 }, "groq-70b": { in: 0.59, out: 0.79 },
  cerebras: { in: 0.1, out: 0.6 }, "mistral-small": { in: 0.1, out: 0.3 },
};

/* One fixed context for every case; production varies it per call. */
const CTX: ParseCtx = {
  today: "2026-09-02",   // a Wednesday
  tasks: [
    { id: "t1", text: "Call mom" },
    { id: "t2", text: "Dentist appointment 14:30" },
    { id: "t3", text: "Cancel MGM reservation" },
    { id: "t4", text: "Water the hoya" },
  ],
  lists: [
    { path: "Lists/Grocery.md", name: "Grocery", items: [
      { id: "g1", text: "milk", state: "cat" },
      { id: "g2", text: "eggs", state: "need" },
      { id: "g3", text: "hot sauce", state: "got" },
    ] },
    { path: "Lists/Camping.md", name: "Camping" },
  ],
  timers: [{ id: "w1", label: "tea", state: "running" }],
};

/* A case passes if its produced actions match ANY alternative pattern
   set: same count, each pattern claiming a distinct action. Pattern
   values: exact match, or a RegExp tested against the value normalized
   to lowercase words; arrays match order-insensitively. */
type Pat = Record<string, unknown>;
type Case = { text: string; want: Pat[][] };
const CASES: Case[] = [
  // creation + day resolution
  { text: "add a task to call dad", want: [[{ kind: "task", day: "2026-09-02", text: /call.*dad/ }]] },
  { text: "create a task for me to call my dad tomorrow", want: [[{ kind: "task", day: "2026-09-03", text: /call.*dad/ }]] },
  { text: "remind me to take out the trash on friday", want: [[{ kind: "task", day: "2026-09-04", text: /trash/ }]] },
  { text: "i need to renew my passport next tuesday", want: [[{ kind: "task", day: "2026-09-08", text: /passport/ }]] },
  { text: "add water the plants to today's tasks", want: [[{ kind: "task", day: "2026-09-02", text: /water.*plants/ }]] },
  // notes
  { text: "i had a cool idea about using sourdough discard in pancakes", want: [[{ kind: "note", text: /sourdough/ }]] },
  { text: "note: parked on level 2 spot 4B", want: [[{ kind: "note", text: /level 2.*4b|4b.*level 2/ }]] },
  // task mutations by loose reference
  { text: "mark the dentist one done", want: [[{ kind: "toggle", id: "t2", done: true }]] },
  { text: "i called mom", want: [[{ kind: "toggle", id: "t1", done: true }]] },
  { text: "change call mom to call mom about her hearing aid", want: [[{ kind: "edit", id: "t1", newText: /hearing aid/ }]] },
  { text: "get rid of the MGM task", want: [[{ kind: "delete", id: "t3" }]] },
  // references matching nothing must fall back to a note, never a guessed id
  { text: "delete the task about the gym", want: [[{ kind: "note", text: /gym/ }]] },
  { text: "mark the piano practice task as done", want: [[{ kind: "note", text: /piano/ }]] },
  // lists
  { text: "add milk to the grocery list",
    want: [[{ kind: "listAdd", path: "Lists/Grocery.md", items: [/milk/] }],
           [{ kind: "listSet", id: "g1", state: "need" }]] },
  { text: "add bread and butter to groceries",
    want: [[{ kind: "listAdd", path: "Lists/Grocery.md", items: [/bread/, /butter/] }]] },
  { text: "add a flashlight to the camping list",
    want: [[{ kind: "listAdd", path: "Lists/Camping.md", items: [/flashlight/] }]] },
  { text: "put milk eggs and bread on the shopping list",
    want: [[{ kind: "listAdd", path: "Lists/Grocery.md", items: [/milk/, /eggs/, /bread/] }]] },
  { text: "got the eggs", want: [[{ kind: "listSet", id: "g2", state: "got" }]] },
  { text: "we're out of hot sauce again",
    want: [[{ kind: "listSet", id: "g3", state: "need" }],
           [{ kind: "listAdd", path: "Lists/Grocery.md", items: [/hot sauce/] }]] },
  { text: "remove hot sauce from the grocery list entirely", want: [[{ kind: "listRemove", id: "g3" }]] },
  { text: "rename eggs to free range eggs", want: [[{ kind: "listEdit", id: "g2", newText: /free range eggs/ }]] },
  { text: "add sunscreen to the beach list", want: [[{ kind: "note", text: /sunscreen/ }]] },
  // timers
  { text: "set a timer for 10 minutes", want: [[{ kind: "timerStart", ms: 600000 }]] },
  { text: "pizza timer, 12 minutes", want: [[{ kind: "timerStart", ms: 720000, label: /pizza/ }]] },
  { text: "start a stopwatch", want: [[{ kind: "timerStart", up: true }]] },
  { text: "start a focus timer on the dentist task", want: [[{ kind: "timerStart", up: true, taskId: "t2" }]] },
  { text: "pause the tea timer", want: [[{ kind: "timerCtl", id: "w1", op: "pause" }]] },
  { text: "resume tea", want: [[{ kind: "timerCtl", id: "w1", op: "resume" }]] },
  { text: "dismiss the tea timer", want: [[{ kind: "timerCtl", id: "w1", op: "dismiss" }]] },
  { text: "stop the laundry timer", want: [[{ kind: "note", text: /laundry/ }]] },
  // compounds
  { text: "add a task to book the campsite and put propane on the camping list",
    want: [[{ kind: "task", day: "2026-09-02", text: /campsite/ },
            { kind: "listAdd", path: "Lists/Camping.md", items: [/propane/] }]] },
  { text: "call dad tomorrow and set a 20 minute timer",
    want: [[{ kind: "task", day: "2026-09-03", text: /call.*dad/ },
            { kind: "timerStart", ms: 1200000 }]] },
  // non-commands
  { text: "asdf qwerty zzz", want: [[{ kind: "note", text: /asdf/ }]] },
  { text: "what's on my grocery list?", want: [[{ kind: "note", text: /grocery/ }]] },
];

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
function matchVal(pat: unknown, val: unknown): boolean {
  if (pat instanceof RegExp) return pat.test(norm(String(val ?? "")));
  if (Array.isArray(pat)) {
    if (!Array.isArray(val) || pat.length !== val.length) return false;
    return assign(pat, val, (p, x) => matchVal(p, x));
  }
  return pat === val;
}
const matchAction = (pat: Pat, act: Record<string, unknown>) =>
  Object.entries(pat).every(([k, p]) => matchVal(p, act[k]));
/* Distinct-assignment match (tiny sets → plain backtracking). */
function assign<P, A>(pats: P[], acts: A[], ok: (p: P, a: A) => boolean): boolean {
  const used = new Set<number>();
  const go = (i: number): boolean => {
    if (i === pats.length) return true;
    for (let j = 0; j < acts.length; j++) {
      if (used.has(j) || !ok(pats[i], acts[j])) continue;
      used.add(j);
      if (go(i + 1)) return true;
      used.delete(j);
    }
    return false;
  };
  return go(0);
}
const matches = (alt: Pat[], acts: Record<string, unknown>[]) =>
  alt.length === acts.length && assign(alt, acts, matchAction);

/* A mutating action aimed at an id no alternative of this case expects. */
function misfired(c: Case, acts: Record<string, unknown>[]): boolean {
  const okIds = new Set(c.want.flat().map((p) => p.id).filter(Boolean));
  return acts.some((a) =>
    ["toggle", "edit", "delete", "listSet", "listEdit", "listRemove", "timerCtl"]
      .includes(String(a.kind)) && !okIds.has(a.id));
}

export const bench = internalAction({
  args: {
    models: v.optional(v.array(v.string())),
    reps: v.optional(v.number()),
    cases: v.optional(v.number()),
  },
  handler: async (_ctx, { models, reps = 1, cases }) => {
    const ps = presets().filter((p) => !models || models.includes(p.name));
    if (!ps.length) return "no matching presets — check ASK_MODELS and key env vars";
    const set = cases ? CASES.slice(0, cases) : CASES;
    const lines: string[] = [];
    for (const p of ps) {
      const lat: number[] = [];
      let pass = 0, calls = 0, misfires = 0, errors = 0, tokIn = 0, tokOut = 0;
      const missed: string[] = [];
      for (let r = 0; r < reps; r++) {
        for (const c of set) {
          calls++;
          const t0 = Date.now();
          try {
            const got = await chatOnce(p, parseSystem(CTX.today), parseUser(c.text, CTX), 400);
            lat.push(Date.now() - t0);
            tokIn += got.usage.in; tokOut += got.usage.out;
            const acts = validateActions(got.text, c.text, CTX)
              .actions.map(({ ...a }) => { delete (a as { label?: string }).label; return a; });
            if (c.want.some((alt) => matches(alt, acts))) pass++;
            else if (r === 0) missed.push(c.text);
            if (misfired(c, acts)) misfires++;
          } catch (e) {
            lat.push(Date.now() - t0);
            errors++;
            if (r === 0) missed.push(`${c.text} [${String(e).slice(0, 60)}]`);
          }
        }
      }
      lat.sort((a, b) => a - b);
      const p50 = lat[Math.floor(lat.length / 2)] ?? 0;
      const p95 = lat[Math.min(lat.length - 1, Math.floor(lat.length * 0.95))] ?? 0;
      const price = PRICES[p.name];
      const cost = price && calls
        ? ((tokIn / calls) * price.in + (tokOut / calls) * price.out) / 1e6 : null;
      lines.push(`${p.name.padEnd(14)} acc ${String(pass).padStart(2)}/${calls}` +
        `  p50 ${String(p50).padStart(5)} ms  p95 ${String(p95).padStart(5)} ms` +
        `  ~${cost !== null ? (cost * 100).toFixed(4) + "¢" : "?"}/parse` +
        (misfires ? `  ⚠ MISFIRES ${misfires}` : "") + (errors ? `  errors ${errors}` : ""));
      for (const m of missed.slice(0, 8)) lines.push(`    ✗ ${m}`);
    }
    lines.push(`qualify: acc ≥ ${(set.length - 2) * reps}/${set.length * reps} and zero misfires; ` +
      `lowest p50 wins → set "parse": true on it in ASK_MODELS`);
    return lines.join("\n");
  },
});
