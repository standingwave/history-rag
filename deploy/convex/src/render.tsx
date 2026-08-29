/* Per-source presentation. Every chunk gets a two-line summary (title +
   sub) for lists and a body for the reading view; sources whose text or
   meta we understand render as cards, anything else falls back to the
   raw text and a generic context dump — never a blank. */
import type { ReactNode } from "react";

export type Chunk = {
  id: string; source: string; text: string; meta?: any;
  location?: string; timestamp?: string; day?: string;
};
export type Summary = { title: string; sub?: string; when?: string };

const COLOR: Record<string, string> = {
  tasks: "#facc15", obsidian: "#a78bfa", calendar: "#f472b6", email: "#60a5fa",
  browser: "#34d399", claude: "#fb923c", git: "#f87171", shell: "#a3e635",
  appusage: "#22d3ee", digest: "#e879f9",
};
export function color(s: string) { return COLOR[s] ?? "#a6a6af"; }

/* ── helpers ──────────────────────────────────────────────────────────── */

const pad = (n: number) => String(n).padStart(2, "0");
export function localDay(ts?: string) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts.slice(0, 10) : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export function hhmm(ts?: string) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? "" : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function duration(sec: number) {
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function lines(t: string) { return t.split("\n").map((l) => l.trim()).filter(Boolean); }
function first(t: string, n = 140) { return (lines(t)[0] ?? "").slice(0, n); }
function host(u?: string) { try { return u ? new URL(u).hostname.replace(/^www\./, "") : ""; } catch { return ""; } }
function project(hash?: string) {
  return hash ? hash.replace(/^-Users-[^-]+-/, "~/").replace(/-/g, "/") : "";
}
function shortPath(p?: string) { return (p ?? "").replace(/\.md$/, ""); }
function home(p?: string) { return (p ?? "").replace(/^\/Users\/[^/]+/, "~"); }
function stripTags(t: string) { return t.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(); }
/* Calendar notes and email bodies arrive as HTML fragments plus Google
   Meet's "-::~:~::~" fences: keep anchor hrefs as text, drop the rest. */
export function cleanHtml(t: string) {
  return t
    .replace(/<a\s[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi, (_, href, label) =>
      stripTags(label) && stripTags(label) !== href ? `${stripTags(label)} ${href}` : href)
    .replace(/<br\s*\/?>|<\/?p[\s>]|<\/div>|<\/li>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, " ")
    .replace(/[-:~]{8,}/g, "\n")
    .replace(/[ \t]+/g, " ").replace(/\n\s*\n+/g, "\n").trim();
}
const URL_RE = /(https?:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?])/g;
/* Text with URLs as links; the only safe markup we render from content. */
export function Linkify({ text }: { text: string }) {
  const parts = text.split(URL_RE);
  return <>{parts.map((p, i) => (i % 2
    ? <a key={i} href={p} target="_blank" rel="noreferrer" className="link">{p.replace(/^https?:\/\//, "").slice(0, 60)}{p.length > 68 ? "…" : ""}</a>
    : <span key={i}>{p}</span>))}</>;
}
/* Claude turns can be tool/task notifications wrapped in tags; the
   <summary> is the readable line when there is one. */
function claudeTitle(t: string) {
  const sm = /<summary>([\s\S]*?)<\/summary>/.exec(t);
  if (sm) return stripTags(sm[1]).slice(0, 120);
  const body = t.replace(/<[a-z-]+>[^<\n]{0,40}<\/[a-z-]+>/g, "");
  return first(stripTags(body), 120) || "(empty turn)";
}
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
export function shortDate(iso?: string) {
  if (!iso) return "";
  const [, m, d] = iso.slice(0, 10).split("-").map(Number);
  return m && d ? `${MON[m - 1]} ${d}` : iso;
}
function plural(n: number, w: string) { return `${n} ${w}${n === 1 ? "" : "s"}`; }

/* "Calendar event on 2026-06-16 (Monday) 12:45–13:45: TITLE — with A, B
   (apple:Work). Notes: …" and the all-day variant. */
export function parseCalendar(text: string, meta: any = {}) {
  let rest = text.replace(/^Calendar event on \S+ \([^)]*\),?\s*/, "");
  let time = "";
  const m = /^(all day|\d{1,2}:\d{2}[–-]\d{1,2}:\d{2}|\d{1,2}:\d{2}):\s*/.exec(rest);
  if (m) { time = m[1]; rest = rest.slice(m[0].length); }
  let notes = "";
  const ni = rest.indexOf(" Notes: ");
  if (ni >= 0) { notes = cleanHtml(rest.slice(ni + 8)); rest = rest.slice(0, ni); }
  rest = rest.replace(/\s*\((?:apple|google|ics|[a-z]+):[^)]*\)\.?\s*$/, "");
  let attendees: string[] = meta.attendees ?? [];
  const wi = rest.indexOf(" — with ");
  if (wi >= 0) {
    if (!attendees.length) attendees = rest.slice(wi + 8).split(/,\s*/).filter(Boolean);
    rest = rest.slice(0, wi);
  }
  return { title: rest.trim() || first(text), time: meta.all_day ? "all day" : time,
           attendees, notes, calendar: meta.calendar ?? "" };
}

/* ── summaries ────────────────────────────────────────────────────────── */

export function describe(c: Chunk): Summary {
  const m = c.meta ?? {};
  const when = c.day || localDay(c.timestamp);
  switch (c.source) {
    case "tasks":
      return { title: first(c.text).replace(/^Task: /, ""),
               sub: [m.done ? "done" : "open", m.section, m.days > 1 && m.first_seen ? `since ${shortDate(m.first_seen)}` : ""].filter(Boolean).join(" · "), when };
    case "obsidian": {
      const ls = lines(c.text);
      const title = [shortPath(m.path ?? c.location), m.heading].filter(Boolean).join(" › ");
      const sub = (ls[0] === (m.heading || shortPath(m.path)) ? ls.slice(1) : ls).join(" ").slice(0, 120);
      return { title, sub, when };
    }
    case "calendar": {
      const ev = parseCalendar(c.text, m);
      return { title: ev.title,
               sub: [ev.time, ev.calendar, ev.attendees.length ? `${ev.attendees.length} attendees` : ""].filter(Boolean).join(" · "),
               when };
    }
    case "email":
      return { title: m.subject || first(c.text), sub: [m.from, m.mailbox].filter(Boolean).join(" · "),
               when: localDay(m.date) || when };
    case "browser":
      return { title: m.title || first(c.text).split(" — ")[0], sub: host(m.url) || c.location,
               when };
    case "claude":
      return { title: claudeTitle(c.text), sub: [home(c.location) || project(m.project_hash), m.role].filter(Boolean).join(" · "), when };
    case "git":
      return { title: first(c.text), sub: `${(m.repo ?? c.location ?? "").split("/").pop()} @ ${(m.sha ?? "").slice(0, 7)}`, when };
    case "shell": {
      const cmds = lines(c.text).slice(1);
      return { title: m.kind === "session" ? `${home(m.cwd ?? c.location)} · ${plural(m.commands ?? cmds.length, "command")}` : first(c.text),
               sub: cmds[0]?.slice(0, 100), when };
    }
    case "appusage":
      return { title: m.app ? `${m.app} · ${duration(m.seconds ?? 0)}` : first(c.text), sub: m.date, when };
    case "digest":
      return { title: `${m.digest_of ?? c.location ?? "digest"} · ${m.date ?? when}`,
               sub: c.text.replace(/^[^:]*: /, "").slice(0, 140), when };
    default:
      return { title: first(c.text), sub: c.location, when };
  }
}

/* ── reading view ─────────────────────────────────────────────────────── */

function Field({ k, v }: { k: string; v?: ReactNode }) {
  if (v === undefined || v === null || v === "") return null;
  return <div className="field"><span className="fk">{k}</span><span className="fv">{v}</span></div>;
}
function Pre({ children, clean }: { children?: string; clean?: boolean }) {
  if (!children) return null;
  return <pre className="body"><Linkify text={clean ? cleanHtml(children) : children} /></pre>;
}

function GenericContext({ ctx }: { ctx: any }) {
  if (ctx == null) return null;
  if (typeof ctx === "string") return <Pre>{ctx}</Pre>;
  if (typeof ctx === "object" && "note" in ctx && Object.keys(ctx).length === 1) return <p className="muted small">{ctx.note}</p>;
  return <Pre>{JSON.stringify(ctx, null, 1)}</Pre>;
}

export function DetailBody({ chunk, context, contextSource, onOpen }:
  { chunk: Chunk; context: any; contextSource?: string | null; onOpen: (id: string) => void }) {
  const m = chunk.meta ?? {};
  const ctxTitle = `CONTEXT · ${contextSource ?? "index"}`;
  const list = (items: any[], row: (x: any) => ReactNode) => (
    <div className="ctxlist">
      {items.map((x, i) => (
        <div key={x.id ?? i} className={`ctxrow ${x.target ? "target" : ""}`}
          onClick={() => x.id && x.id !== chunk.id && onOpen(x.id)}>{row(x)}</div>
      ))}
    </div>
  );

  switch (chunk.source) {
    case "calendar": {
      const ev = parseCalendar(chunk.text, m);
      return <>
        <h2 className="dtitle">{ev.title}</h2>
        <Field k="when" v={`${localDay(chunk.timestamp) || m.start} · ${ev.time || hhmm(chunk.timestamp)}`} />
        <Field k="calendar" v={[m.app, ev.calendar].filter(Boolean).join(" · ")} />
        <Field k="with" v={ev.attendees.length ? ev.attendees.join(", ") : undefined} />
        {ev.notes && <Pre>{ev.notes}</Pre>}
        {context?.agenda && <><p className="sect">AGENDA · {context.day}</p>
          {list(context.agenda, (e) => <><span className="mono time">{hhmm(e.timestamp) || "all day"}</span>{parseCalendar(e.text).title}</>)}</>}
      </>;
    }
    case "email":
      return <>
        <h2 className="dtitle">{m.subject || first(chunk.text)}</h2>
        <Field k="from" v={m.from} />
        <Field k="to" v={Array.isArray(m.to) ? m.to.join(", ") : m.to} />
        <Field k="date" v={m.date ? `${localDay(m.date)} ${hhmm(m.date)}` : undefined} />
        <Field k="mailbox" v={m.mailbox} />
        <Pre clean>{chunk.text.replace(/^Email from .*?\): /, "")}</Pre>
        <GenericContext ctx={context} />
      </>;
    case "browser":
      return <>
        <h2 className="dtitle">{m.title || first(chunk.text)}</h2>
        <Field k="url" v={m.url ? <a href={m.url} target="_blank" rel="noreferrer">{m.url}</a> : undefined} />
        <Field k="visits" v={m.visit_count} />
        <Field k="profile" v={chunk.location} />
        {context?.visits && <><p className="sect">SAME DAY · {context.day}</p>
          {list(context.visits, (v) => <><span className="mono time">{hhmm(v.timestamp)}</span>{v.text.split(" — ")[0]}</>)}</>}
      </>;
    case "claude":
      return <>
        <Field k="project" v={home(chunk.location) || project(m.project_hash)} />
        <Field k="role" v={m.role} />
        <Pre>{chunk.text}</Pre>
        {context?.turns && <><p className="sect">CONVERSATION</p>
          {list(context.turns, (t) => <><span className="mono time" style={{ color: t.role === "user" ? "#8ab4f8" : "#a6a6af" }}>{t.role}</span><span className="clip">{t.text}</span></>)}</>}
      </>;
    case "git":
      return <>
        <Field k="repo" v={m.repo ?? chunk.location} />
        <Field k="commit" v={<span className="mono">{(m.sha ?? "").slice(0, 12)}</span>} />
        <Pre>{chunk.text}</Pre>
        {context?.show ? <><p className="sect">GIT SHOW</p><Pre>{context.show}</Pre></> : <GenericContext ctx={context} />}
      </>;
    case "shell": {
      const cmds = lines(chunk.text).slice(1);
      return <>
        <Field k="cwd" v={m.cwd ?? chunk.location} />
        <Field k="span" v={m.start ? `${localDay(m.start)} ${hhmm(m.start)}–${hhmm(m.end)}` : undefined} />
        <pre className="body">{cmds.join("\n") || chunk.text}</pre>
        {context && <><p className="sect">{ctxTitle}</p><GenericContext ctx={context} /></>}
      </>;
    }
    case "obsidian":
      return <>
        <h2 className="dtitle">{[shortPath(m.path ?? chunk.location), m.heading].filter(Boolean).join(" › ")}</h2>
        <Field k="vault" v={m.vault} />
        {context?.note_text
          ? <><p className="sect">WHOLE NOTE</p><Pre>{context.note_text}</Pre></>
          : <><Pre>{chunk.text}</Pre>
              {context?.sections && <><p className="sect">OTHER SECTIONS</p>
                {list(context.sections, (s) => <span className="clip">{s.location?.split("#").pop() || first(s.text)}</span>)}</>}</>}
        {m.vault && m.path && <p><a href={`obsidian://open?vault=${encodeURIComponent(m.vault)}&file=${encodeURIComponent(shortPath(m.path))}`}>open in Obsidian ↗</a></p>}
      </>;
    case "tasks":
      return <>
        <h2 className="dtitle">{first(chunk.text).replace(/^Task: /, "")}</h2>
        <Field k="state" v={m.done ? `done${m.done_on ? ` · ${m.done_on}` : ""}` : "open"} />
        <Field k="listed" v={m.first_seen && m.last_seen ? `${m.first_seen} → ${m.last_seen} (${m.days ?? 1}d)` : undefined} />
        {(m.subtasks ?? []).length > 0 && <div className="ctxlist">{m.subtasks.map((s: any, i: number) =>
          <div key={i} className={`ctxrow ${s.done ? "done" : ""}`}>{s.done ? "●" : "○"} {s.text}</div>)}</div>}
        {context?.tasks && <><p className="sect">THAT DAY · {context.day}</p>
          {list(context.tasks, (t) => <span className={t.done ? "done" : ""}>{t.done ? "●" : "○"} {t.text}{t.section === "Routine" ? <span className="muted"> · routine</span> : null}</span>)}</>}
      </>;
    case "appusage":
      return <>
        <h2 className="dtitle">{m.app ?? first(chunk.text)}</h2>
        <Field k="date" v={m.date} />
        <Field k="time" v={m.seconds != null ? duration(m.seconds) : undefined} />
        {context?.seconds_by_app && <><p className="sect">THE DAY</p>
          {list(Object.entries(context.seconds_by_app).sort((a: any, b: any) => b[1] - a[1]).map(([app, s]) => ({ app, s })),
            (r) => <><span className="mono time">{duration(r.s)}</span>{r.app}</>)}</>}
      </>;
    case "digest":
      return <>
        <h2 className="dtitle">{m.digest_of ?? chunk.location} · {m.date}</h2>
        <p className="answer">{chunk.text.replace(/^[^:]*: /, "")}</p>
        {context?.rollup && <><p className="sect">ROLLUP</p><GenericContext ctx={context.rollup} /></>}
      </>;
    default:
      return <>
        <Field k="location" v={chunk.location} />
        <Pre>{chunk.text}</Pre>
        {context && <><p className="sect">{ctxTitle}</p><GenericContext ctx={context} /></>}
      </>;
  }
}
