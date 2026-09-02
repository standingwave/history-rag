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
import { derive, fmt as fmtLeft, durLabel } from "../convex/timerMath";

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
  return t.text.split("\n", 1)[0].replace(/^(Task|Routine): /, "");
}

/* ── routine schedules: the template's day tags, picked with a segmented
   bar; "pick days" opens seven letter toggles. [] = every day. ── */
const DAY_TAGS7 = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"];
type Sched = { kind: "daily" | "weekday" | "weekend" | "pick"; days: string[] };
const schedTags = (s: Sched): string[] =>
  s.kind === "daily" ? [] : s.kind === "pick" ? s.days : [s.kind];
function toSched(days: string[]): Sched {
  if (!days.length) return { kind: "daily", days: [] };
  if (days.length === 1 && days[0] === "weekday") return { kind: "weekday", days: [] };
  if (days.length === 1 && days[0] === "weekend") return { kind: "weekend", days: [] };
  return { kind: "pick", days: days.filter((d) => DAY_TAGS7.includes(d)) };
}
function schedLabel(days: string[]): string {
  if (!days.length) return "daily";
  if (days.length === 1 && days[0] === "weekday") return "weekdays";
  if (days.length === 1 && days[0] === "weekend") return "weekend";
  return days.map((d) => d[0].toUpperCase() + d.slice(1, 3)).join(" ");
}
function SchedulePicker({ sched, setSched }: { sched: Sched; setSched: (s: Sched) => void }) {
  return (
    <>
      <div className="cbar sbar">
        {([["daily", "daily"], ["weekday", "weekdays"], ["weekend", "weekend"], ["pick", "pick days"]] as const)
          .map(([k, lbl]) => <button key={k} className={sched.kind === k ? "sel" : ""}
            onClick={() => setSched({ kind: k, days: k === "pick" && !sched.days.length ? ["mon", "wed", "fri"] : sched.days })}>
            {lbl}</button>)}
      </div>
      {sched.kind === "pick" && (
        <div className="dayrow">
          {DAY_TAGS7.map((d, i) => <button key={d} className={`dtl ${sched.days.includes(d) ? "on" : ""}`}
            onClick={() => {
              const has = sched.days.includes(d);
              if (has && sched.days.length === 1) return;
              setSched({ ...sched, days: DAY_TAGS7.filter((x) => (x === d) !== sched.days.includes(x)) });
            }}>{DAY_LETTERS[i]}</button>)}
        </div>
      )}
    </>
  );
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
const ORDER = ["agenda", "tasks", "digest"];

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
  const timers = useQuery(api.timers.list, {});
  const intents = useQuery(api.today.intents, {});
  const waiting = (intents ?? []).filter((i: any) => !i.appliedAt && !i.error).length;
  const starting = (intents ?? []).some((i: any) => i.kind === "start" && !i.appliedAt && !i.error);
  const startDay = useMutation(api.today.startDay);
  const hour = new Date().getHours();
  const openId = (id: string) => open(`x:${encodeURIComponent(id)}`);

  if (sheet === "tasks") return <TasksSheet day={day} tasks={tasks} latest={latest} onBack={close}
    onRoutines={() => open("routines")} onLists={() => open("lists")} />;
  if (sheet === "routines") return <RoutinesSheet onBack={() => history.back()} />;
  if (sheet === "lists") return <ListsSheet onBack={() => history.back()}
    onOpen={(p) => open(`list:${encodeURIComponent(p)}`)} />;
  if (sheet.startsWith("list:"))
    return <VaultListSheet path={decodeURIComponent(sheet.slice(5))} onBack={() => history.back()} />;
  if (sheet === "timers") return <TimersSheet timers={timers} onBack={close} />;
  if (sheet === "agenda") return <AgendaSheet day={day} agenda={agenda} onBack={close} onOpen={openId} />;
  if (sheet === "digest") return <DigestSheet brief={brief} agenda={agenda} tasks={tasks} latest={latest}
    onBack={close} onOpen={openId} />;
  if (sheet.startsWith("x:")) return <Detail id={decodeURIComponent(sheet.slice(2))} onBack={() => history.back()} />;
  // search/ask/browse live as tabs of History; old hashes land on their tab.
  if (["history", "search", "ask", "browse"].includes(sheet))
    return <HistorySheet initial={sheet === "history" ? "search" : sheet} onBack={close} onOpen={openId} />;
  if (sheet === "capture") return <CaptureSheet day={day} intents={intents} onBack={close} />;
  if (sheet === "settings") return <SettingsSheet onBack={close} />;

  const tiles: Record<string, JSX.Element> = {
    tasks: <TasksTile key="tasks" tasks={tasks} waiting={waiting} starting={starting}
      onStart={() => startDay({ day })} onOpen={() => open("tasks")} />,
    agenda: <AgendaTile key="agenda" agenda={agenda} upcoming={upcoming} onOpen={() => open("agenda")} />,
    digest: <DigestTile key="digest" brief={brief} agenda={agenda} tasks={tasks} latest={latest}
      onOpen={() => open("digest")} />,
  };
  return (
    <section>
      <div className="daterow">
        <span>{dayLabel(day)} · {period(hour)}</span>
        <span>
          <button className="lnk" onClick={() => open("history")}>history</button>{" · "}
          <button className="lnk" onClick={() => open("timers")}>timers</button>
        </span>
      </div>
      <TimersBar timers={timers} onOpen={() => open("timers")} />
      <div className="grid">{ORDER.map((id) => tiles[id])}</div>
      <CaptureFab day={day} onSheet={() => open("capture")} />
    </section>
  );
}

function TasksTile({ tasks, waiting, starting, onStart, onOpen }:
  { tasks?: Item[]; waiting: number; starting: boolean; onStart: () => Promise<unknown>; onOpen: () => void }) {
  const [startErr, setStartErr] = useState("");
  const list = tasks ?? [];
  const main = list.filter((t) => t.meta.section !== "Routine");
  const done = main.filter((t) => t.meta.done);
  const open = main.filter((t) => !t.meta.done);
  const shown = [...open, ...done].slice(0, 5);
  const more = open.length - Math.min(open.length, 5);
  return (
    <button className="tile large" onClick={onOpen}>
      <div className="thead"><span style={{ color: "#facc15" }}>☑ Tasks</span>
        <span className="muted">{tasks ? `${done.length}/${main.length} done` : "…"} ›</span></div>
      {!tasks && <Skeleton widths={[70, 55, 62]} style={{ margin: "6px 0" }} />}
      {shown.map((t) => <TaskRow key={t.id} t={t} compact />)}
      {tasks && !list.length && (starting
        ? <p className="muted"><span className="waiting">starting the day — waiting for your Mac</span></p>
        : <p className="muted">no note for today · <span className="lnk" role="button"
            style={{ color: "#8ab4f8" }}
            onClick={(e) => { e.stopPropagation(); setStartErr(""); onStart().catch((err) => setStartErr(String(err?.message ?? err))); }}>
            start day</span></p>)}
      {startErr && <p className="muted small" style={{ color: "#f87171" }}>{startErr}</p>}
      {tasks && list.length > 0 && !main.length && <p className="muted">no tasks yet — just routines</p>}
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
  onMakeRoutine: (days: string[]) => void;
  onFocus: () => void;
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
type Mode = "acts" | "edit" | "delete" | "routine" | null;
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
  const [sched, setSched] = useState<Sched>({ kind: "daily", days: [] });
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
          <button onClick={() => { acts?.onFocus(); setMode?.(null); }}>focus</button>
          <button onClick={() => { setSched({ kind: "daily", days: [] }); setMode?.("routine"); }}>routine…</button>
          <button className="danger" onClick={() => setMode?.("delete")}>delete</button>
        </div>
      )}
      {mode === "routine" && (
        <div className="schedwrap">
          <SchedulePicker sched={sched} setSched={setSched} />
          <div className="savebar">
            <button className="go" onClick={() => { acts?.onMakeRoutine(schedTags(sched)); setMode?.(null); }}>make routine</button>
            <button className="cancelv" onClick={() => setMode?.(null)}>cancel</button>
          </div>
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

const KIND_VERB: Record<string, string> = {
  toggle: "apply", add: "add", edit: "edit", delete: "delete", attach: "attach",
  routineAdd: "add routine", routineEdit: "edit routine", routineDelete: "delete routine",
};

function TasksSheet({ day, tasks, latest, onBack, onRoutines, onLists }:
  { day: string; tasks?: Item[]; latest?: string | null; onBack: () => void;
    onRoutines: () => void; onLists: () => void }) {
  const toggle = useMutation(api.today.toggle);
  const startDay = useMutation(api.today.startDay);
  const routineAdd = useMutation(api.today.routineAdd);
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
  const timerStart = useMutation(api.timers.start);
  const intents = useQuery(api.today.intents, {});
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");
  const [routineOn, setRoutineOn] = useState(false);
  const [sched, setSched] = useState<Sched>({ kind: "daily", days: [] });
  const [active, setActive] = useState<{ id: string; mode: NonNullable<Mode> } | null>(null);
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
    if (routineOn) { routineAdd({ day, text, days: schedTags(sched) }).catch(fail); setRoutineOn(false); }
    else add({ day, text }).catch(fail);
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
    onMakeRoutine: (days) => void routineAdd({ day, text: title(t), days }).catch(fail),
    onFocus: () => {
      unlockAudio();
      void timerStart({ label: title(t), durationMs: 0, up: true, taskChunkId: t.id }).catch(fail);
    },
  });
  // chunk id → what's in flight for it, so placeholders read "adding…" and
  // can't be toggled before they exist in the note.
  const inflight: Record<string, Busy> = {};        // placeholders
  const rowBusy: Record<string, Busy> = {};         // toggles, deletes, attachments on a row
  const subBusy: Record<string, Record<string, Busy>> = {};   // chunkId → sub text → busy
  const LABEL: Record<string, string> = { toggle: "ticking", add: "adding", edit: "editing", delete: "deleting", attach: "attaching", start: "starting" };
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
    e.kind === "start" ? "start the day"
    : e.kind === "attach" ? `attach "${e.text}" to "${e.parent}"`
    : e.parent ? `${KIND_VERB[e.kind] ?? e.kind} subtask "${e.text}"` : `${KIND_VERB[e.kind] ?? e.kind} "${e.text}"`;
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button>
        <span>☑ Tasks · {doneT.length}/{main.length} done · {dayLabel(day)}</span></div>
      {stalled && <div className="wait"><span>{unapplied.length} change{unapplied.length > 1 ? "s" : ""} waiting for the Mac</span>
        <span className="muted">since {new Date(oldest).toTimeString().slice(0, 5)}</span></div>}
      {errView}
      {errors.map((e: { id: string; kind: string; text: string; parent: string | null; error: string | null }) =>
        <ErrBox key={e.id} onClose={() => setDismissed((d) => new Set(d).add(e.id))}>couldn't {errWhat(e)}: {e.error}</ErrBox>)}
      {!tasks && <Skeleton widths={[70, 55, 62, 48]} style={{ margin: "8px 0" }} />}
      {tasks && !list.length && (
        <>
          <p className="muted">no note for {day}{latest ? ` — latest is ${latest}` : ""}</p>
          {unapplied.some((i: any) => i.kind === "start")
            ? <p className="muted"><span className="waiting">starting the day — waiting for your Mac</span></p>
            : <p><button className="go" onClick={() => void startDay({ day }).catch(fail)}>start day</button>
                <span className="muted small" style={{ marginLeft: 10 }}>carries open tasks, adds routines</span></p>}
        </>
      )}
      {openT.map((t) => row(t))}
      {doneT.map((t) => row(t))}
      {composing && (
        <>
          <div className="trow composer">
            <span className="glyph">{routineOn ? "↻" : "○"}</span>
            <input ref={inputRef} autoFocus placeholder={routineOn ? "new routine" : "new task"}
              value={draft} onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") setComposing(false); }} />
            <button className={`cancelv ${routineOn ? "ron" : ""}`} onClick={() => setRoutineOn(!routineOn)}>routine</button>
            <button className="go" onClick={submit}>add</button>
            <button className="cancelv" onClick={() => setComposing(false)}>cancel</button>
          </div>
          {routineOn && <div className="schedwrap"><SchedulePicker sched={sched} setSched={setSched} /></div>}
        </>
      )}
      {routine.length > 0 && <p className="sect">ROUTINE</p>}
      {routine.map((t) => row(t, true))}
      <p><button className="lnk" onClick={onRoutines}>routines ›</button>{" · "}
        <button className="lnk" onClick={onLists}>lists ›</button></p>
      {vault && <p><a href={`obsidian://open?vault=${encodeURIComponent(vault)}&file=${day}`}>open today's note in Obsidian ↗</a></p>}
      {!inputOpen && <button className="fab" aria-label="add a task" onClick={() => { setActive(null); setComposing(true); }}>+</button>}
    </section>
  );
}

/* The routine template, one row per entry with its schedule at the right;
   edit / days / delete per row, the + composer at the bottom. Writes queue
   for the Mac exactly like task writes. */
function RoutinesSheet({ onBack }: { onBack: () => void }) {
  const routines = useQuery(api.today.routines, {});
  const intents = useQuery(api.today.intents, {});
  const routineAdd = useMutation(api.today.routineAdd);
  const routineEdit = useMutation(api.today.routineEdit);
  const routineRemove = useMutation(api.today.routineRemove);
  const { push: fail, view: errView } = useErrors();
  const [active, setActive] = useState<{ id: string; mode: "acts" | "edit" | "days" | "delete" } | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [rowSched, setRowSched] = useState<Sched>({ kind: "daily", days: [] });
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");
  const [sched, setSched] = useState<Sched>({ kind: "daily", days: [] });
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  useTick(5000, (intents ?? []).some((i: any) => !i.appliedAt && !i.error));
  const mine = (i: any) => String(i.kind).startsWith("routine");
  const unapplied = (intents ?? []).filter((i: any) => mine(i) && !i.appliedAt && !i.error);
  const errors = (intents ?? []).filter((i: any) =>
    mine(i) && i.error && Date.now() - i.requestedAt < 3600e3 && !dismissed.has(i.id));
  const oldest = unapplied.reduce((m: number, i: any) => Math.min(m, i.requestedAt), Infinity);
  const stalled = unapplied.length > 0 && Date.now() - oldest > YOUNG_MS;
  const busyFor: Record<string, Busy> = {};
  for (const i of unapplied) {
    const b: Busy = { label: { routineAdd: "adding", routineEdit: "editing", routineDelete: "deleting" }[i.kind as string] ?? i.kind,
                      young: Date.now() - i.requestedAt < YOUNG_MS };
    busyFor[i.chunkId] = b;
    if (i.newId) busyFor[i.newId] = b;
  }
  const list = (routines ?? []) as Item[];
  const submit = () => {
    const text = draft.trim();
    if (!text) { setComposing(false); return; }
    setDraft("");
    routineAdd({ day: localDay(), text, days: schedTags(sched) }).catch(fail);
  };
  const saveEdit = (t: Item) => {
    const v = editDraft.trim();
    if (v && v !== title(t)) routineEdit({ id: t.id, newText: v }).catch(fail);
    setActive(null);
  };
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button>
        <span>↻ Routines · {list.length}</span></div>
      {stalled && <div className="wait"><span>{unapplied.length} change{unapplied.length > 1 ? "s" : ""} waiting for the Mac</span>
        <span className="muted">since {new Date(oldest).toTimeString().slice(0, 5)}</span></div>}
      {errView}
      {errors.map((e: any) =>
        <ErrBox key={e.id} onClose={() => setDismissed((d) => new Set(d).add(e.id))}>
          couldn't {KIND_VERB[e.kind] ?? e.kind} "{e.text}": {e.error}</ErrBox>)}
      {!routines && <Skeleton widths={[70, 55, 62]} style={{ margin: "8px 0" }} />}
      {routines && !list.length && <p className="muted">no routines yet — a routine is added to every day it's scheduled for</p>}
      {list.map((t) => {
        const busy = t.pending ? busyFor[t.id] : undefined;
        const open = active?.id === t.id;
        const mode = open ? active!.mode : null;
        const days: string[] = t.meta.days ?? [];
        if (mode === "edit") return (
          <div key={t.id} className="trow"><div className="tline edit">
            <span className="glyph">↻</span>
            <input autoFocus value={editDraft} onChange={(e) => setEditDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") saveEdit(t); if (e.key === "Escape") setActive(null); }} />
            <button className="go" onClick={() => saveEdit(t)}>save</button>
            <button className="lnk" onClick={() => setActive(null)}>✕</button>
          </div></div>
        );
        return (
          <div key={t.id} className={`trow ${t.pending ? "pending" : ""}`}>
            <div className="tline">
              <span className={`glyph ${busy?.young ? "pulse" : ""}`}>↻</span>
              <span className="ttext">{title(t)}</span>
              {busy ? busyLabel(busy) : <span className="sched">{schedLabel(days)}</span>}
              {!t.pending && <button className={`more ${mode ? "on" : ""}`} aria-label="actions"
                onClick={() => setActive(open ? null : { id: t.id, mode: "acts" })}>…</button>}
            </div>
            {mode === "acts" && (
              <div className="cbar">
                <button onClick={() => { setEditDraft(title(t)); setActive({ id: t.id, mode: "edit" }); }}>edit</button>
                <button onClick={() => { setRowSched(toSched(days)); setActive({ id: t.id, mode: "days" }); }}>days</button>
                <button className="danger" onClick={() => setActive({ id: t.id, mode: "delete" })}>delete</button>
                <button onClick={() => setActive(null)}>✕</button>
              </div>
            )}
            {mode === "days" && (
              <div className="schedwrap">
                <SchedulePicker sched={rowSched} setSched={setRowSched} />
                <div className="savebar">
                  <button className="go" onClick={() => { routineEdit({ id: t.id, days: schedTags(rowSched) }).catch(fail); setActive(null); }}>save</button>
                  <button className="cancelv" onClick={() => setActive(null)}>cancel</button>
                </div>
              </div>
            )}
            {mode === "delete" && (
              <div className="confirm">delete routine<span className="muted small">&nbsp;(today's copy stays)</span>:
                <button className="yes" onClick={() => { routineRemove({ id: t.id }).catch(fail); setActive(null); }}>Yes</button>
                <button className="no" onClick={() => setActive(null)}>No</button>
              </div>
            )}
          </div>
        );
      })}
      {composing && (
        <>
          <div className="trow composer">
            <span className="glyph">↻</span>
            <input autoFocus placeholder="new routine" value={draft} onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") setComposing(false); }} />
            <button className="go" onClick={submit}>add</button>
            <button className="cancelv" onClick={() => setComposing(false)}>cancel</button>
          </div>
          <div className="schedwrap"><SchedulePicker sched={sched} setSched={setSched} /></div>
        </>
      )}
      {!composing && <button className="fab" aria-label="add a routine" onClick={() => { setActive(null); setComposing(true); }}>+</button>}
    </section>
  );
}

/* ── timers (wip/SPEC-timers.md): pure Convex state, no Mac. A running
   timer is an absolute endsAt the client counts down from; repeat cycles
   and "done" are derived, so a ringing timer looks the same on every
   device and dismiss anywhere clears everywhere. ── */
type Timer = {
  id: string; label: string; durationMs: number; repeat?: boolean; up?: boolean;
  endsAt?: number; remainingMs?: number; startedAt: number; taskChunkId?: string;
};
const timerGlyph = (t: Timer) => (t.up ? "◷" : t.repeat ? "↻" : "⏱");
const timerName = (t: Timer) => t.label || (t.up ? "stopwatch" : durLabel(t.durationMs));

/* iOS only lets pages play sound after a tap, so start/resume/pause
   unlock the context; without it the ring is visual only. */
let audioCtx: AudioContext | null = null;
function unlockAudio() {
  try {
    audioCtx ??= new AudioContext();
    if (audioCtx.state === "suspended") void audioCtx.resume();
  } catch { /* no audio support; the visual ring still works */ }
}
function chime() {
  navigator.vibrate?.(200);
  const ac = audioCtx;
  if (!ac || ac.state !== "running") return;
  for (const d of [0, 0.22]) {
    const o = ac.createOscillator(), g = ac.createGain();
    o.frequency.value = 880; o.connect(g); g.connect(ac.destination);
    g.gain.setValueAtTime(0.12, ac.currentTime + d);
    g.gain.exponentialRampToValueAtTime(0.001, ac.currentTime + d + 0.18);
    o.start(ac.currentTime + d); o.stop(ac.currentTime + d + 0.2);
  }
}

/* Chime once per crossing (one-shot end, repeat cycle boundary), on
   whichever timer surface is mounted. lastSeen outlives the components,
   so a ring that happened while another sheet was open sounds as soon as
   the bar or the timers sheet is back. */
const lastSeen = new Map<string, { st: string; cycle: number }>();
function ringCheck(timers: Timer[]) {
  const now = Date.now();
  const alive = new Set(timers.map((t) => t.id));
  for (const id of [...lastSeen.keys()]) if (!alive.has(id)) lastSeen.delete(id);
  for (const t of timers) {
    const s = derive(t, now), prev = lastSeen.get(t.id);
    if (prev && ((s.st === "done" && prev.st === "running") || s.cycle > prev.cycle)) chime();
    lastSeen.set(t.id, { st: s.st, cycle: s.cycle });
  }
}

/* Flash the tab/app title while a one-shot is ringing. */
function useRingTitle(ringing: boolean) {
  useEffect(() => {
    if (!ringing) return;
    const base = document.title;
    let on = false;
    const t = setInterval(() => { on = !on; document.title = on ? "⏰ done — Oriel" : base; }, 1000);
    return () => { clearInterval(t); document.title = base; };
  }, [ringing]);
}

/* The dashboard's slim ticking bar; exists only while timers do. */
function TimersBar({ timers, onOpen }: { timers?: Timer[]; onOpen: () => void }) {
  const dismiss = useMutation(api.timers.dismiss);
  const list = timers ?? [];
  useTick(500, list.length > 0);
  useEffect(() => ringCheck(list));
  const now = Date.now();
  useRingTitle(list.some((t) => derive(t, now).st === "done"));
  if (!list.length) return null;
  return (
    <div className="tbar" role="button" tabIndex={0} onClick={onOpen}>
      {list.map((t) => {
        const s = derive(t, now);
        return (
          <div key={t.id} className={`brow ${s.st === "paused" ? "tpaused" : ""} ${s.st === "done" ? "tdone" : ""}`}>
            <span className="bglyph">{timerGlyph(t)}</span>
            <span className="blabel">{timerName(t)}</span>
            {s.st === "paused" && <span className="pstate">paused</span>}
            {s.st === "done"
              ? <>
                  <span className="btime">done</span>
                  <button className="bx" aria-label="dismiss"
                    onClick={(e) => { e.stopPropagation(); void dismiss({ id: t.id as Id<"timers"> }); }}>✕</button>
                </>
              : <span className="btime">{fmtLeft(s.left, t.up)}</span>}
          </div>
        );
      })}
    </div>
  );
}

const TIMER_PRESETS: [number, string][] = [[3, "3m"], [5, "5m"], [10, "10m"], [15, "15m"], [25, "25m"], [45, "45m"], [60, "1h"]];

/* Lock-screen alerts: one Web Push subscription per install, stored in
   Convex; timers:start schedules the actual send. On iOS this needs the
   app added to the home screen (16.4+). */
function urlB64(s: string) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}
function useAlerts() {
  const vapid = useQuery(api.push.vapidPublicKey, {});
  const save = useMutation(api.push.subscribe);
  const [state, setState] = useState<"checking" | "unsupported" | "off" | "on" | "denied">("checking");
  useEffect(() => {
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      setState("unsupported"); return;
    }
    if (Notification.permission === "denied") { setState("denied"); return; }
    navigator.serviceWorker.getRegistration()
      .then((r) => r?.pushManager.getSubscription() ?? null)
      .then((s) => setState(s ? "on" : "off"))
      .catch(() => setState("off"));
  }, []);
  const enable = async () => {
    if (!vapid) throw new Error("push isn't configured on the server");
    if ((await Notification.requestPermission()) !== "granted") {
      setState("denied");
      throw new Error("notifications are blocked for Oriel");
    }
    const reg = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64(vapid) });
    const j = sub.toJSON();
    await save({ endpoint: sub.endpoint, keys: { p256dh: j.keys!.p256dh, auth: j.keys!.auth },
                 ua: navigator.userAgent.slice(0, 80) });
    setState("on");
  };
  return { state, configured: !!vapid, enable };
}

function TimersSheet({ timers, onBack }: { timers?: Timer[]; onBack: () => void }) {
  const start = useMutation(api.timers.start);
  const pause = useMutation(api.timers.pause);
  const resume = useMutation(api.timers.resume);
  const dismiss = useMutation(api.timers.dismiss);
  const { push: fail, view: errView } = useErrors();
  const [mins, setMins] = useState("");
  const [label, setLabel] = useState("");
  const [rep, setRep] = useState(false);
  const list = timers ?? [];
  useTick(500, list.length > 0);
  useEffect(() => ringCheck(list));
  const now = Date.now();
  useRingTitle(list.some((t) => derive(t, now).st === "done"));
  const go = (label: string, durationMs: number, repeat = false, up = false) => {
    unlockAudio();
    void start({ label, durationMs, repeat: repeat || undefined, up: up || undefined }).catch(fail);
  };
  const custom = () => {
    const m = parseFloat(mins);
    if (!m || m <= 0) return;
    go(label.trim(), Math.round(m * 60_000), rep);
    setMins(""); setLabel(""); setRep(false);
  };
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button>
        <span>⏱ Timers · {list.length}</span></div>
      {errView}
      {!timers && <Skeleton widths={[70, 55]} style={{ margin: "8px 0" }} />}
      {timers && !list.length && <p className="muted">no timers running</p>}
      {list.map((t) => {
        const s = derive(t, now);
        const meta = t.up ? (t.taskChunkId ? "focus — logs to the task on ✕" : "stopwatch")
          : t.repeat ? `↻ every ${durLabel(t.durationMs)}` : `${durLabel(t.durationMs)} timer`;
        const id = t.id as Id<"timers">;
        return (
          <div key={t.id} className={`timerrow ${s.st === "paused" ? "tpaused" : ""} ${s.st === "done" ? "tdone" : ""}`}>
            <span className="tbig">{fmtLeft(s.left, t.up)}</span>
            <div className="tmid">
              <div className="tlab">{timerName(t)}</div>
              <div className="tmeta">{s.st === "done" ? "done" : meta}{s.st === "paused" ? " · paused" : ""}</div>
            </div>
            {s.st === "done"
              ? <button className="lnk" onClick={() => void dismiss({ id }).catch(fail)}>dismiss</button>
              : <>
                  <button className="lnk" onClick={() => {
                    unlockAudio();
                    void (s.st === "paused" ? resume({ id }) : pause({ id })).catch(fail);
                  }}>{s.st === "paused" ? "resume" : "pause"}</button>
                  <button className="lnk tstop" aria-label="stop" onClick={() => void dismiss({ id }).catch(fail)}>✕</button>
                </>}
          </div>
        );
      })}
      <p className="sect">NEW TIMER</p>
      <div className="chips tchips">
        {TIMER_PRESETS.map(([m, l]) =>
          <button key={l} className="chip" onClick={() => go("", m * 60_000)}>{l}</button>)}
        <button className="chip" onClick={() => go("", 0, false, true)}>◷ stopwatch</button>
      </div>
      <div className="trow composer">
        <input className="tmin" inputMode="decimal" placeholder="min" value={mins}
          onChange={(e) => setMins(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") custom(); }} />
        <input placeholder="label (optional)" value={label} onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") custom(); }} />
        <button className={`cancelv ${rep ? "ron" : ""}`} onClick={() => setRep(!rep)}>repeat</button>
        <button className="go" onClick={custom}>start</button>
      </div>
    </section>
  );
}

/* ── vault lists (wip/SPEC-vault-lists.md): notes in Lists/, three item
   states. The list you shop from shows only what you need; the catalog
   waits below, filtered by the add box, most-recently-shelved first. ── */
type ListWords = { need: string; got: string; done: string };
const LIST_CAT_CAP = 12;

function ListsSheet({ onBack, onOpen }: { onBack: () => void; onOpen: (path: string) => void }) {
  const data = useQuery(api.lists.lists, {});
  const create = useMutation(api.lists.create);
  const vocab = useAction(api.lists.vocab);
  const { push: fail, view: errView } = useErrors();
  const [composing, setComposing] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [words, setWords] = useState<ListWords | null>(null);
  const [thinking, setThinking] = useState(false);
  const genId = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Debounced wording generation as the name is typed; stale replies drop.
  const gen = (name: string) => {
    if (timer.current) clearTimeout(timer.current);
    genId.current++;
    if (!name.trim()) { setWords(null); setThinking(false); return; }
    timer.current = setTimeout(() => {
      const id = ++genId.current;
      setThinking(true);
      vocab({ name: name.trim() })
        .then((w) => { if (id === genId.current) { setThinking(false); setWords(w); } })
        .catch(() => { if (id === genId.current) { setThinking(false); setWords(null); } });
    }, 500);
  };
  const submit = () => {
    const n = nameDraft.trim();
    if (!n) { setComposing(false); return; }
    create({ name: n, words: words ?? undefined }).catch(fail);
    setNameDraft(""); setWords(null); setThinking(false); setComposing(false);
  };
  const list = data ?? [];
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button>
        <span>☰ Lists · {list.length}</span></div>
      {errView}
      {!data && <Skeleton widths={[70, 55, 62]} style={{ margin: "8px 0" }} />}
      {data && !list.length && <p className="muted">no lists yet — a list is a note in the vault's Lists folder</p>}
      {list.map((l) => (
        <button key={l.path} className="lirow" onClick={() => onOpen(l.path)}>
          <span className="linm">{l.name}<span className="lisub">{l.path}</span></span>
          <span className={`licnt ${l.pending ? "pulse" : ""}`}>
            {l.pending ? "creating…"
              : l.need ? `${l.need} ${l.words.need}`
              : l.got ? `✓ all ${l.words.got}`
              : l.cat ? `${l.cat} shelved` : "empty"}</span>
          <span className="lichev">›</span>
        </button>
      ))}
      {composing && (
        <div style={{ marginTop: 14 }}>
          <div className="trow composer">
            <span className="glyph">☰</span>
            <input autoFocus placeholder="list name…" value={nameDraft}
              onChange={(e) => { setNameDraft(e.target.value); gen(e.target.value); }}
              onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") setComposing(false); }} />
            <button className="go" onClick={submit}>create</button>
            <button className="cancelv" onClick={() => setComposing(false)}>cancel</button>
          </div>
          <p className="muted small" style={{ margin: "8px 0 0 26px" }}>
            {thinking ? <span className="pulse" style={{ color: "#facc15" }}>naming the verbs…</span>
              : words ? <>wording: <b style={{ color: "#e8e8eb" }}>{words.need}</b> · <b style={{ color: "#e8e8eb" }}>{words.got}</b> · <b style={{ color: "#e8e8eb" }}>{words.done}</b>
                  {" "}<button className="lnk" onClick={() => gen(nameDraft)}>↻</button>{" "}
                  <button className="lnk" style={{ color: "#a6a6af" }} onClick={() => setWords(null)}>plain</button></>
              : "wording: plain (to do · done · reset) — generated from the name as you type"}
          </p>
        </div>
      )}
      {!composing && <button className="fab" aria-label="new list" onClick={() => setComposing(true)}>+</button>}
    </section>
  );
}

function VaultListSheet({ path, onBack }: { path: string; onBack: () => void }) {
  const data = useQuery(api.lists.items, { path });
  const setSt = useMutation(api.lists.setState);
  const addIt = useMutation(api.lists.addItem);
  const editIt = useMutation(api.lists.editItem);
  const removeIt = useMutation(api.lists.removeItem);
  const resetL = useMutation(api.lists.reset);
  const { push: fail, view: errView } = useErrors();
  const [draft, setDraft] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [active, setActive] = useState<{ id: string; mode: "acts" | "edit" | "delete" } | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [showAll, setShowAll] = useState(false);
  const items = data?.items ?? [];
  const words = data?.words ?? { need: "to do", got: "done", done: "reset" };
  const need = items.filter((i) => i.state === "need");
  const got = items.filter((i) => i.state === "got");
  const q = draft.trim().toLowerCase();
  const catAll = items.filter((i) => i.state === "cat");
  let cat = q ? catAll.filter((i) => i.text.toLowerCase().includes(q)) : catAll;
  const capped = !q && !showAll && cat.length > LIST_CAT_CAP;
  if (capped) cat = cat.slice(0, LIST_CAT_CAP);
  const submit = () => {
    const t = draft.trim();
    if (!t) return;
    addIt({ path, text: t }).catch(fail);
    setDraft("");
  };
  const row = (i: { id: string; text: string; state: string; pending: boolean }) => {
    const open = active?.id === i.id;
    if (open && active!.mode === "edit") return (
      <div key={i.id} className="trow"><div className="tline">
        <span className="glyph">{i.state === "got" ? "✓" : "○"}</span>
        <input autoFocus value={editDraft} onChange={(e) => setEditDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") { const t = editDraft.trim();
              if (t && t !== i.text) editIt({ id: i.id, newText: t }).catch(fail); setActive(null); }
            if (e.key === "Escape") setActive(null);
          }} />
        <button className="go" onClick={() => { const t = editDraft.trim();
          if (t && t !== i.text) editIt({ id: i.id, newText: t }).catch(fail); setActive(null); }}>save</button>
        <button className="cancelv" onClick={() => setActive(null)}>✕</button>
      </div></div>
    );
    const tap = () => {
      if (i.pending) return;
      const want = i.state === "cat" ? "need" : i.state === "need" ? "got" : "need";
      void setSt({ id: i.id, want }).catch(fail);
    };
    return (
      <div key={i.id} className={`trow ${i.state === "got" ? "done" : ""} ${i.state === "cat" ? "catrow" : ""} ${i.pending ? "pending" : ""}`}>
        <div className="tline">
          <button className={`glyph ${i.pending ? "pulse" : ""}`} onClick={tap}>
            {i.state === "got" ? "✓" : i.state === "cat" ? "+" : "○"}</button>
          <span className="ttext" onClick={i.state === "cat" ? tap : undefined}
            style={i.state === "cat" ? { cursor: "pointer" } : undefined}>{i.text}</span>
          {i.pending && <span className="small pulse" style={{ color: "#facc15" }}>syncing…</span>}
          {!i.pending && <button className={`more ${open ? "on" : ""}`} aria-label="actions"
            onClick={() => setActive(open ? null : { id: i.id, mode: "acts" })}>…</button>}
        </div>
        {open && active!.mode === "acts" && (
          <div className="cbar">
            <button onClick={() => { setEditDraft(i.text); setActive({ id: i.id, mode: "edit" }); }}>edit</button>
            {i.state !== "cat" && <button onClick={() => { void setSt({ id: i.id, want: "cat" }).catch(fail); setActive(null); }}>shelve</button>}
            <button className="danger" onClick={() => setActive({ id: i.id, mode: "delete" })}>delete</button>
            <button onClick={() => setActive(null)}>✕</button>
          </div>
        )}
        {open && active!.mode === "delete" && (
          <div className="confirm">delete "{i.text}" from the list:
            <button className="yes" onClick={() => { void removeIt({ id: i.id }).catch(fail); setActive(null); }}>Yes</button>
            <button className="no" onClick={() => setActive(null)}>No</button>
          </div>
        )}
      </div>
    );
  };
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ lists</button>
        <span>{data?.name ?? "…"}</span></div>
      {errView}
      {!data && <Skeleton widths={[70, 55, 62]} style={{ margin: "8px 0" }} />}
      {data && (
        <>
          <div className="lhdr">
            <span className="lcount">{need.length ? `${need.length} ${words.need}`
              : got.length ? `✓ all ${words.got}` : "nothing needed"}</span>
            {got.length > 0 && !confirming &&
              <button className="lnk" onClick={() => setConfirming(true)}>{words.done}</button>}
          </div>
          {confirming && (
            <div className="confirm">{words.done} — shelve everything {words.got}?
              <button className="yes" onClick={() => { void resetL({ path }).catch(fail); setConfirming(false); }}>Yes</button>
              <button className="no" onClick={() => setConfirming(false)}>No</button>
            </div>
          )}
          <p className="sect">{words.need.toUpperCase()}</p>
          {need.length ? need.map(row)
            : <p className="muted small">nothing {words.need} — tap the catalog below</p>}
          {got.length > 0 && <p className="sect">{words.got.toUpperCase()}</p>}
          {got.map(row)}
          <div className="trow composer" style={{ marginTop: 10 }}>
            <span className="glyph">+</span>
            <input placeholder={`${words.need}… (filters the catalog)`} value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") submit(); }} />
            <button className="go" onClick={submit}>add</button>
          </div>
          <p className="sect">{q ? `CATALOG · matching "${q}"` : "CATALOG · tap to need · recent first"}</p>
          {cat.map(row)}
          {capped && <p><button className="lnk" onClick={() => setShowAll(true)}>show all {catAll.length} ›</button></p>}
          {q && !cat.length && <p className="muted small">no catalog match — add creates "{draft.trim()}"</p>}
          {!q && !catAll.length && <p className="muted small">the catalog fills as you {words.done.split(" ")[0]} — {words.got} items shelve here</p>}
        </>
      )}
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

/* The day ahead in one line: events, the next one, open tasks. Shared by
   the Digest tile and sheet; the 06:00 push composes its own server-side
   (digest.facts) because it must run with no client open. */
function dayAhead(agenda?: Item[], tasks?: Item[], latest?: string | null) {
  if (!agenda || !tasks) return "";
  const now = Date.now();
  const next = agenda.find((e) => timeOnly(e.timestamp) !== "all day" && new Date(e.timestamp).getTime() > now);
  const parts = [agenda.length ? `${agenda.length} event${agenda.length > 1 ? "s" : ""}` : "no events"];
  if (next) parts.push(`next ${hhmm(next.timestamp)} ${describe(next).title}`);
  const main = tasks.filter((t) => t.meta.section !== "Routine");
  const open = main.filter((t) => !t.meta.done);
  parts.push(main.length ? `${open.length} open task${open.length === 1 ? "" : "s"}`
    : latest ? `tasks to carry from ${dayLabel(latest)}` : "no tasks yet");
  return parts.join(" · ");
}

/* The digest: today at a glance on top, the last-24h brief beneath. */
function DigestTile({ brief, agenda, tasks, latest, onOpen }:
  { brief?: Brief; agenda?: Item[]; tasks?: Item[]; latest?: string | null; onOpen: () => void }) {
  const line = dayAhead(agenda, tasks, latest);
  return (
    <button className="tile large brief" onClick={onOpen}>
      <div className="thead"><span style={{ color: "#a78bfa" }}>Digest</span>
        <span className="muted">{brief?.generatedAt ? ago(brief.generatedAt) : ""} ›</span></div>
      {line && <p className="dayline">{line}</p>}
      {!brief && <Skeleton widths={[92, 84, 60]} style={{ margin: "6px 0" }} />}
      {brief && !brief.answer && <p className="muted">{brief.lastError ? `couldn't answer: ${brief.lastError.error}` : "not generated yet — open to run it"}</p>}
      {brief?.answer && <div className="clamp"><Answer text={brief.answer} compact /></div>}
    </button>
  );
}

function DigestSheet({ brief, agenda, tasks, latest, onBack, onOpen }:
  { brief?: Brief; agenda?: Item[]; tasks?: Item[]; latest?: string | null;
    onBack: () => void; onOpen: (id: string) => void }) {
  const refresh = useAction(api.brief.refresh);
  const [busy, setBusy] = useState(false);
  const [t0, setT0] = useState(0);
  useTick(1000, busy);
  const { push: fail, view: errView } = useErrors();
  const run = () => { setBusy(true); setT0(Date.now()); refresh({}).catch(fail).finally(() => setBusy(false)); };
  const secs = busy ? Math.round((Date.now() - t0) / 1000) : 0;
  // A brief older than the cron interval refreshes itself on open — the
  // 3-hour cron stays as the backstop. Once per mount.
  const autoRan = useRef(false);
  useEffect(() => {
    if (!brief || busy || autoRan.current) return;
    autoRan.current = true;
    if (!brief.generatedAt || Date.now() - brief.generatedAt > 3 * 3600e3) run();
  }, [brief]);   // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button><span>Digest</span></div>
      <p className="dayline">{dayAhead(agenda, tasks, latest)}</p>
      <p className="sect">LAST 24 H</p>
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
/* ── quick capture (wip/SPEC-quick-capture.md): the dashboard's + and
   the capture sheet. A note is an intent the Mac appends under
   "## Notes"; the sheet reads the intent queue itself — your words are
   visible the instant they're queued. ── */
const hhmmNow = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

/* Chip text for a proposed action; `label` is the target's current text,
   attached server-side. */
function chipText(a: any, today: string): string {
  const q = (s: string) => `“${s.length > 60 ? s.slice(0, 57) + "…" : s}”`;
  const dayHint = (d: string) => {
    const diff = Math.round((Date.parse(d + "T12:00:00Z") - Date.parse(today + "T12:00:00Z")) / 864e5);
    const wd = new Date(d + "T12:00:00Z").toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
    return diff === 1 ? `tomorrow (${wd})` : diff === -1 ? "yesterday" : `${wd} ${d.slice(5)}`;
  };
  switch (a.kind) {
    case "task": return `＋ task ${q(a.text)}${a.day !== today ? ` · ${dayHint(a.day)}` : ""}`;
    case "note": return `✎ note ${q(a.text)}`;
    case "toggle": return a.done ? `✓ done ${q(a.label)}` : `○ reopen ${q(a.label)}`;
    case "edit": return `✎ ${q(a.label)} → ${q(a.newText)}`;
    case "delete": return `✕ delete ${q(a.label)}`;
    case "listAdd": return `＋ ${a.items.length > 1 ? `${a.items.length} items` : q(a.items[0])} → ${a.label}`;
    case "listCreate": return `＋ list ${q(a.name)}${a.items?.length
      ? ` with ${a.items.length} item${a.items.length > 1 ? "s" : ""}` : ""}`;
    case "listSet": return a.state === "got" ? `✓ got ${q(a.label)}` : `＋ need ${q(a.label)}`;
    case "listEdit": return `✎ ${q(a.label)} → ${q(a.newText)}`;
    case "listRemove": return `✕ remove ${q(a.label)} from list`;
    case "timerStart": return a.up
      ? `⏱ ${a.taskId ? "focus" : "stopwatch"} ${q(a.label)}`
      : `⏱ ${durLabel(a.ms)} ${q(a.label)}${a.repeat ? " ↻" : ""}`;
    case "timerCtl": return `⏱ ${a.op} ${q(a.label)}`;
    default: return JSON.stringify(a);
  }
}

function CaptureComposer({ day, onDone, onCancel, fail }:
  { day: string; onDone?: (msg?: string) => void; onCancel?: () => void; fail: (e: unknown) => void }) {
  const capture = useMutation(api.today.capture);
  const add = useMutation(api.today.add);
  const toggleTask = useMutation(api.today.toggle);
  const editTask = useMutation(api.today.edit);
  const removeTask = useMutation(api.today.remove);
  const addListItem = useMutation(api.lists.addItem);
  const setListState = useMutation(api.lists.setState);
  const editListItem = useMutation(api.lists.editItem);
  const removeListItem = useMutation(api.lists.removeItem);
  const createList = useMutation(api.lists.create);
  const vocabCmd = useAction(api.lists.vocab);
  const timerStart = useMutation(api.timers.start);
  const timerPause = useMutation(api.timers.pause);
  const timerResume = useMutation(api.timers.resume);
  const timerDismiss = useMutation(api.timers.dismiss);
  const parseCmd = useAction(api.command.parse);
  const [mode, setMode] = useState<"note" | "task" | "smart">("note");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [prop, setProp] = useState<{ actions: any[]; fallback: boolean;
    model: string | null; ms: number } | null>(null);
  const [drop, setDrop] = useState<Set<number>>(new Set());
  const reset = () => { setDraft(""); setMode("note"); setProp(null); setDrop(new Set()); };
  const submit = () => {
    const text = draft.trim();
    if (!text) { onCancel?.(); return; }
    if (mode === "smart") {
      if (busy) return;
      setBusy(true); setProp(null); setDrop(new Set());
      parseCmd({ text, today: day })
        .then(setProp).catch(fail).finally(() => setBusy(false));
      return;
    }
    (mode === "note" ? capture({ day, text, at: hhmmNow() }) : add({ day, text })).catch(fail);
    reset(); onDone?.(mode === "note" ? "✎ note queued" : "○ task queued");
  };
  const run = (a: any): Promise<unknown> => {
    const tid = a.id as Id<"timers">;
    switch (a.kind) {
      case "task": return add({ day: a.day, text: a.text });
      case "note": return capture({ day, text: a.text, at: hhmmNow() });
      case "toggle": return toggleTask({ id: a.id });
      case "edit": return editTask({ id: a.id, newText: a.newText });
      case "delete": return removeTask({ id: a.id });
      case "listAdd": return Promise.all(
        a.items.map((t: string) => addListItem({ path: a.path, text: t }).catch(fail)));
      case "listCreate": return vocabCmd({ name: a.name }).catch(() => undefined)
        .then((words) => createList({ name: a.name, words }))
        .then((path) => Promise.all((a.items ?? []).map(
          (t: string) => addListItem({ path, text: t }).catch(fail))));
      case "listSet": return setListState({ id: a.id, want: a.state });
      case "listEdit": return editListItem({ id: a.id, newText: a.newText });
      case "listRemove": return removeListItem({ id: a.id });
      case "timerStart": unlockAudio(); return timerStart({
        label: a.label, durationMs: a.ms, repeat: a.repeat, up: a.up, taskChunkId: a.taskId });
      case "timerCtl": return (a.op === "pause" ? timerPause
        : a.op === "resume" ? timerResume : timerDismiss)({ id: tid });
      default: return Promise.resolve();
    }
  };
  const queueAll = () => {
    let n = 0;
    for (const [i, a] of prop!.actions.entries()) {
      if (!drop.has(i)) { n++; void run(a).catch(fail); }
    }
    reset(); onDone?.(`✨ ${n} action${n === 1 ? "" : "s"} queued`);
  };
  const kept = prop ? prop.actions.filter((_, i) => !drop.has(i)) : [];
  return (
    <>
      <textarea className="capin" autoFocus rows={2}
        placeholder={mode === "note" ? "capture a thought…"
          : mode === "task" ? "new task…" : "tell Oriel what to do…"}
        value={draft} onChange={(e) => { setDraft(e.target.value); setProp(null); }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
          if (e.key === "Escape") onCancel?.();
        }} />
      {prop && <div className="props">
        {prop.fallback && <p className="muted small" style={{ margin: "2px 0" }}>
          couldn't read that as a command — keeping it as a note:</p>}
        {prop.actions.map((a, i) => drop.has(i) ? null : (
          <div key={i} className={`crow prow${a.kind === "delete" || a.kind === "listRemove" ? " del" : ""}`}>
            <span className="cx">{chipText(a, day)}</span>
            <button className="cancelv" aria-label="drop" onClick={() =>
              setDrop((d) => new Set(d).add(i))}>✕</button>
          </div>))}
        {!kept.length && <p className="muted small">nothing left to queue</p>}
        <p className="muted small" style={{ margin: "4px 0 0" }}>
          {prop.model} · {(prop.ms / 1000).toFixed(1)} s</p>
      </div>}
      <div className="vrow">
        <button className={`vpill ${mode === "note" ? "on" : ""}`} onClick={() => setMode("note")}>✎ note</button>
        <button className={`vpill ${mode === "task" ? "on" : ""}`} onClick={() => setMode("task")}>○ task</button>
        <button className={`vpill ${mode === "smart" ? "on" : ""}`} onClick={() => setMode("smart")}>✨ smart</button>
        {onCancel && <button className="cancelv" onClick={onCancel}>cancel</button>}
        {prop
          ? <button className="go" style={{ marginLeft: "auto" }} disabled={!kept.length}
              onClick={queueAll}>queue</button>
          : <button className="go" style={{ marginLeft: "auto" }} disabled={busy}
              onClick={submit}>{mode === "smart" ? (busy ? "…" : "✨ go") : "add"}</button>}
      </div>
    </>
  );
}

function CaptureFab({ day, onSheet }: { day: string; onSheet: () => void }) {
  const [openP, setOpenP] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { push: fail, view: errView } = useErrors();
  const done = (msg?: string) => {
    setOpenP(false);
    if (!msg) return;
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2200);
  };
  if (!openP) return (
    <>
      {toast && <div className="toast">{toast}</div>}
      <button className="fab" aria-label="capture" onClick={() => setOpenP(true)}>+</button>
    </>
  );
  return (
    <>
      <div className="veil" onClick={() => setOpenP(false)} />
      <div className="cappanel">
        {errView}
        <CaptureComposer day={day} fail={fail}
          onDone={done} onCancel={() => setOpenP(false)} />
        <p className="muted small" style={{ margin: "10px 0 0" }}>
          <button className="lnk" onClick={onSheet}>today's captures ›</button></p>
      </div>
    </>
  );
}

function CaptureSheet({ day, intents, onBack }:
  { day: string; intents?: any[]; onBack: () => void }) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const { push: fail, view: errView } = useErrors();
  const notes = (intents ?? [])
    .filter((i) => i.kind === "note" && i.day === day)
    .sort((a, b) => a.requestedAt - b.requestedAt);
  useTick(5000, notes.some((i) => !i.appliedAt && !i.error));
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button>
        <span>✎ Capture · {dayLabel(day)}</span></div>
      {errView}
      {notes.filter((i) => i.error && !dismissed.has(i.id)).map((i) => (
        <ErrBox key={i.id} onClose={() => setDismissed((d) => new Set(d).add(i.id))}>
          couldn't save "{i.text}": {i.error}</ErrBox>))}
      <p className="sect">TODAY</p>
      {!notes.length && <p className="muted small">nothing captured yet today</p>}
      {notes.filter((i) => !i.error).map((i) => {
        const pending = !i.appliedAt;
        const young = Date.now() - i.requestedAt < YOUNG_MS;
        return (
          <div key={i.id} className={`crow ${pending ? "pend" : ""}`}>
            <span className="ct">{i.at ?? ""}</span>
            <span className="cx">{i.text}</span>
            {pending && <span className={`st ${young ? "pulse" : ""}`}>{young ? "adding…" : "queued"}</span>}
          </div>
        );
      })}
      <div style={{ marginTop: 16 }}>
        <CaptureComposer day={day} fail={fail} />
      </div>
      <p className="muted small" style={{ marginTop: 12 }}>
        notes land in today's note under "Notes"; tasks join the task list</p>
    </section>
  );
}

/* ── settings: every pref in one place (they used to squat in the
   agenda/digest/timers footers) ── */
declare const __BUILD__: string;

function SettingsSheet({ onBack }: { onBack: () => void }) {
  const alerts = useAlerts();
  const rstat = useQuery(api.reminders.status, {});
  const setPref = useMutation(api.reminders.setPref);
  const mint = useMutation(api.oauth.approvalCode);
  const [approval, setApproval] = useState<{ code: string; expiresAt: number } | null>(null);
  useTick(1000, !!approval);
  const { push: fail, view: errView } = useErrors();
  const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const toggle = (on: boolean, onClick: () => void) =>
    <button className={`tog ${on ? "on" : ""}`} role="switch" aria-checked={on} onClick={onClick} />;
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button><span>⚙ Settings</span></div>
      {errView}
      <p className="sect">NOTIFICATIONS</p>
      <div className="srow">
        <span>lock-screen alerts<span className="sub">
          {alerts.state === "on" ? "on for this device"
            : alerts.state === "denied" ? "blocked — allow notifications in system Settings"
            : alerts.state === "unsupported" ? "add Oriel to your home screen first"
            : alerts.configured ? "off" : "not configured on the server"}</span></span>
        {alerts.state === "off" && alerts.configured &&
          <button className="lnk" style={{ marginLeft: "auto" }}
            onClick={() => void alerts.enable().catch(fail)}>enable</button>}
        {alerts.state === "on" && <span className="val">on</span>}
      </div>
      {rstat && <>
        <div className="srow"><span>event reminders<span className="sub">10 min before</span></span>
          {toggle(rstat.remindEvents, () => void setPref({ name: "remindEvents", value: !rstat.remindEvents }).catch(fail))}</div>
        <div className="srow"><span>morning digest<span className="sub">06:00</span></span>
          {toggle(rstat.digestPush, () => void setPref({ name: "digestPush", value: !rstat.digestPush }).catch(fail))}</div>
        <div className="srow"><span>notification times</span>
          <span className="val">{tzLabel(rstat.timezone)}</span></div>
        {deviceTz && deviceTz !== rstat.timezone &&
          <p className="muted small"><button className="lnk"
            onClick={() => void setPref({ name: "timezone", value: deviceTz }).catch(fail)}>
            use device timezone ({deviceTz})</button></p>}
      </>}
      <p className="sect">CONNECTIONS</p>
      <div className="srow">
        {approval && approval.expiresAt > Date.now()
          ? <>
              <span>claude.ai code<span className="sub">enter it on the connect page</span></span>
              <span className="val"><span className="codebox">{approval.code}</span>
                {" · "}{Math.max(0, Math.ceil((approval.expiresAt - Date.now()) / 1000))} s</span>
            </>
          : <button className="lnk" onClick={() => void mint({}).then(setApproval).catch(fail)}>connect claude.ai…</button>}
      </div>
      {rstat &&
        <div className="srow"><span>claude.ai actions<span className="sub">let it create and change tasks, lists, timers</span></span>
          {toggle(rstat.mcpWrites, () => void setPref({ name: "mcpWrites", value: !rstat.mcpWrites }).catch(fail))}</div>}
      <p className="sect">APP</p>
      <div className="srow"><a className="lnk" href="/guide.html">how to use Oriel ›</a></div>
      <p className="muted small" style={{ marginTop: 12 }}>build {__BUILD__}</p>
    </section>
  );
}

/* ── history: Search / Ask / Browse as tabs of one sheet; all three stay
   mounted so each keeps its state while the sheet is open ── */
function HistorySheet({ initial, onBack, onOpen }:
  { initial: string; onBack: () => void; onOpen: (id: string) => void }) {
  const [tab, setTab] = useState(initial);
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button><span>History</span></div>
      <div className="cbar sbar htabs">
        {(["search", "ask", "browse"] as const).map((t) =>
          <button key={t} className={tab === t ? "sel" : ""} onClick={() => setTab(t)}>{t}</button>)}
      </div>
      <div className={tab === "search" ? "" : "offstage"}><SearchSheet bare onBack={onBack} onOpen={onOpen} /></div>
      <div className={tab === "ask" ? "" : "offstage"}><AskSheet bare onBack={onBack} onOpen={onOpen} /></div>
      <div className={tab === "browse" ? "" : "offstage"}><BrowseSheet bare onBack={onBack} onOpen={onOpen} /></div>
    </section>
  );
}

/* "America/Los_Angeles" → "Pacific"; anything else shows its city. */
const tzLabel = (tz: string) =>
  tz === "America/Los_Angeles" ? "Pacific" : (tz.split("/").pop() ?? tz).replace(/_/g, " ");

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
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button>
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
              // Shorter than two text lines: one line, time and title inline.
              const h = Math.max(25, ((en - s) / 60) * HOUR_PX - 2);
              const slim = h < 38;
              return (
                <button key={x.e.id} className={`ev ${slim ? "slim" : ""} ${past ? "past" : ""}`}
                  onClick={() => onOpen(x.e.id)}
                  style={{ top: top(s), height: h,
                           left: `${(x.col / x.cols) * 100}%`, width: `calc(${100 / x.cols}% - 3px)` }}>
                  <span className="evt">{hhmm(x.start.toISOString())}{slim ? "" : `–${hhmm(x.end.toISOString())}`}</span>
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
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ back</button><span>{title}</span></div>
      {items === undefined && <Skeleton widths={[80, 62, 71]} style={{ margin: "8px 0" }} />}
      {items?.length === 0 && <p className="muted">nothing</p>}
      {items?.map((i) => <div key={i.id}>{row(i)}</div>)}
    </section>
  );
}
