/* Design D (widget grid) from SPEC-dashboard-tasks.md Part B: Tasks
   (large, interactive checkboxes), Agenda + Notes (small); Search / Ask /
   Browse and the reading view are sheets from ./Archive. Sheets route by
   the `#w=` hash; `#w=x:<id>` is the reading view. */
import { useEffect, useState, type JSX } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "../convex/_generated/api";
import { SearchSheet, AskSheet, BrowseSheet, Detail } from "./Archive";
import { shortDate } from "./render";

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
  const notes = useQuery(api.today.notes, { since: new Date(Date.now() - 7 * 864e5).toISOString() });
  const latest = useQuery(api.today.latestTaskDay, {});
  const hour = new Date().getHours();

  if (sheet === "tasks") return <TasksSheet day={day} tasks={tasks} latest={latest} onBack={close} />;
  if (sheet === "agenda") return <ListSheet title="Agenda" items={agenda} onBack={close}
    row={(e) => <><span className="mono time">{timeOnly(e.timestamp)}</span>{title(e)}</>} />;
  if (sheet === "notes") return <ListSheet title="Notes · 7d" items={notes} onBack={close}
    row={(n) => <><span className="mono time">{n.day.slice(5)}</span>{n.location}</>} />;
  const openId = (id: string) => open(`x:${encodeURIComponent(id)}`);
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

function TaskRow({ t, compact, onToggle }: { t: Item; compact?: boolean; onToggle?: () => void }) {
  const subs: { text: string; done: boolean }[] = t.meta.subtasks ?? [];
  const [open, setOpen] = useState(false);
  const glyph = t.meta.done ? "●" : "○";
  return (
    <div className={`trow ${t.meta.done ? "done" : ""} ${t.pending ? "pending" : ""}`}>
      <div className="tline">
        {onToggle
          ? <button className="glyph" aria-label="toggle" onClick={(e) => { e.stopPropagation(); onToggle(); }}>{glyph}</button>
          : <span className="glyph">{glyph}</span>}
        <span className="ttext" onClick={() => !compact && setOpen(!open)}>{title(t)}</span>
        <span className="muted mono small">
          {subs.length ? `${subs.filter((s) => s.done).length}/${subs.length}` : t.meta.days > 1 && t.meta.first_seen ? `since ${shortDate(t.meta.first_seen)}` : ""}
        </span>
      </div>
      {open && !compact && (
        <div className="tdetail">
          {subs.map((s, i) => <div key={i} className={s.done ? "done" : ""}>{s.done ? "●" : "○"} {s.text}</div>)}
          {(t.meta.attachments ?? []).map((a: string) => <div key={a} className="muted">📎 {a}</div>)}
          {t.meta.days > 1 && <div className="muted">on the list since {shortDate(t.meta.first_seen)} · {t.meta.days} days</div>}
          <a href={`obsidian://open?vault=${encodeURIComponent(t.meta.vault)}&file=${t.day}`}>open in Obsidian ↗</a>
        </div>
      )}
    </div>
  );
}

function TasksSheet({ day, tasks, latest, onBack }:
  { day: string; tasks?: Item[]; latest?: string | null; onBack: () => void }) {
  const toggle = useMutation(api.today.toggle);
  const intents = useQuery(api.today.intents, {});
  const errors = (intents ?? []).filter((i: { error: string | null; requestedAt: number }) => i.error && Date.now() - i.requestedAt < 3600e3);
  const list = tasks ?? [];
  const main = list.filter((t) => t.meta.section !== "Routine");
  const routine = list.filter((t) => t.meta.section === "Routine");
  const openT = main.filter((t) => !t.meta.done), doneT = main.filter((t) => t.meta.done);
  const onToggle = (t: Item) => void toggle({ id: t.id }).catch((e) => alert(String(e)));
  const vault = list[0]?.meta.vault ?? "";
  return (
    <section>
      <div className="daterow"><button className="lnk" onClick={onBack}>‹ Today</button>
        <span>☑ Tasks · {doneT.length}/{main.length} done · {dayLabel(day)}</span></div>
      {errors.map((e: { id: string; error: string | null }) => <p key={e.id} className="err">couldn't apply: {e.error}</p>)}
      {tasks && !list.length && (
        <p className="muted">no note for {day}{latest ? ` — latest is ${latest}` : ""}</p>
      )}
      {openT.map((t) => <TaskRow key={t.id} t={t} onToggle={() => onToggle(t)} />)}
      {doneT.map((t) => <TaskRow key={t.id} t={t} onToggle={() => onToggle(t)} />)}
      {routine.length > 0 && <p className="sect">ROUTINE</p>}
      {routine.map((t) => <TaskRow key={t.id} t={t} onToggle={() => onToggle(t)} />)}
      {vault && <p><a href={`obsidian://open?vault=${encodeURIComponent(vault)}&file=${day}`}>open today's note in Obsidian ↗</a></p>}
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
      {items?.map((i) => <div key={i.id} className="lrow">{row(i)}</div>)}
    </section>
  );
}
