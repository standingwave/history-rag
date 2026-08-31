/* Design D (widget grid) from SPEC-dashboard-tasks.md Part B: Tasks
   (large, interactive checkboxes), Agenda + Notes (small); Search / Ask /
   Browse and the reading view are sheets from ./Archive. Sheets route by
   the `#w=` hash; `#w=x:<id>` is the reading view. */
import { useEffect, useRef, useState, type JSX, type ReactNode } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "../convex/_generated/api";
import type { Id } from "../convex/_generated/dataModel";
import { SearchSheet, AskSheet, BrowseSheet, Detail, Row, Answer } from "./Archive";
import { useAction } from "convex/react";
import { shortDate, hhmm, Linkify, describe } from "./render";

type Item = {
  id: string; source: string; timestamp: string; day: string;
  location: string; text: string; meta: any; pending: boolean;
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function localDay(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dayLabel(iso: string) {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return `${DAYS[dt.getDay()]} ${MONTHS[m - 1]} ${d}`;
}
function timeOnly(ts: string) {
  const d = new Date(ts);
  if (isNaN(d.getTime()) || (!d.getHours() && !d.getMinutes())) return "all day";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function title(t: Item) {
  return t.text.split("\n", 1)[0].replace(/^Task: /, "");
}
/* Re-render every `ms` while mounted: elapsed counters, intent ages. */
export function useTick(ms: number, on = true) {
  const [, set] = useState(0);
  useEffect(() => { if (!on) return; const t = setInterval(() => set((n) => n + 1), ms); return () => clearInterval(t); }, [ms, on]);
}
const YOUNG_MS = 60_000;   // an intent younger than this is "on its way"; older is "queued"

/* Errors that belong to this screen: a red box in the waiting line's
   shape, dismissable, gone by itself after 8 s. */
export function useErrors() {
  const [errs, setErrs] = useState<{ id: number; msg: string }[]>([]);
  const dismiss = (id: number) => setErrs((e) => e.filter((x) => x.id !== id));
  const push = (e: unknown) => {
    const id = Date.now() + Math.random();
    setErrs((old) => [...old, { id, msg: String(e instanceof Error ? e.message : e).replace(/^.*Uncaught Error: /, "").split("\n")[0] }]);
    setTimeout(() => dismiss(id), 8000);
  };
  const view = errs.map((e) => <ErrBox key={e.id} onClose={() => dismiss(e.id)}>{e.msg}</ErrBox>);
  return { push, view };
}
export function ErrBox({ children, onClose, action }: { children: ReactNode; onClose?: () => void; action?: ReactNode }) {
  return <div className="errbox"><span>{children}</span>{action ?? (onClose && <button className="lnk" onClick={onClose}>✕</button>)}</div>;
}
export function Skeleton({ widths, style }: { widths: number[]; style?: React.CSSProperties }) {
  return <div style={{ display: "grid", gap: 8, ...style }}>{widths.map((w, i) => <span key={i} className="skel" style={{ width: `${w}%` }} />)}</div>;
}

function period(h: number) {
  return h < 5 ? "night" : h < 12 ? "morning" : h < 18 ? "day" : "night";
}
const ORDER = ["agenda", "tasks", "brief"];

export function Today() {
  const [day, setDay] = useState(localDay());
  const [sheet, setSheet] = useState<string>(
    () => new URLSearchParams(location.hash.slice(1)).get("w") ?? "");
  useEffect(() => {
    const t = setInterval(() => setDay(localDay()), 60_000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    const onPop = () => setSheet(new URLSearchParams(location.hash.slice(1)).get("w") ?? "");
    addEventListener("popstate", onPop);
    return () => removeEventListener("popstate", onPop);
  }, []);
  const open = (id: string) => { history.pushState(null, "", `#w=${id}`); setSheet(id); };
  const close = () => { history.pushState(null, "", "#"); setSheet(""); };

  const tasks = useQuery(api.today.tasks, { day });
  const agenda = useQuery(api.today.agenda, { day });
  const brief = useQuery(api.brief.latest, {});
  // Stable arg (local midnight), so no resubscribe loop; filtered to "after
  // now" where it's used.
  const upcoming = useQuery(api.today.upcoming, { after: new Date(day + "T00:00:00").toISOString() });
  const latest = useQuery(api.today.latestTaskDay, {});
  const intents = useQuery(api.today.intents, {});
  const waiting = (intents ?? []).filter((i: any) => !i.appliedAt && !i.error).length;
  const hour = new Date().getHours();
  const openId = (id: string) => open(`x:${encodeURIComponent(id)}`);

  if (sheet === "tasks") return <TasksSheet day={day} tasks={tasks} latest={latest} onBack={close} />;
  if (sheet === "agenda") return <AgendaSheet day={day} agenda={agenda} onBack={close} onOpen={openId} />;
  if (sheet === "brief") return <BriefSheet brief={brief} onBack={close} onOpen={openId} />;
  if (sheet.startsWith("x:")) return <Detail id={decodeURIComponent(sheet.slice(2))} onBack={() => history.back()} />;
  if (sheet === "search") return <SearchSheet onBack={close} onOpen={openId} />;
  if (sheet === "ask") return <AskSheet onBack={close} onOpen={openId} />;
  if (sheet === "browse") return <BrowseSheet onBack={close} onOpen={openId} />;

  const tiles: Record<string, JSX.Element> = {
    tasks: <TasksTile key="tasks" tasks={tasks} hour={hour} waiting={waiting} onOpen={() => open("tasks")} />,
    agenda: <AgendaTile key="agenda" agenda={agenda} upcoming={upcoming} onOpen={() => open("agenda")} />,
    brief: <BriefTile key="brief" brief={brief} onOpen={() => open("brief")} />,
  };
  return (
    <section>
      <div className="daterow">
        <span>{dayLabel(day)} · {period(hour)}</span>
        <span>
          <button className="lnk" onClick={() => open("search")}>search</button>{" · "}
          <button className="lnk" onClick={() => open("ask")}>ask</button>{" · "}
          <button className="lnk" onClick={() => open("browse")}>browse</button>
        </span>
      </div>
      <div className="grid">{ORDER.map((id) => tiles[id])}</div>
    </section>
  );
}

function TasksTile({ tasks, hour, waiting, onOpen }: { tasks?: Item[]; hour: number; waiting: number; onOpen: () => void }) {
  const night = period(hour) === "night";
  const list = tasks ?? [];
  const main = list.filter((t) => t.meta.section !== "Routine");
  const done = main.filter((t) => t.meta.done);
  const shown = (night ? done : main.filter((t) => !t.meta.done)).slice(0, 5);
  const more = (night ? done : main.filter((t) => !t.meta.done)).length - shown.length;
  return (
    <button className="tile large" onClick={onOpen}>
      <div className="thead"><span style={{ color: "#facc15" }}>☑ Tasks</span>
        <span className="muted">{tasks ? `${done.length}/${main.length} done` : "…"} ›</span></div>
      {!tasks && <Skeleton widths={[70, 55, 62]} style={{ margin: "6px 0" }} />}
      {shown.map((t) => <TaskRow key={t.id} t={t} compact />)}
      {tasks && !main.length && <p className="muted">no note for today</p>}
      {tasks && main.length > 0 && (
        <p className="muted small">
          {more > 0 ? `+${more} more · ` : ""}{done.length} done · {list.length - main.length} routine
          {waiting > 0 && <span className="waiting"> · {waiting} waiting</span>}
        </p>
      )}
    </button>
  );
}

type Acts = {
  onToggle: () => void; onEdit: (text: string) => void; onDelete: () => void;
  onSubToggle: (text: string) => void; onSubAdd: (text: string) => void;
  onSubEdit: (text: string, newText: string) => void; onSubDelete: (text: string) => void;
  onAttach: (text: string) => void;
  onAttachFile: (file: File, onProgress: (loaded: number, total: number) => void) => Promise<void>;
};
type Busy = { label: string; young: boolean };
function busyLabel(b?: Busy) {
  return b ? <span className={`muted mono small ${b.young ? "pulse" : ""}`}>{b.young ? `${b.label}…` : "queued"}</span> : null;
}

/* A note line under a task: markdown links as links, bare URLs too. */
function NoteLine({ line }: { line: string }) {
  const m = /^\[([^\]]*)\]\((\S+)\)$/.exec(line);
  if (m) return <div className="notel">🔗 <a className="link" href={m[2]} target="_blank" rel="noreferrer">{m[1] || m[2]}</a></div>;
  return <div className="notel muted">📝 <Linkify text={line} /></div>;
}

/* One line under a task: toggle glyph, text, its own … for edit/delete,
   or the underlined input while editing. */
function SubRow({ s, acts, mode, setMode, busy }:
  { s: { text: string; done: boolean }; acts?: Acts; mode: Mode; setMode: (m: Mode) => void; busy?: Busy }) {
  const [draft, setDraft] = useState(s.text);
  const save = () => { const v = draft.trim(); if (v && v !== s.text) acts?.onSubEdit(s.text, v); setMode(null); };
  if (mode === "edit") return (
    <div className="sub edit"><span className="glyph">{s.done ? "●" : "○"}</span>
      <input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setMode(null); }} />
      <button className="go" onClick={save}>save</button><button className="cancelv" onClick={() => setMode(null)}>cancel</button>
    </div>
  );
  return (
    <>
      <div className={`sub ${s.done ? "done" : ""} ${busy ? "pending" : ""}`}>
        {acts && !busy ? <button className="glyph" onClick={() => acts.onSubToggle(s.text)}>{s.done ? "●" : "○"}</button>
              : <span className={`glyph ${busy?.young ? "pulse" : ""}`}>{s.done ? "●" : "○"}</span>}
        <span>{s.text}</span>
        {busyLabel(busy)}
        {acts && <button className={`more ${mode ? "on" : ""}`} onClick={() => setMode(mode ? null : "acts")}>…</button>}
      </div>
      {mode === "acts" && <div className="cbar subbar">
        <button onClick={() => { setDraft(s.text); setMode("edit"); }}>edit</button>
        <button className="danger" onClick={() => setMode("delete")}>delete</button></div>}
      {mode === "delete" && <div className="confirm subc">delete:
        <button className="yes" onClick={() => { acts?.onSubDelete(s.text); setMode(null); }}>Yes</button>
        <button className="no" onClick={() => setMode(null)}>No</button></div>}
    </>
  );
}

/* One task. `acts` makes it editable: the row ends in a faint … that opens
   the action strip (edit · delete); edit swaps the text for an underlined
   input in place; delete asks once, naming what the block takes with it. */
type Mode = "acts" | "edit" | "delete" | null;
function TaskRow({ t, compact, acts, fixed, placeholder, busy, subBusy, mode, setMode }:
  { t: Item; compact?: boolean; acts?: Acts; fixed?: boolean; placeholder?: Busy; busy?: Busy;
    subBusy?: Record<string, Busy>; mode?: Mode; setMode?: (m: Mode) => void }) {
  const subs: { text: string; done: boolean }[] = t.meta.subtasks ?? [];
  const atts: string[] = t.meta.attachments ?? [];
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [subMode, setSubMode] = useState<{ i: number; m: Mode } | null>(null);
  const [addingSub, setAddingSub] = useState(false);
  const [attaching, setAttaching] = useState<"pick" | "link" | "note" | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [attDraft, setAttDraft] = useState("");
  const notes: string[] = t.meta.notes ?? [];
  const sendAtt = () => { const v = attDraft.trim(); if (v) acts?.onAttach(v); setAttDraft(""); setAttaching(null); };
  // A file on its way up: local to this row, from the first byte.
  const [upload, setUpload] = useState<{ file: File; loaded: number; err?: string } | null>(null);
  const startUpload = (file: File) => {
    setUpload({ file, loaded: 0 });
    acts?.onAttachFile(file, (loaded) => setUpload((u) => u && { ...u, loaded }))
      .then(() => setUpload(null), (e) => setUpload({ file, loaded: 0, err: String(e instanceof Error ? e.message : e) }));
  };
  const [subDraft, setSubDraft] = useState("");
  const subRef = useRef<HTMLInputElement>(null);
  const addSub = () => {
    const v = subDraft.trim();
    if (!v) { setAddingSub(false); return; }
    setSubDraft(""); acts?.onSubAdd(v); subRef.current?.focus();
  };
  const glyph = t.meta.done ? "●" : "○";
  // A placeholder (add/edit awaiting the Mac) has no line in the note yet.
  const editable = !!acts && !placeholder;
  const pulse = (placeholder ?? busy)?.young ? "pulse" : "";
  const startEdit = () => { setDraft(title(t)); setMode?.("edit"); };
  const save = () => { const v = draft.trim(); if (v && v !== title(t)) acts?.onEdit(v); setMode?.(null); };
  if (mode === "edit") return (
    <div className="trow">
      <div className="tline edit">
        <span className="glyph">{glyph}</span>
        <input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setMode?.(null); }} />
        <button className="go" onClick={save}>save</button>
        <button className="lnk" onClick={() => setMode?.(null)}>✕</button>
      </div>
    </div>
  );
  const what = [subs.length && `its ${subs.length} subtask${subs.length > 1 ? "s" : ""}`,
                atts.length && `${atts.length} attachment${atts.length > 1 ? "s" : ""}`].filter(Boolean).join(" and ");
  return (
    <div className={`trow ${t.meta.done ? "done" : ""} ${t.pending ? "pending" : ""}`}>
      <div className="tline">
        {editable
          ? <button className={`glyph ${pulse}`} aria-label="toggle" onClick={(e) => { e.stopPropagation(); acts.onToggle(); }}>{glyph}</button>
          : <span className={`glyph ${pulse}`}>{glyph}</span>}
        <span className="ttext" onClick={() => !compact && setOpen(!open)}>{title(t)}</span>
        {placeholder ? busyLabel(placeholder) : <span className="muted mono small">
          {subs.length ? `${subs.filter((s) => s.done).length}/${subs.length}` : ""}
        </span>}
        {editable && !fixed && <button className={`more ${mode ? "on" : ""}`} aria-label="actions"
          onClick={() => setMode?.(mode ? null : "acts")}>…</button>}
      </div>
      {mode === "acts" && (
        <div className="cbar">
          <button onClick={startEdit}>edit</button>
          <button onClick={() => { setOpen(true); setAddingSub(true); setMode?.(null); }}>subtask</button>
          <button onClick={() => { setOpen(true); setAttaching("pick"); setMode?.(null); }}>attach</button>
          <button className="danger" onClick={() => setMode?.("delete")}>delete</button>
        </div>
      )}
      {mode === "delete" && (
        <div className="confirm">delete{what ? <span className="muted small">&nbsp;(takes {what})</span> : ""}:
          <button className="yes" onClick={() => { acts?.onDelete(); setMode?.(null); }}>Yes</button>
          <button className="no" onClick={() => setMode?.(null)}>No</button>
        </div>
      )}
      {open && !compact && (
        <div className="tdetail">
          {subs.map((s, i) => <SubRow key={s.text} s={s} acts={editable ? acts : undefined} busy={subBusy?.[normKey(s.text)]}
            mode={subMode?.i === i ? subMode.m : null} setMode={(m) => setSubMode(m ? { i, m } : null)} />)}
          {addingSub && (
            <div className="sub edit composer"><span className="glyph">○</span>
              <input ref={subRef} autoFocus placeholder="new subtask" value={subDraft} onChange={(e) => setSubDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") addSub(); if (e.key === "Escape") setAddingSub(false); }} />
              <button className="go" onClick={addSub}>add</button>
              <button className="cancelv" onClick={() => setAddingSub(false)}>cancel</button>
            </div>
          )}
          {atts.map((a: string) => <div key={a} className="muted">📎 {a}</div>)}
          {notes.map((n, i) => <NoteLine key={i} line={n} />)}
          {upload && !upload.err && (
            <div className="sub pending"><span className="pulse" style={{ color: "#facc15" }}>🖼</span>
              <span className="muted">{upload.file.name}</span>
              <span className="muted mono small">{mb(upload.loaded)} / {mb(upload.file.size)} MB</span></div>
          )}
          {upload?.err && <ErrBox action={<button className="lnk" onClick={() => startUpload(upload.file)}>retry</button>}>🖼 {upload.file.name} · {upload.err}</ErrBox>}
          {busy?.label === "attaching" && <div className="sub pending"><span className={busy.young ? "pulse" : ""} style={{ color: "#facc15" }}>📎</span>{busyLabel(busy)}</div>}
          {attaching === "pick" && (
            <div className="cbar" style={{ marginLeft: 0 }}>
              <button onClick={() => setAttaching("link")}>🔗 link</button>
              <button onClick={() => setAttaching("note")}>📝 note</button>
              <button onClick={() => fileRef.current?.click()}>🖼 file</button>
              <button onClick={() => setAttaching(null)}>cancel</button>
              <input ref={fileRef} type="file" hidden accept="image/*,application/pdf,.txt,.md"
                onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) { startUpload(f); setAttaching(null); } }} />
            </div>
          )}
          {(attaching === "link" || attaching === "note") && (
            <div className="sub edit composer"><span>{attaching === "link" ? "🔗" : "📝"}</span>
              <input autoFocus type={attaching === "link" ? "url" : "text"} inputMode={attaching === "link" ? "url" : "text"}
                placeholder={attaching === "link" ? "https://…" : "a note under this task"}
                value={attDraft} onChange={(e) => setAttDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") sendAtt(); if (e.key === "Escape") setAttaching(null); }} />
              <button className="go" onClick={sendAtt}>attach</button>
              <button className="cancelv" onClick={() => setAttaching(null)}>cancel</button>
            </div>
          )}
          {t.meta.days > 1 && <div className="muted">on the list since {shortDate(t.meta.first_seen)} · {t.meta.days} days</div>}
          <a href={`obsidian://open?vault=${encodeURIComponent(t.meta.vault)}&file=${t.day}`}>open in Obsidian ↗</a>
        </div>
      )}
      {upload && !upload.err && <div className="prog"><i style={{ width: `${Math.round(100 * upload.loaded / Math.max(1, upload.file.size))}%` }} /></div>}
    </div>
  );
}
const mb = (n: number) => (n / 1048576).toFixed(1);
const normKey = (s: string) => s.split(/\s+/).filter(Boolean).join(" ").toLowerCase();

/* POST a file to a Convex upload URL with progress (fetch has none). */
function uploadWithProgress(url: string, file: File, onProgress: (loaded: number, total: number) => void) {
  return new Promise<string>((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    x.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    x.upload.onprogress = (e) => onProgress(e.loaded, e.total || file.size);
    x.onerror = () => reject(new Error("upload failed (network)"));
    x.onload = () => {
      if (x.status < 200 || x.status >= 300) return reject(new Error(`upload failed (${x.status})`));
      try { resolve(JSON.parse(x.responseText).storageId); } catch { reject(new Error("upload failed (bad response)")); }
    };
    x.send(file);
  });
}

const KIND_VERB: Record<string, string> = { toggle: "apply", add: "add", edit: "edit", delete: "delete", attach: "attach" };

function TasksSheet({ day, tasks, latest, onBack }:
  { day: string; tasks?: Item[]; latest?: string | null; onBack: () => void }) {
  const toggle = useMutation(api.today.toggle);
  const add = useMutation(api.today.add);
  const edit = useMutation(api.today.edit);
  const remove = useMutation(api.today.remove);
  const subToggle = useMutation(api.today.subToggle);
  const subAdd = useMutation(api.today.subAdd);
  const subEdit = useMutation(api.today.subEdit);
  const subRemove = useMutation(api.today.subRemove);
  const attach = useMutation(api.today.attach);
  const uploadUrl = useMutation(api.today.uploadUrl);
  const attachFile = useMutation(api.today.attachFile);
  const intents = useQuery(api.today.intents, {});
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");
  const [active, setActive] = useState<{ id: string; mode: "acts" | "edit" | "delete" } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useTick(5000, (intents ?? []).some((i: any) => !i.appliedAt && !i.error));
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const errors = (intents ?? []).filter((i: { id: string; error: string | null; requestedAt: number }) =>
    i.error && Date.now() - i.requestedAt < 3600e3 && !dismissed.has(i.id));
  const { push: fail, view: errView } = useErrors();
  const unapplied = (intents ?? []).filter((i: any) => !i.appliedAt && !i.error);
  const oldest = unapplied.reduce((m: number, i: any) => Math.min(m, i.requestedAt), Infinity);
  const stalled = unapplied.length > 0 && Date.now() - oldest > YOUNG_MS;
  const list = tasks ?? [];
  const main = list.filter((t) => t.meta.section !== "Routine");
  const routine = list.filter((t) => t.meta.section === "Routine");
  const openT = main.filter((t) => !t.meta.done), doneT = main.filter((t) => t.meta.done);
  const submit = () => {
    const text = draft.trim();
    if (!text) { setComposing(false); return; }
    setDraft("");
    add({ day, text }).catch(fail);
    inputRef.current?.focus();
  };
  useEffect(() => { if (composing) inputRef.current?.scrollIntoView({ block: "nearest" }); }, [composing]);
  const actsFor = (t: Item): Acts => ({
    onToggle: () => void toggle({ id: t.id }).catch(fail),
    onEdit: (newText) => void edit({ id: t.id, newText }).catch(fail),
    onDelete: () => void remove({ id: t.id }).catch(fail),
    onSubToggle: (text) => void subToggle({ id: t.id, text }).catch(fail),
    onSubAdd: (text) => void subAdd({ id: t.id, text }).catch(fail),
    onSubEdit: (text, newText) => void subEdit({ id: t.id, text, newText }).catch(fail),
    onSubDelete: (text) => void subRemove({ id: t.id, text }).catch(fail),
    onAttach: (text) => void attach({ id: t.id, text }).catch(fail),
    onAttachFile: async (file, onProgress) => {
      const url = await uploadUrl({});
      const storageId = await uploadWithProgress(url, file, onProgress);
      await attachFile({ id: t.id, storageId: storageId as Id<"_storage">, name: file.name });
    },
  });
  // chunk id → what's in flight for it, so placeholders read "adding…" and
  // can't be toggled before they exist in the note.
  const inflight: Record<string, Busy> = {};        // placeholders
  const rowBusy: Record<string, Busy> = {};         // toggles, deletes, attachments on a row
  const subBusy: Record<string, Record<string, Busy>> = {};   // chunkId → sub text → busy
  const LABEL: Record<string, string> = { toggle: "ticking", add: "adding", edit: "editing", delete: "deleting", attach: "attaching" };
  for (const i of unapplied) {
    const b: Busy = { label: LABEL[i.kind] ?? i.kind, young: Date.now() - i.requestedAt < YOUNG_MS };
    if (i.kind === "attach") rowBusy[i.chunkId] = b;
    else if (i.parent) (subBusy[i.chunkId] ??= {})[normKey(i.text)] = b;
    else if (i.kind === "add") inflight[i.chunkId] = b;
    else if (i.kind === "edit" && i.newId) inflight[i.newId] = b;
    else rowBusy[i.chunkId] = b;
  }
  const row = (t: Item, fixed = false) => <TaskRow key={t.id} t={t} acts={actsFor(t)} fixed={fixed}
    placeholder={t.pending ? inflight[t.id] : undefined} busy={t.pending ? rowBusy[t.id] : undefined} subBusy={subBusy[t.id]}
    mode={active?.id === t.id ? active.mode : null}
    setMode={(m) => setActive(m ? { id: t.id, mode: m } : null)} />;
  const vault = list[0]?.meta.vault ?? "";
  const inputOpen = composing || active?.mode === "edit";
  const errWhat = (e: { kind: string; text: string; parent: string | null }) =>
    e.kind === "attach" ? `attach "${e.text}" to "${e.parent}"`
    : e.parent ? `${KIND_VERB[e.kind] ?? e.kind} subtask "${e.text}"` : `${KIND_VERB[e.kind] ?? e.kind} "${e.text}"`;
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Oriel</button>
        <span>☑ Tasks · {doneT.length}/{main.length} done · {dayLabel(day)}</span></div>
      {stalled && <div className="wait"><span>{unapplied.length} change{unapplied.length > 1 ? "s" : ""} waiting for the Mac</span>
        <span className="muted">since {new Date(oldest).toTimeString().slice(0, 5)}</span></div>}
      {errView}
      {errors.map((e: { id: string; kind: string; text: string; parent: string | null; error: string | null }) =>
        <ErrBox key={e.id} onClose={() => setDismissed((d) => new Set(d).add(e.id))}>couldn't {errWhat(e)}: {e.error}</ErrBox>)}
      {!tasks && <Skeleton widths={[70, 55, 62, 48]} style={{ margin: "8px 0" }} />}
      {tasks && !list.length && (
        <p className="muted">no note for {day}{latest ? ` — latest is ${latest}` : ""}</p>
      )}
      {openT.map((t) => row(t))}
      {doneT.map((t) => row(t))}
      {composing && (
        <div className="trow composer">
          <span className="glyph">○</span>
          <input ref={inputRef} autoFocus placeholder="new task" value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") setComposing(false); }} />
          <button className="go" onClick={submit}>add</button>
          <button className="cancelv" onClick={() => setComposing(false)}>cancel</button>
        </div>
      )}
      {routine.length > 0 && <p className="sect">ROUTINE</p>}
      {routine.map((t) => row(t, true))}
      {vault && <p><a href={`obsidian://open?vault=${encodeURIComponent(vault)}&file=${day}`}>open today's note in Obsidian ↗</a></p>}
      {!inputOpen && <button className="fab" aria-label="add a task" onClick={() => { setActive(null); setComposing(true); }}>+</button>}
    </section>
  );
}

type Brief = {
  question: string; answer: string | null; citations: string[]; note: string | null; model: string | null;
  generatedAt: number | null; ms: number | null; lastError: { error: string; at: number } | null;
};
const ago = (t: number) => {
  const m = Math.round((Date.now() - t) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

/* start/end for an event chunk: meta.start/end when present, else the
   indexed timestamp and half an hour. */
function evTimes(e: Item) {
  const start = new Date(e.meta?.start ?? e.timestamp);
  const end = e.meta?.end ? new Date(e.meta.end) : new Date(start.getTime() + 30 * 60000);
  return { start, end, allDay: !!e.meta?.all_day || timeOnly(e.timestamp) === "all day" };
}

/* Today's events, always the first tile and full width: one line when
   there's nothing (then the next event anywhere ahead, if known),
   otherwise the next few with the upcoming one marked. */
function AgendaTile({ agenda, upcoming, onOpen }: { agenda?: Item[]; upcoming?: Item[]; onOpen: () => void }) {
  const now = Date.now();
  const list = agenda ?? [];
  const nextI = list.findIndex((e) => new Date(e.timestamp).getTime() > now);
  const start = Math.max(0, Math.min(nextI < 0 ? list.length : nextI, list.length - 4));
  const shown = list.slice(start, start + 4);
  const label = (e: Item) => describe(e).title;
  const next = (upcoming ?? []).find((e) => new Date(e.timestamp).getTime() > now || evTimes(e).end.getTime() > now);
  return (
    <button className="tile large agenda" onClick={onOpen}>
      <div className="thead" style={shown.length ? undefined : { marginBottom: 0 }}>
        <span style={{ color: "#f472b6" }}>Agenda</span>
        <span className="muted">{!agenda ? "…" : list.length ? `${list.length} event${list.length > 1 ? "s" : ""}` : "no events today"} ›</span>
      </div>
      {!agenda && <span className="skel" style={{ width: "60%", margin: "4px 0" }} />}
      {shown.map((e, i) => {
        const past = new Date(e.timestamp).getTime() <= now && timeOnly(e.timestamp) !== "all day";
        return (
          <div key={e.id} className={`lrow ${start + i === nextI ? "next" : ""} ${past ? "past" : ""}`}>
            <span className="time">{timeOnly(e.timestamp) === "all day" ? "all day" : hhmm(e.timestamp)}</span>
            <span className="grow">{label(e)}</span>
          </div>
        );
      })}
      {list.length > start + 4 && <p className="muted small" style={{ margin: "2px 0 0" }}>+{list.length - start - 4} more</p>}
      {agenda && nextI < 0 && next && (
        <p className="muted small" style={{ margin: shown.length ? "4px 0 0" : "6px 0 0" }}>
          next: {describe(next).title} · {dayLabel(next.day)} · {timeOnly(next.timestamp) === "all day" ? "all day" : hhmm(next.timestamp)}
        </p>
      )}
    </button>
  );
}

/* "What did I do over the previous 24 hours?" — the stored answer, rendered
   the same way as the sheet but compact and clamped with a fade. */
function BriefTile({ brief, onOpen }: { brief?: Brief; onOpen: () => void }) {
  return (
    <button className="tile large brief" onClick={onOpen}>
      <div className="thead"><span style={{ color: "#a78bfa" }}>Last 24 h</span>
        <span className="muted">{brief?.generatedAt ? ago(brief.generatedAt) : ""} ›</span></div>
      {!brief && <Skeleton widths={[92, 84, 60]} style={{ margin: "6px 0" }} />}
      {brief && !brief.answer && <p className="muted">{brief.lastError ? `couldn't answer: ${brief.lastError.error}` : "not generated yet — open to run it"}</p>}
      {brief?.answer && <div className="clamp"><Answer text={brief.answer} compact /></div>}
    </button>
  );
}

function BriefSheet({ brief, onBack, onOpen }: { brief?: Brief; onBack: () => void; onOpen: (id: string) => void }) {
  const refresh = useAction(api.brief.refresh);
  const [busy, setBusy] = useState(false);
  const [t0, setT0] = useState(0);
  useTick(1000, busy);
  const { push: fail, view: errView } = useErrors();
  const run = () => { setBusy(true); setT0(Date.now()); refresh({}).catch(fail).finally(() => setBusy(false)); };
  const secs = busy ? Math.round((Date.now() - t0) / 1000) : 0;
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Oriel</button><span>Last 24 h</span></div>
      <p className="muted small">{brief?.question ?? "…"}</p>
      {errView}
      {brief?.lastError && !busy && <ErrBox>refresh {ago(brief.lastError.at)} failed: {brief.lastError.error}</ErrBox>}
      {busy && <p className="muted"><span className="pulse">thinking with {brief?.model ?? "the default model"}</span> · <span className="mono">{secs} s</span></p>}
      {!brief && <Skeleton widths={[96, 88, 92, 60]} />}
      <div className={busy ? "stale" : ""}>
        {brief?.answer && <Answer text={brief.answer} onOpen={onOpen} />}
        {brief && !brief.answer && !busy && <p className="muted">nothing yet</p>}
      </div>
      {brief?.generatedAt && !busy && <p className="muted mono small">
        {brief.model} · {ago(brief.generatedAt)} · {((brief.ms ?? 0) / 1000).toFixed(0)} s{brief.note ? ` · ${brief.note}` : ""}
        {brief.citations.length === 0 ? " · no sources cited" : ` · ${brief.citations.length} sources`}</p>}
      <div className="cbar slim"><button disabled={busy} className={busy ? "pulse" : ""} onClick={run}>{busy ? "refreshing…" : "refresh now"}</button></div>
    </section>
  );
}

/* The agenda as a day calendar: hour lines, events as positioned blocks
   (overlaps share the width), all-day events as chips above, a line for
   now. Tap a block for the reading view. */
const HOUR_PX = 52;
function AgendaSheet({ day, agenda, onBack, onOpen }:
  { day: string; agenda?: Item[]; onBack: () => void; onOpen: (id: string) => void }) {
  useTick(60_000, day === localDay());
  const evs = (agenda ?? []).map((e) => ({ e, ...evTimes(e) }));
  const allDay = evs.filter((x) => x.allDay);
  const timed = evs.filter((x) => !x.allDay).sort((a, b) => a.start.getTime() - b.start.getTime());
  const dayStart = new Date(day + "T00:00:00").getTime();
  const mins = (d: Date) => (d.getTime() - dayStart) / 60000;
  const loH = Math.min(8, ...timed.map((x) => Math.floor(mins(x.start) / 60)));
  const hiH = Math.max(18, ...timed.map((x) => Math.ceil(Math.min(mins(x.end), 1440) / 60)));
  // Overlap layout: transitive clusters, then first-free-column within each.
  type Laid = (typeof timed)[number] & { col: number; cols: number };
  const laid: Laid[] = [];
  let cluster: Laid[] = [], colEnds: number[] = [], clusterEnd = -1;
  const closeCluster = () => { for (const x of cluster) x.cols = colEnds.length; cluster = []; colEnds = []; };
  for (const x of timed) {
    const s = mins(x.start), en = Math.max(mins(x.end), s + 24 / (HOUR_PX / 60));
    if (s >= clusterEnd && cluster.length) closeCluster();
    let col = colEnds.findIndex((e) => e <= s);
    if (col < 0) { col = colEnds.length; colEnds.push(en); } else colEnds[col] = en;
    const l = Object.assign(x, { col, cols: 1 });
    cluster.push(l); laid.push(l);
    clusterEnd = Math.max(clusterEnd, en);
  }
  if (cluster.length) closeCluster();
  const nowMin = mins(new Date());
  const top = (m: number) => ((m - loH * 60) / 60) * HOUR_PX;
  const hours = []; for (let h = loH; h <= hiH; h++) hours.push(h);
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Oriel</button>
        <span>Agenda · {dayLabel(day)}</span></div>
      {agenda === undefined && <Skeleton widths={[80, 62, 71]} style={{ margin: "8px 0" }} />}
      {agenda && !evs.length && <p className="muted">nothing scheduled</p>}
      {allDay.length > 0 && (
        <div className="chips" style={{ marginBottom: 10 }}>
          {allDay.map(({ e }) => <button key={e.id} className="chip on" style={{ color: "#f472b6" }}
            onClick={() => onOpen(e.id)}>{describe(e).title}</button>)}
        </div>
      )}
      {timed.length > 0 && (
        <div className="cal" style={{ height: (hiH - loH) * HOUR_PX + 14 }}>
          {hours.map((h) => (
            <div key={h} className="hline" style={{ top: (h - loH) * HOUR_PX }}>
              <span>{String(h).padStart(2, "0")}:00</span>
            </div>
          ))}
          {day === localDay() && nowMin >= loH * 60 && nowMin <= hiH * 60 &&
            <div className="nowline" style={{ top: top(nowMin) }} />}
          <div className="evarea">
            {laid.map((x) => {
              const s = mins(x.start), en = Math.min(mins(x.end), 1440);
              const past = x.end.getTime() < Date.now() && day === localDay();
              return (
                <button key={x.e.id} className={`ev ${past ? "past" : ""}`} onClick={() => onOpen(x.e.id)}
                  style={{ top: top(s), height: Math.max(24, ((en - s) / 60) * HOUR_PX - 2),
                           left: `${(x.col / x.cols) * 100}%`, width: `calc(${100 / x.cols}% - 3px)` }}>
                  <span className="evt">{hhmm(x.start.toISOString())}–{hhmm(x.end.toISOString())}</span>
                  <span className="evn">{describe(x.e).title}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

function StatTile({ title, color, stat, caption, loading, onOpen }:
  { title: string; color: string; stat: string; caption: string; loading?: boolean; onOpen: () => void }) {
  return (
    <button className="tile small" onClick={onOpen}>
      <div className="thead"><span style={{ color }}>{title}</span><span className="muted">›</span></div>
      <div className={`stat ${loading ? "muted" : ""}`}>{stat}</div>
      {loading ? <span className="skel" style={{ width: "60%" }} /> : <div className="muted small">{caption}</div>}
    </button>
  );
}

function ListSheet({ title, items, row, onBack }:
  { title: string; items?: Item[]; row: (i: Item) => JSX.Element; onBack: () => void }) {
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Oriel</button><span>{title}</span></div>
      {items === undefined && <Skeleton widths={[80, 62, 71]} style={{ margin: "8px 0" }} />}
      {items?.length === 0 && <p className="muted">nothing</p>}
      {items?.map((i) => <div key={i.id}>{row(i)}</div>)}
    </section>
  );
}
