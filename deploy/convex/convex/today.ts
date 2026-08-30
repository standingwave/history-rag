/* The dashboard's reads and its task writes (wip/SPEC-task-writes.md).
   `tasks` is the subscribed query behind the Tasks widget; `toggle`,
   `add`, `edit` and `remove` change `items` optimistically and queue an
   intent for the Mac, which confirms through `applyIntent` (internal,
   deploy-key only) and, eventually, a re-push of the real state that
   clears `pending`/`hidden`. Placeholder rows carry the chunk id the
   source will compute, so the re-push patches them in place. */
import { v } from "convex/values";
import {
  query, mutation, internalQuery, internalMutation,
} from "./_generated/server";
import { requireUser } from "./auth";
import { want } from "./schema";
import { taskChunkId, normTask } from "./ids";
import { bump } from "./sync";

function pub(row: {
  chunkId: string; source: string; timestamp: string; day: string;
  location: string; text: string; meta: unknown; pending?: boolean;
}) {
  const { chunkId, source, timestamp, day, location, text, meta, pending } = row;
  return { id: chunkId, source, timestamp, day, location, text, meta, pending: !!pending };
}

export const tasks = query({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    await requireUser(ctx);
    const rows = await ctx.db
      .query("items")
      .withIndex("by_source_day", (q) => q.eq("source", "tasks").eq("day", day))
      .collect();
    rows.sort((a, b) => (a.meta?.order ?? 0) - (b.meta?.order ?? 0));
    return rows.filter((r) => !r.hidden).map(pub);
  },
});

/* The latest day that has any task — the empty state's label. */
export const latestTaskDay = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    const row = await ctx.db
      .query("items")
      .withIndex("by_source_timestamp", (q) => q.eq("source", "tasks"))
      .order("desc")
      .first();
    return row?.day ?? null;
  },
});

export const agenda = query({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    await requireUser(ctx);
    const rows = await ctx.db
      .query("items")
      .withIndex("by_source_day", (q) => q.eq("source", "calendar").eq("day", day))
      .collect();
    rows.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    return rows.map(pub);
  },
});

export const notes = query({
  args: { since: v.string(), limit: v.optional(v.number()) },
  handler: async (ctx, { since, limit }) => {
    await requireUser(ctx);
    const rows = await ctx.db
      .query("items")
      .withIndex("by_source_timestamp", (q) =>
        q.eq("source", "obsidian").gte("timestamp", since))
      .order("desc")
      .take(limit ?? 10);
    return rows.map(pub);
  },
});

export const counts = query({
  args: { day: v.string() },
  handler: async (ctx, { day }) => {
    await requireUser(ctx);
    const out: Record<string, number> = {};
    for (const source of ["tasks", "calendar", "obsidian"]) {
      const rows = await ctx.db
        .query("items")
        .withIndex("by_source_day", (q) => q.eq("source", source).eq("day", day))
        .collect();
      out[source] = rows.length;
    }
    return out;
  },
});

export const toggle = mutation({
  args: { id: v.string() },
  handler: async (ctx, { id }) => {
    await requireUser(ctx);
    const row = await ctx.db
      .query("items")
      .withIndex("by_chunkId", (q) => q.eq("chunkId", id))
      .unique();
    if (!row || row.source !== "tasks") throw new Error("not a task");
    const done = !!row.meta?.done;
    await ctx.db.patch(row._id, {
      meta: { ...row.meta, done: !done },
      pending: true,
    });
    const text = row.text.split("\n", 1)[0].replace(/^Task: /, "");
    return await ctx.db.insert("taskIntents", {
      kind: "toggle", chunkId: id, day: row.day, vault: String(row.meta?.vault ?? ""),
      text, want: done ? "open" : "done", requestedAt: Date.now(),
    });
  },
});

function taskText(row: { text: string }) {
  return row.text.split("\n", 1)[0].replace(/^Task: /, "");
}
async function byChunkId(ctx: { db: any }, id: string) {
  return ctx.db.query("items").withIndex("by_chunkId", (q: any) => q.eq("chunkId", id)).unique();
}
async function taskRow(ctx: { db: any }, id: string) {
  const row = await byChunkId(ctx, id);
  if (!row || row.source !== "tasks") throw new Error("not a task");
  if (row.pending) throw new Error("that task is still being applied");
  return row;
}
/* The vault a day's note lives in: from any task already on that day, else
   the most recent task. Adds are refused when there is nothing to go on. */
async function vaultFor(ctx: { db: any }, day: string): Promise<string> {
  const same = await ctx.db.query("items")
    .withIndex("by_source_day", (q: any) => q.eq("source", "tasks").eq("day", day)).first();
  const any = same ?? await ctx.db.query("items")
    .withIndex("by_source_timestamp", (q: any) => q.eq("source", "tasks")).order("desc").first();
  const vault = String(any?.meta?.vault ?? "");
  if (!vault) throw new Error("no vault known yet — index tasks first");
  return vault;
}
/* A pending placeholder shaped like the row `sources/tasks.py` will push. */
function placeholder(vault: string, day: string, text: string, meta: Record<string, unknown>, order: number) {
  return {
    chunkId: taskChunkId(vault, text), source: "tasks", timestamp: `${day}T00:00:00`,
    day, month: day.slice(0, 7), location: `${day}.md#${order}`, text: `Task: ${text}`,
    meta: { vault, done: false, first_seen: day, last_seen: day, done_on: null, section: "",
            subtasks: [], attachments: [], days: 1, ...meta, order },
    entryId: "", contentHash: "", pending: true,
  };
}
async function nextOrder(ctx: { db: any }, day: string) {
  const rows = await ctx.db.query("items")
    .withIndex("by_source_day", (q: any) => q.eq("source", "tasks").eq("day", day)).collect();
  return rows.reduce((m: number, r: any) => Math.max(m, r.meta?.order ?? 0), 0) + 1;
}

export const add = mutation({
  args: { day: v.string(), text: v.string() },
  handler: async (ctx, { day, text }) => {
    await requireUser(ctx);
    text = text.trim();
    if (!text) throw new Error("empty task");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw new Error("bad day");
    const vault = await vaultFor(ctx, day);
    const id = taskChunkId(vault, text);
    const dup = await byChunkId(ctx, id);
    if (dup && !dup.hidden) throw new Error("a task with that text already exists");
    const row = placeholder(vault, day, text, {}, await nextOrder(ctx, day));
    if (dup) await ctx.db.replace(dup._id, row);
    else { await ctx.db.insert("items", row); await bump(ctx, "tasks", 1, day); }
    return await ctx.db.insert("taskIntents", {
      kind: "add", chunkId: id, day, vault, text, requestedAt: Date.now(),
    });
  },
});

export const edit = mutation({
  args: { id: v.string(), newText: v.string() },
  handler: async (ctx, { id, newText }) => {
    await requireUser(ctx);
    newText = newText.trim();
    const row = await taskRow(ctx, id);
    const text = taskText(row);
    if (!newText) throw new Error("empty task");
    if (normTask(newText) === normTask(text)) return null;
    const vault = String(row.meta?.vault ?? "");
    const newId = taskChunkId(vault, newText);
    const dup = await byChunkId(ctx, newId);
    if (dup && !dup.hidden) throw new Error("a task with that text already exists");
    const fresh = placeholder(vault, row.day, newText, { ...row.meta }, row.meta?.order ?? 0);
    if (dup) await ctx.db.replace(dup._id, fresh);
    else { await ctx.db.insert("items", fresh); await bump(ctx, "tasks", 1, row.day); }
    await ctx.db.patch(row._id, { pending: true, hidden: true });
    return await ctx.db.insert("taskIntents", {
      kind: "edit", chunkId: id, day: row.day, vault, text, newText, requestedAt: Date.now(),
    });
  },
});

/* ── subtasks: one level under a task; identity is the parent's row, the
   change is to its meta.subtasks[] and the intent carries `parent`. ── */

type Sub = { text: string; done: boolean; depth?: number };
function subIndex(subs: Sub[], text: string) {
  const hits = subs.map((s, i) => [normTask(s.text) === normTask(text), i] as const).filter(([h]) => h);
  if (hits.length !== 1) throw new Error(hits.length ? "subtask text is ambiguous" : "no such subtask");
  return hits[0][1];
}
async function subIntent(ctx: { db: any }, row: any, kind: "toggle" | "add" | "edit" | "delete",
                         subs: Sub[], fields: Record<string, unknown>) {
  const prior = row.meta?.subtasks ?? [];
  await ctx.db.patch(row._id, { meta: { ...row.meta, subtasks: subs }, pending: true });
  return await ctx.db.insert("taskIntents", {
    kind, chunkId: row.chunkId, day: row.day, vault: String(row.meta?.vault ?? ""),
    parent: taskText(row), prior, requestedAt: Date.now(), ...fields,
  });
}

export const subToggle = mutation({
  args: { id: v.string(), text: v.string() },
  handler: async (ctx, { id, text }) => {
    await requireUser(ctx);
    const row = await taskRow(ctx, id);
    const subs: Sub[] = [...(row.meta?.subtasks ?? [])];
    const i = subIndex(subs, text);
    subs[i] = { ...subs[i], done: !subs[i].done };
    return subIntent(ctx, row, "toggle", subs, { text: subs[i].text, want: subs[i].done ? "done" : "open" });
  },
});

export const subAdd = mutation({
  args: { id: v.string(), text: v.string() },
  handler: async (ctx, { id, text }) => {
    await requireUser(ctx);
    text = text.trim();
    if (!text) throw new Error("empty subtask");
    const row = await taskRow(ctx, id);
    const subs: Sub[] = [...(row.meta?.subtasks ?? [])];
    if (subs.some((s) => normTask(s.text) === normTask(text))) throw new Error("a subtask with that text already exists");
    subs.push({ text, done: false, depth: 4 });
    return subIntent(ctx, row, "add", subs, { text });
  },
});

export const subEdit = mutation({
  args: { id: v.string(), text: v.string(), newText: v.string() },
  handler: async (ctx, { id, text, newText }) => {
    await requireUser(ctx);
    newText = newText.trim();
    if (!newText) throw new Error("empty subtask");
    const row = await taskRow(ctx, id);
    const subs: Sub[] = [...(row.meta?.subtasks ?? [])];
    const i = subIndex(subs, text);
    if (normTask(newText) === normTask(subs[i].text)) return null;
    if (subs.some((s, j) => j !== i && normTask(s.text) === normTask(newText))) throw new Error("a subtask with that text already exists");
    subs[i] = { ...subs[i], text: newText };
    return subIntent(ctx, row, "edit", subs, { text, newText });
  },
});

export const subRemove = mutation({
  args: { id: v.string(), text: v.string() },
  handler: async (ctx, { id, text }) => {
    await requireUser(ctx);
    const row = await taskRow(ctx, id);
    const subs: Sub[] = [...(row.meta?.subtasks ?? [])];
    const i = subIndex(subs, text);
    subs.splice(i, 1);
    return subIntent(ctx, row, "delete", subs, { text });
  },
});

export const remove = mutation({
  args: { id: v.string() },
  handler: async (ctx, { id }) => {
    await requireUser(ctx);
    const row = await taskRow(ctx, id);
    await ctx.db.patch(row._id, { pending: true, hidden: true });
    return await ctx.db.insert("taskIntents", {
      kind: "delete", chunkId: id, day: row.day, vault: String(row.meta?.vault ?? ""),
      text: taskText(row), requestedAt: Date.now(),
    });
  },
});

/* Recent intents for the UI: pending ones and errors worth showing. */
export const intents = query({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    const rows = await ctx.db.query("taskIntents").order("desc").take(20);
    return rows.map((r) => ({
      id: r._id, chunkId: r.chunkId, kind: r.kind ?? "toggle", text: r.text, want: r.want ?? null,
      newId: r.kind === "edit" && !r.parent ? taskChunkId(r.vault, r.newText ?? "") : null,
      parent: r.parent ?? null,
      requestedAt: r.requestedAt, appliedAt: r.appliedAt ?? null, error: r.error ?? null,
    }));
  },
});

/* ── the Mac's side (deploy key) ── */

export const pendingIntents = internalQuery({
  args: {},
  handler: async (ctx) => {
    const rows = await ctx.db
      .query("taskIntents")
      .withIndex("by_appliedAt", (q) => q.eq("appliedAt", undefined))
      .collect();
    return rows.map((r) => ({
      id: r._id, kind: r.kind ?? "toggle", chunkId: r.chunkId, day: r.day, vault: r.vault,
      text: r.text, want: r.want ?? null, newText: r.newText ?? null, parent: r.parent ?? null,
      requestedAt: r.requestedAt,
    }));
  },
});

export const applyIntent = internalMutation({
  args: { id: v.id("taskIntents"), error: v.optional(v.string()) },
  handler: async (ctx, { id, error }) => {
    const intent = await ctx.db.get(id);
    if (!intent) return;
    await ctx.db.patch(id, { appliedAt: Date.now(), error });
    if (!error) return;
    // The vault didn't change: undo the optimistic effect of this kind.
    const row = await byChunkId(ctx, intent.chunkId);
    if (intent.parent != null) {
      if (row) await ctx.db.patch(row._id, { meta: { ...row.meta, subtasks: intent.prior ?? [] }, pending: false });
      return;
    }
    switch (intent.kind ?? "toggle") {
      case "toggle":
        if (row) await ctx.db.patch(row._id, { meta: { ...row.meta, done: intent.want === "open" }, pending: false });
        break;
      case "add":
        if (row?.pending) { await ctx.db.delete(row._id); await bump(ctx, "tasks", -1); }
        break;
      case "edit": {
        const fresh = await byChunkId(ctx, taskChunkId(intent.vault, intent.newText ?? ""));
        if (fresh?.pending) { await ctx.db.delete(fresh._id); await bump(ctx, "tasks", -1); }
        if (row) await ctx.db.patch(row._id, { pending: false, hidden: false });
        break;
      }
      case "delete":
        if (row) await ctx.db.patch(row._id, { pending: false, hidden: false });
        break;
    }
  },
});

export { want };
