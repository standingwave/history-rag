/* Vault lists (wip/SPEC-vault-lists.md): notes in Lists/, three item
   states — need/got as checkboxes, catalog bullets below "## Catalog".
   Reads group the sentinel-day ("list") chunks; writes are optimistic in
   `items` and queue intents for the Mac exactly like task writes.
   vocab() is the app's first live LLM call: one tiny completion naming a
   new list's verbs, plain on any failure, never in the read path. */
import { v } from "convex/values";
import {
  query, mutation, action, internalQuery, internalMutation,
  type QueryCtx, type MutationCtx,
} from "./_generated/server";
import type { Doc } from "./_generated/dataModel";
import { requireUser, requireUserAction } from "./auth";
import { listChunkId, listNoteId } from "./ids";
import { bump } from "./sync";
import { presets } from "./archive";

export const PLAIN_WORDS = { need: "to do", got: "done", done: "reset" };
const wordsArg = v.object({ need: v.string(), got: v.string(), done: v.string() });

async function listRows(ctx: { db: any }): Promise<Doc<"items">[]> {
  const rows = await ctx.db.query("items")
    .withIndex("by_source_day", (q: any) => q.eq("source", "tasks").eq("day", "list"))
    .collect();
  return rows.filter((r: Doc<"items">) => !r.hidden);
}
async function byChunkId(ctx: { db: any }, chunkId: string) {
  return await ctx.db.query("items")
    .withIndex("by_chunkId", (q: any) => q.eq("chunkId", chunkId)).unique();
}
const itemText = (r: Doc<"items">) => {
  const pfx = `${r.meta?.list} list: `;
  return r.text.startsWith(pfx) ? r.text.slice(pfx.length) : r.text;
};
const goodWords = (w: any) =>
  w?.need && w?.got && w?.done ? (w as typeof PLAIN_WORDS) : null;

async function listsH(ctx: QueryCtx) {
    const by: Record<string, { path: string; name: string; words: typeof PLAIN_WORDS;
      need: number; got: number; cat: number; pending: boolean }> = {};
    for (const r of await listRows(ctx)) {
      const path = String(r.meta?.path ?? "");
      if (!path) continue;
      const e = (by[path] ??= { path, name: String(r.meta?.list ?? path),
        words: PLAIN_WORDS, need: 0, got: 0, cat: 0, pending: false });
      if (r.meta?.listNote) {
        e.name = String(r.meta.list ?? e.name);
        e.words = goodWords(r.meta.words) ?? e.words;
        e.pending = e.pending || !!r.pending;
      } else {
        const s = String(r.meta?.state ?? "need") as "need" | "got" | "cat";
        e[s] = (e[s] ?? 0) + 1;
      }
    }
    return Object.values(by).sort((a, b) => a.name.localeCompare(b.name));
}
export const lists = query({
  args: {},
  handler: async (ctx) => { await requireUser(ctx); return listsH(ctx); },
});
export const listsInternal = internalQuery({ args: {}, handler: (ctx) => listsH(ctx) });

async function itemsH(ctx: QueryCtx, path: string) {
    const rows = (await listRows(ctx)).filter((r) => r.meta?.path === path);
    const marker = rows.find((r) => r.meta?.listNote);
    return {
      name: String(marker?.meta?.list
        ?? path.replace(/^Lists\//, "").replace(/\.md$/, "")),
      words: goodWords(marker?.meta?.words) ?? PLAIN_WORDS,
      exists: !!marker,
      items: rows.filter((r) => !r.meta?.listNote)
        .sort((a, b) => (a.meta?.order ?? 0) - (b.meta?.order ?? 0))
        .map((r) => ({ id: r.chunkId, text: itemText(r),
          state: String(r.meta?.state ?? "need") as "need" | "got" | "cat",
          pending: !!r.pending })),
    };
}
export const items = query({
  args: { path: v.string() },
  handler: async (ctx, { path }) => { await requireUser(ctx); return itemsH(ctx, path); },
});
export const itemsInternal = internalQuery({
  args: { path: v.string() }, handler: (ctx, { path }) => itemsH(ctx, path),
});

/* ── writes ──────────────────────────────────────────────────────────── */

async function anchor(ctx: { db: any }, path: string) {
  const hit = (await listRows(ctx)).find((r) => r.meta?.path === path);
  if (!hit) throw new Error("unknown list");
  return { vault: String(hit.meta.vault ?? ""), name: String(hit.meta.list ?? "") };
}

function itemRow(vault: string, path: string, name: string, text: string,
                 state: string, order: number) {
  return {
    chunkId: listChunkId(vault, path, text), source: "tasks",
    timestamp: new Date().toISOString(), day: "list", month: "",
    location: `${path}#${order}`, text: `${name} list: ${text}`,
    meta: { vault, list: name, path, state, order },
    entryId: "", contentHash: "", pending: true,
  };
}

const stateArg = v.union(v.literal("need"), v.literal("got"), v.literal("cat"));
async function setStateH(ctx: MutationCtx, id: string, want: "need" | "got" | "cat") {
  const row = await byChunkId(ctx, id);
  if (!row || row.meta?.listNote || !row.meta?.path) throw new Error("not a list item");
  if (row.pending) throw new Error("that item is still syncing");
  const prior = { state: row.meta.state };
  await ctx.db.patch(row._id, { meta: { ...row.meta, state: want }, pending: true });
  return await ctx.db.insert("taskIntents", {
    kind: "listSet", chunkId: id, day: "list", vault: String(row.meta.vault ?? ""),
    path: String(row.meta.path), text: itemText(row), want, prior,
    requestedAt: Date.now(),
  });
}
export const setState = mutation({
  args: { id: v.string(), want: stateArg },
  handler: async (ctx, { id, want }) => { await requireUser(ctx); return setStateH(ctx, id, want); },
});
export const setStateInternal = internalMutation({
  args: { id: v.string(), want: stateArg },
  handler: (ctx, { id, want }) => setStateH(ctx, id, want),
});

async function addItemH(ctx: MutationCtx, path: string, text: string) {
  text = text.trim();
  if (!text) throw new Error("empty item");
  const { vault, name } = await anchor(ctx, path);
  const id = listChunkId(vault, path, text);
  const dup = await byChunkId(ctx, id);
  if (dup && !dup.hidden) {
    // exists in the catalog → this is "need it", not a duplicate
    if (dup.meta?.state !== "cat") throw new Error("already on the list");
    if (dup.pending) throw new Error("that item is still syncing");
    await ctx.db.patch(dup._id, { meta: { ...dup.meta, state: "need" }, pending: true });
    return await ctx.db.insert("taskIntents", {
      kind: "listSet", chunkId: id, day: "list", vault, path,
      text: itemText(dup), want: "need", prior: { state: "cat" },
      requestedAt: Date.now(),
    });
  }
  const rows = (await listRows(ctx)).filter((r) => r.meta?.path === path);
  const order = rows.reduce((m, r) => Math.max(m, r.meta?.order ?? 0), 0) + 1;
  const row = itemRow(vault, path, name, text, "need", order);
  if (dup) await ctx.db.replace(dup._id, row);
  else { await ctx.db.insert("items", row); await bump(ctx, "tasks", 1); }
  return await ctx.db.insert("taskIntents", {
    kind: "listAdd", chunkId: id, day: "list", vault, path, text, requestedAt: Date.now(),
  });
}
export const addItem = mutation({
  args: { path: v.string(), text: v.string() },
  handler: async (ctx, { path, text }) => { await requireUser(ctx); return addItemH(ctx, path, text); },
});
export const addItemInternal = internalMutation({
  args: { path: v.string(), text: v.string() },
  handler: (ctx, { path, text }) => addItemH(ctx, path, text),
});

async function editItemH(ctx: MutationCtx, id: string, newText: string) {
  newText = newText.trim();
  if (!newText) throw new Error("empty item");
  const row = await byChunkId(ctx, id);
  if (!row || row.meta?.listNote || !row.meta?.path) throw new Error("not a list item");
  if (row.pending) throw new Error("that item is still syncing");
  const vault = String(row.meta.vault ?? ""), path = String(row.meta.path);
  const freshId = listChunkId(vault, path, newText);
  const clash = await byChunkId(ctx, freshId);
  if (clash && !clash.hidden && clash.chunkId !== id) throw new Error("an item with that text already exists");
  const fresh = itemRow(vault, path, String(row.meta.list ?? ""), newText,
    String(row.meta.state ?? "need"), row.meta.order ?? 0);
  if (clash) await ctx.db.replace(clash._id, fresh);
  else { await ctx.db.insert("items", fresh); await bump(ctx, "tasks", 1); }
  await ctx.db.patch(row._id, { hidden: true, pending: true });
  return await ctx.db.insert("taskIntents", {
    kind: "listEdit", chunkId: id, day: "list", vault, path,
    text: itemText(row), newText, requestedAt: Date.now(),
  });
}
export const editItem = mutation({
  args: { id: v.string(), newText: v.string() },
  handler: async (ctx, { id, newText }) => { await requireUser(ctx); return editItemH(ctx, id, newText); },
});
export const editItemInternal = internalMutation({
  args: { id: v.string(), newText: v.string() },
  handler: (ctx, { id, newText }) => editItemH(ctx, id, newText),
});

async function removeItemH(ctx: MutationCtx, id: string) {
  const row = await byChunkId(ctx, id);
  if (!row || row.meta?.listNote || !row.meta?.path) throw new Error("not a list item");
  await ctx.db.patch(row._id, { hidden: true, pending: true });
  return await ctx.db.insert("taskIntents", {
    kind: "listRemove", chunkId: id, day: "list", vault: String(row.meta.vault ?? ""),
    path: String(row.meta.path), text: itemText(row), requestedAt: Date.now(),
  });
}
export const removeItem = mutation({
  args: { id: v.string() },
  handler: async (ctx, { id }) => { await requireUser(ctx); return removeItemH(ctx, id); },
});
export const removeItemInternal = internalMutation({
  args: { id: v.string() },
  handler: (ctx, { id }) => removeItemH(ctx, id),
});

/* The done verb: every got item shelves to the catalog head. */
export const reset = mutation({
  args: { path: v.string() },
  handler: async (ctx, { path }) => {
    await requireUser(ctx);
    const { vault } = await anchor(ctx, path);
    const got = (await listRows(ctx)).filter(
      (r) => r.meta?.path === path && !r.meta?.listNote && r.meta?.state === "got");
    if (!got.length) return null;
    for (const r of got) {
      await ctx.db.patch(r._id, { meta: { ...r.meta, state: "cat" }, pending: true });
    }
    return await ctx.db.insert("taskIntents", {
      kind: "listReset", chunkId: "", day: "list", vault, path, text: "",
      prior: { got: got.map((r) => r.chunkId) }, requestedAt: Date.now(),
    });
  },
});

async function createH(ctx: MutationCtx, name: string, words?: typeof PLAIN_WORDS) {
    // Mirror the applier's sanitizer so the placeholder path matches.
    const clean = name.trim().split(/\s+/).join(" ")
      .replace(/[/\\]/g, "-").replace(/^[. ]+|[. ]+$/g, "");
    if (!clean) throw new Error("empty list name");
    const path = `Lists/${clean}.md`;
    const rows = await listRows(ctx);
    if (rows.some((r) => r.meta?.path === path)) throw new Error("a list with that name already exists");
    const vault = String(rows[0]?.meta?.vault
      ?? (await ctx.db.query("items")
        .withIndex("by_source_timestamp", (q: any) => q.eq("source", "tasks"))
        .order("desc").first())?.meta?.vault ?? "");
    if (!vault) throw new Error("no vault known yet — index tasks first");
    const id = listNoteId(vault, path);
    const dup = await byChunkId(ctx, id);
    const marker = {
      chunkId: id, source: "tasks", timestamp: new Date().toISOString(),
      day: "list", month: "", location: `${path}#0`, text: `List: ${clean}`,
      meta: { vault, listNote: true, list: clean, path, words: words ?? PLAIN_WORDS },
      entryId: "", contentHash: "", pending: true,
    };
    if (dup) await ctx.db.replace(dup._id, marker);
    else { await ctx.db.insert("items", marker); await bump(ctx, "tasks", 1); }
    await ctx.db.insert("taskIntents", {
      kind: "listCreate", chunkId: id, day: "list", vault, path, text: clean,
      ...(words ? { words } : {}), requestedAt: Date.now(),
    });
    return path;
}
export const create = mutation({
  args: { name: v.string(), words: v.optional(wordsArg) },
  handler: async (ctx, { name, words }) => { await requireUser(ctx); return createH(ctx, name, words); },
});
export const createInternal = internalMutation({
  args: { name: v.string(), words: v.optional(wordsArg) },
  handler: (ctx, { name, words }) => createH(ctx, name, words),
});

/* ── the wording generator (live LLM, creation-time only) ────────────── */

export async function vocabWords(name: string): Promise<typeof PLAIN_WORDS> {
    const lat = (m: { latency?: string }) =>
      Number((m.latency ?? "").replace(/\D/g, "")) || 99;
    const pick = presets().sort((a, b) => lat(a) - lat(b))[0];
    if (!pick || pick.backend !== "openai-compatible" || !pick.base_url) return PLAIN_WORDS;
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10_000);
      const r = await fetch(`${pick.base_url}/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${pick._key}` },
        body: JSON.stringify({
          model: pick.model, max_tokens: 100, temperature: 0.7,
          messages: [{ role: "user", content:
            `A checklist app names its verbs per list. List name: ${JSON.stringify(name)}.\n` +
            `Reply with ONLY a JSON object {"need":"...","got":"...","done":"..."}.\n` +
            `need = short phrase for items still to act on (e.g. "to pack"); ` +
            `got = past-tense state (e.g. "packed"); ` +
            `done = short label for finishing the whole run (e.g. "trip done"). ` +
            `Lowercase, at most 3 words each, no punctuation.` }],
        }),
        signal: ctrl.signal,
      });
      clearTimeout(timer);
      const j = await r.json();
      const txt: string = j?.choices?.[0]?.message?.content ?? "";
      const m = /\{[^}]*\}/.exec(txt);
      const w = JSON.parse(m ? m[0] : txt);
      const clean = (s: unknown) => String(s ?? "").toLowerCase()
        .replace(/[^\p{L}\p{N} '-]/gu, "").trim().split(/\s+/).slice(0, 3).join(" ").trim();
      const out = { need: clean(w.need), got: clean(w.got), done: clean(w.done) };
      return out.need && out.got && out.done ? out : PLAIN_WORDS;
    } catch {
      return PLAIN_WORDS;
    }
}
export const vocab = action({
  args: { name: v.string() },
  handler: async (ctx, { name }): Promise<typeof PLAIN_WORDS> => {
    await requireUserAction(ctx);
    return vocabWords(name);
  },
});
