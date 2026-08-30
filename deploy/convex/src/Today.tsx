/* Design D (widget grid) from SPEC-dashboard-tasks.md Part B: Tasks
   (large, interactive checkboxes), Agenda + Notes (small); Search / Ask /
   Browse and the reading view are sheets from ./Archive. Sheets route by
   the `#w=` hash; `#w=x:<id>` is the reading view. */
import { useEffect, useRef, useState, type JSX } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "../convex/_generated/api";
import { SearchSheet, AskSheet, BrowseSheet, Detail, Row } from "./Archive";
import { shortDate, hhmm } from "./render";

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
function period(h: number) {
  return h < 5 ? "night" : h < 12 ? "morning" : h < 18 ? "day" : "night";
}
const ORDER: Record<string, string[]> = {
  morning: ["agenda", "tasks", "notes"],
  day: ["tasks", "agenda", "notes"],
  night: ["tasks", "notes", "agenda"],
};

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
  // Derived from `day`, not Date.now(): a fresh timestamp each render would
  // be a new subscription each render, re-running the query continuously.
  const notesSince = new Date(new Date(day + "T00:00:00").getTime() - 6 * 864e5).toISOString();
  const notes = useQuery(api.today.notes, { since: notesSince });
  const latest = useQuery(api.today.latestTaskDay, {});
  const hour = new Date().getHours();
  const openId = (id: string) => open(`x:${encodeURIComponent(id)}`);

  if (sheet === "tasks") return <TasksSheet day={day} tasks={tasks} latest={latest} onBack={close} />;
  if (sheet === "agenda") return <ListSheet title={`Agenda · ${dayLabel(day)}`} items={agenda} onBack={close}
    row={(e) => <Row c={e} right={timeOnly(e.timestamp) === "all day" ? "all day" : hhmm(e.timestamp)} onOpen={openId} />} />;
  if (sheet === "notes") return <ListSheet title="Notes · 7d" items={notes} onBack={close}
    row={(n) => <Row c={n} onOpen={openId} />} />;
  if (sheet.startsWith("x:")) return <Detail id={decodeURIComponent(sheet.slice(2))} onBack={() => history.back()} />;
  if (sheet === "search") return <SearchSheet onBack={close} onOpen={openId} />;
  if (sheet === "ask") return <AskSheet onBack={close} onOpen={openId} />;
  if (sheet === "browse") return <BrowseSheet onBack={close} onOpen={openId} />;

  const tiles: Record<string, JSX.Element> = {
    tasks: <TasksTile key="tasks" tasks={tasks} hour={hour} onOpen={() => open("tasks")} />,
    agenda: <StatTile key="agenda" title="Agenda" color="#f472b6" onOpen={() => open("agenda")}
      stat={agenda ? String(agenda.length) : "…"}
      caption={agenda?.length ? `next ${timeOnly(agenda.find((e: Item) => new Date(e.timestamp) > new Date())?.timestamp ?? agenda[0].timestamp)}` : "no events"} />,
    notes: <StatTile key="notes" title="Notes" color="#a78bfa" onOpen={() => open("notes")}
      stat={notes ? String(notes.length) : "…"} caption="edited · 7d" />,
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
      <div className="grid">{ORDER[period(hour)].map((id) => tiles[id])}</div>
    </section>
  );
}

function TasksTile({ tasks, hour, onOpen }: { tasks?: Item[]; hour: number; onOpen: () => void }) {
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
      {shown.map((t) => <TaskRow key={t.id} t={t} compact />)}
      {tasks && !main.length && <p className="muted">no note for today</p>}
      {tasks && main.length > 0 && (
        <p className="muted small">
          {more > 0 ? `+${more} more · ` : ""}{done.length} done · {list.length - main.length} routine
        </p>
      )}
    </button>
  );
}

type Acts = { onToggle: () => void; onEdit: (text: string) => void; onDelete: () => void };

/* One task. `acts` makes it editable: the row ends in a faint … that opens
   the action strip (edit · delete); edit swaps the text for an underlined
   input in place; delete asks once, naming what the block takes with it. */
type Mode = "acts" | "edit" | "delete" | null;
function TaskRow({ t, compact, acts, fixed, placeholder, mode, setMode }:
  { t: Item; compact?: boolean; acts?: Acts; fixed?: boolean; placeholder?: string; mode?: Mode; setMode?: (m: Mode) => void }) {
  const subs: { text: string; done: boolean }[] = t.meta.subtasks ?? [];
  const atts: string[] = t.meta.attachments ?? [];
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const glyph = t.meta.done ? "●" : "○";
  // A placeholder (add/edit awaiting the Mac) has no line in the note yet.
  const editable = !!acts && !placeholder;
  const status = placeholder ? `${placeholder}…` : "";
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
          ? <button className="glyph" aria-label="toggle" onClick={(e) => { e.stopPropagation(); acts.onToggle(); }}>{glyph}</button>
          : <span className="glyph">{glyph}</span>}
        <span className="ttext" onClick={() => !compact && setOpen(!open)}>{title(t)}</span>
        <span className="muted mono small">
          {status || (subs.length ? `${subs.filter((s) => s.done).length}/${subs.length}` : "")}
        </span>
        {editable && !fixed && <button className={`more ${mode ? "on" : ""}`} aria-label="actions"
          onClick={() => setMode?.(mode ? null : "acts")}>…</button>}
      </div>
      {mode === "acts" && (
        <div className="acts">
          <button className="act" onClick={startEdit}>edit</button>
          <button className="act danger" onClick={() => setMode?.("delete")}>delete</button>
        </div>
      )}
      {mode === "delete" && (
        <div className="confirm">delete this task{what ? `, ${what}` : ""}?
          <button className="act danger" onClick={() => { acts?.onDelete(); setMode?.(null); }}>yes, delete</button>
          <button className="lnk" onClick={() => setMode?.(null)}>keep</button>
        </div>
      )}
      {open && !compact && (
        <div className="tdetail">
          {subs.map((s, i) => <div key={i} className={s.done ? "done" : ""}>{s.done ? "●" : "○"} {s.text}</div>)}
          {atts.map((a: string) => <div key={a} className="muted">📎 {a}</div>)}
          {t.meta.days > 1 && <div className="muted">on the list since {shortDate(t.meta.first_seen)} · {t.meta.days} days</div>}
          <a href={`obsidian://open?vault=${encodeURIComponent(t.meta.vault)}&file=${t.day}`}>open in Obsidian ↗</a>
        </div>
      )}
    </div>
  );
}

const KIND_VERB: Record<string, string> = { toggle: "apply", add: "add", edit: "edit", delete: "delete" };

function TasksSheet({ day, tasks, latest, onBack }:
  { day: string; tasks?: Item[]; latest?: string | null; onBack: () => void }) {
  const toggle = useMutation(api.today.toggle);
  const add = useMutation(api.today.add);
  const edit = useMutation(api.today.edit);
  const remove = useMutation(api.today.remove);
  const intents = useQuery(api.today.intents, {});
  const [composing, setComposing] = useState(false);
  const [draft, setDraft] = useState("");
  const [active, setActive] = useState<{ id: string; mode: "acts" | "edit" | "delete" } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const errors = (intents ?? []).filter((i: { error: string | null; requestedAt: number }) => i.error && Date.now() - i.requestedAt < 3600e3);
  const list = tasks ?? [];
  const main = list.filter((t) => t.meta.section !== "Routine");
  const routine = list.filter((t) => t.meta.section === "Routine");
  const openT = main.filter((t) => !t.meta.done), doneT = main.filter((t) => t.meta.done);
  const fail = (e: unknown) => alert(String(e instanceof Error ? e.message : e));
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
  });
  // chunk id → what's in flight for it, so placeholders read "adding…" and
  // can't be toggled before they exist in the note.
  const inflight: Record<string, string> = {};
  for (const i of intents ?? []) if (!i.appliedAt && !i.error) {
    if (i.kind === "add") inflight[i.chunkId] = "adding";
    if (i.kind === "edit" && i.newId) inflight[i.newId] = "editing";
  }
  const row = (t: Item, fixed = false) => <TaskRow key={t.id} t={t} acts={actsFor(t)} fixed={fixed}
    placeholder={t.pending ? inflight[t.id] : undefined}
    mode={active?.id === t.id ? active.mode : null}
    setMode={(m) => setActive(m ? { id: t.id, mode: m } : null)} />;
  const vault = list[0]?.meta.vault ?? "";
  const inputOpen = composing || active?.mode === "edit";
  return (
    <section className="tasks-sheet">
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Today</button>
        <span>☑ Tasks · {doneT.length}/{main.length} done · {dayLabel(day)}</span></div>
      {errors.map((e: { id: string; kind: string; text: string; error: string | null }) =>
        <p key={e.id} className="err">couldn't {KIND_VERB[e.kind] ?? e.kind} "{e.text}": {e.error}</p>)}
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
          <button className="lnk" onClick={() => setComposing(false)}>✕</button>
        </div>
      )}
      {routine.length > 0 && <p className="sect">ROUTINE</p>}
      {routine.map((t) => row(t, true))}
      {vault && <p><a href={`obsidian://open?vault=${encodeURIComponent(vault)}&file=${day}`}>open today's note in Obsidian ↗</a></p>}
      {!inputOpen && <button className="fab" aria-label="add a task" onClick={() => { setActive(null); setComposing(true); }}>+</button>}
    </section>
  );
}

function StatTile({ title, color, stat, caption, onOpen }:
  { title: string; color: string; stat: string; caption: string; onOpen: () => void }) {
  return (
    <button className="tile small" onClick={onOpen}>
      <div className="thead"><span style={{ color }}>{title}</span><span className="muted">›</span></div>
      <div className="stat">{stat}</div>
      <div className="muted small">{caption}</div>
    </button>
  );
}

function ListSheet({ title, items, row, onBack }:
  { title: string; items?: Item[]; row: (i: Item) => JSX.Element; onBack: () => void }) {
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Today</button><span>{title}</span></div>
      {items === undefined && <p className="muted">…</p>}
      {items?.length === 0 && <p className="muted">nothing</p>}
      {items?.map((i) => <div key={i.id}>{row(i)}</div>)}
    </section>
  );
}
