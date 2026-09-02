/* The in-app command parser's pure half (wip/SPEC-llm-actions.md): the
   prompt the model sees and the validator that turns its JSON into
   actions the client may queue. History-blind by construction — the
   context is only the user's own open tasks, list names (plus a
   referenced list's items), and timers. The model has no write
   authority: it returns a proposal, every id it emits must be one the
   prompt offered, and anything unusable degrades to a plain note so a
   thought is never lost. */
import { checkDay, stemName } from "./mcpCore.ts";
import { MIN_MS, MAX_MS } from "./timerMath.ts";

export type ParseCtx = {
  today: string;   // the CLIENT's local day — "tomorrow" anchors to the phone
  tasks: { id: string; text: string }[];
  lists: { path: string; name: string;
           items?: { id: string; text: string; state: string }[] }[];
  timers: { id: string; label: string; state: string }[];
};

/* `label` is display-only: the target's current text, attached during
   validation so chips can name what they touch. */
export type ParsedAction =
  | { kind: "task"; text: string; day: string }
  | { kind: "note"; text: string }
  | { kind: "toggle"; id: string; done: boolean; label: string }
  | { kind: "edit"; id: string; newText: string; label: string }
  | { kind: "delete"; id: string; label: string }
  | { kind: "listAdd"; path: string; items: string[]; label: string }
  | { kind: "listCreate"; name: string; items?: string[] }
  | { kind: "listSet"; id: string; state: "need" | "got"; label: string }
  | { kind: "listEdit"; id: string; newText: string; label: string }
  | { kind: "listRemove"; id: string; label: string }
  | { kind: "timerStart"; label: string; ms: number; repeat?: boolean; up?: boolean; taskId?: string }
  | { kind: "timerCtl"; id: string; op: "pause" | "resume" | "dismiss"; label: string };

const weekday = (day: string) => new Intl.DateTimeFormat("en-US",
  { weekday: "long", timeZone: "UTC" }).format(new Date(day + "T12:00:00Z"));

export function parseSystem(today: string): string {
  return `You turn one sentence from the user of a personal task app into JSON actions.
Reply with ONLY a JSON object {"actions":[...]}, no prose. Action shapes:
{"kind":"task","text":"...","day":"YYYY-MM-DD"} — new task in that day's note
{"kind":"note","text":"..."} — save a thought to today's notes
{"kind":"toggle","id":"...","done":true} — mark an OPEN TASK done (false reopens)
{"kind":"edit","id":"...","newText":"..."} — rewrite an open task
{"kind":"delete","id":"..."} — remove an open task
{"kind":"listAdd","path":"...","items":["..."]} — add items to a LIST by its path
{"kind":"listCreate","name":"...","items":["..."]} — make a NEW list (only when no listed one fits); items optional
{"kind":"listSet","id":"...","state":"got"} — check a LIST ITEM off; "need" puts it back on the list
{"kind":"listEdit","id":"...","newText":"..."} — rewrite a list item
{"kind":"listRemove","id":"..."} — delete a list item outright
{"kind":"timerStart","label":"...","ms":600000} — countdown; add "up":true for a stopwatch; "taskId" (an open task's id) makes the stopwatch a focus session
{"kind":"timerCtl","id":"...","op":"pause"} — pause/resume/dismiss a TIMER
Today is ${today} (${weekday(today)}); resolve relative days to real dates.
Use only ids and paths listed in the message. If the sentence refers to a task, list or timer that is not listed, or is a question or musing rather than an instruction, reply {"actions":[{"kind":"note","text":"<the sentence>"}]}.
At most 10 actions.`;
}

export function parseUser(text: string, ctx: ParseCtx): string {
  let msg = `OPEN TASKS: ${ctx.tasks.length
    ? JSON.stringify(ctx.tasks.map(({ id, text: t }) => ({ id, text: t }))) : "none"}\n`;
  msg += `LISTS: ${ctx.lists.length
    ? JSON.stringify(ctx.lists.map(({ path, name }) => ({ path, name }))) : "none"}\n`;
  for (const l of ctx.lists) {
    if (l.items?.length) msg += `ITEMS OF ${l.name}: ${JSON.stringify(l.items)}\n`;
  }
  msg += `TIMERS: ${ctx.timers.length ? JSON.stringify(ctx.timers) : "none"}\n`;
  msg += `SENTENCE: ${JSON.stringify(text)}`;
  return msg;
}

const trim500 = (x: unknown) => String(x ?? "").trim().slice(0, 500);

export function validateActions(raw: string, sentence: string, ctx: ParseCtx):
  { actions: ParsedAction[]; fallback: boolean } {
  const asNote = { actions: [{ kind: "note" as const, text: trim500(sentence) }], fallback: true };
  let parsed: { actions?: unknown };
  try {
    parsed = JSON.parse(raw.slice(raw.indexOf("{"), raw.lastIndexOf("}") + 1));
  } catch { return asNote; }
  const src = Array.isArray(parsed?.actions) ? parsed.actions : [];
  const task = new Map(ctx.tasks.map((t) => [t.id, t]));
  const list = new Map(ctx.lists.map((l) => [l.path, l]));
  const item = new Map(ctx.lists.flatMap((l) => (l.items ?? []).map((i) => [i.id, i] as const)));
  const timer = new Map(ctx.timers.map((t) => [t.id, t]));
  const out: ParsedAction[] = [];
  for (const a of src.slice(0, 10) as Record<string, unknown>[]) {
    const id = String(a?.id ?? "");
    switch (a?.kind) {
      case "task": {
        const dayRaw = String(a.day ?? "").trim();
        const day = dayRaw ? checkDay(dayRaw, ctx.today) : ctx.today;
        const text = trim500(a.text);
        if (day && text) out.push({ kind: "task", text, day });
        break;
      }
      case "note": {
        const text = trim500(a.text);
        if (text) out.push({ kind: "note", text });
        break;
      }
      case "toggle": {
        const t = task.get(id);
        if (t) out.push({ kind: "toggle", id, done: a.done !== false, label: t.text });
        break;
      }
      case "edit": {
        const t = task.get(id), newText = trim500(a.newText);
        if (t && newText) out.push({ kind: "edit", id, newText, label: t.text });
        break;
      }
      case "delete": {
        const t = task.get(id);
        if (t) out.push({ kind: "delete", id, label: t.text });
        break;
      }
      case "listAdd": {
        const l = list.get(String(a.path ?? ""));
        const items = (Array.isArray(a.items) ? a.items : [])
          .map(trim500).filter(Boolean).slice(0, 20);
        if (l && items.length) out.push({ kind: "listAdd", path: l.path, items, label: l.name });
        break;
      }
      case "listCreate": {
        const name = trim500(a.name).slice(0, 60);
        const items = (Array.isArray(a.items) ? a.items : [])
          .map(trim500).filter(Boolean).slice(0, 20);
        if (!name) break;
        // "Create" a list that already exists = add to it (or nothing to do).
        const existing = ctx.lists.find((l) => stemName(l.name) === stemName(name));
        if (existing) {
          if (items.length) out.push({ kind: "listAdd", path: existing.path, items, label: existing.name });
          break;
        }
        out.push({ kind: "listCreate", name, ...(items.length ? { items } : {}) });
        break;
      }
      case "listSet": {
        const i = item.get(id), state = a.state === "need" ? "need" : a.state === "got" ? "got" : null;
        if (i && state) out.push({ kind: "listSet", id, state, label: i.text });
        break;
      }
      case "listEdit": {
        const i = item.get(id), newText = trim500(a.newText);
        if (i && newText) out.push({ kind: "listEdit", id, newText, label: i.text });
        break;
      }
      case "listRemove": {
        const i = item.get(id);
        if (i) out.push({ kind: "listRemove", id, label: i.text });
        break;
      }
      case "timerStart": {
        const label = trim500(a.label).slice(0, 40) || "timer";
        const taskId = task.has(String(a.taskId ?? "")) ? String(a.taskId) : undefined;
        const up = a.up === true || !!taskId;
        if (up) { out.push({ kind: "timerStart", label, ms: 0, up: true, ...(taskId ? { taskId } : {}) }); break; }
        const ms = Math.round(Number(a.ms));
        if (Number.isFinite(ms) && ms >= MIN_MS && ms <= MAX_MS) {
          out.push({ kind: "timerStart", label, ms, ...(a.repeat === true ? { repeat: true } : {}) });
        }
        break;
      }
      case "timerCtl": {
        const t = timer.get(id);
        const op = a.op === "pause" || a.op === "resume" || a.op === "dismiss" ? a.op : null;
        if (t && op) out.push({ kind: "timerCtl", id, op, label: t.label });
        break;
      }
    }
  }
  return out.length ? { actions: out, fallback: false } : asNote;
}
