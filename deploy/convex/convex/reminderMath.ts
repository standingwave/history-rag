/* Event-reminder logic (wip/SPEC-event-reminders.md), pure so the sweep,
   the node sender, and the tests share it. */

export const LEAD_MS = 10 * 60_000;
export const SWEEP_MS = 5 * 60_000;
export const DEFAULT_TZ = "America/Los_Angeles";

/* Due when the event starts within lead (+ one sweep interval of slack,
   so a 5-minute cron can't step over an event) but hasn't started yet. */
export function due(startMs: number, nowMs: number): boolean {
  const delta = startMs - nowMs;
  return delta > 0 && delta <= LEAD_MS + SWEEP_MS;
}

/* "Calendar event on 2026-09-01 (Tuesday) 13:30–14:30: Dentist at Valley
   Dental — with A, B (apple:Work). Notes: …" → "Dentist at Valley
   Dental". Mirrors src/render.tsx parseCalendar, title only (that module
   pulls in React, so convex code can't import it). The room rides inside
   the title, which is why the notification body doesn't need a location. */
export function eventTitle(text: string): string {
  let rest = text
    .replace(/^Calendar event on \S+ \([^)]*\),?\s*/, "")
    .replace(/^Recurring calendar event \([^)]*\) starting \S+ \([^)]*\),?\s*/, "")
    .replace(/^(all day|\d{1,2}:\d{2}[–-]\d{1,2}:\d{2}|\d{1,2}:\d{2}):\s*/, "")
    .replace(/^:\s*/, "");
  const ni = rest.indexOf(" Notes: ");
  if (ni >= 0) rest = rest.slice(0, ni);
  rest = rest.replace(/\s*\([a-z]+:[^)]*\)\.?\s*$/, "");
  const wi = rest.indexOf(" — with ");
  if (wi >= 0) rest = rest.slice(0, wi);
  return (rest.trim() || text).split("\n", 1)[0];
}

/* Wall-clock "HH:MM" in a zone; an invalid zone falls back to Pacific
   rather than failing a send. */
export function wallTime(iso: string, tz: string): string {
  const opts = { hour: "2-digit", minute: "2-digit" } as const;
  try {
    return new Date(iso).toLocaleTimeString("en-GB", { ...opts, timeZone: tz });
  } catch {
    return new Date(iso).toLocaleTimeString("en-GB", { ...opts, timeZone: DEFAULT_TZ });
  }
}

export function reminderTitle(startIso: string, title: string, tz: string): string {
  return `⏰ ${wallTime(startIso, tz)} ${title}`;
}

export function reminderBody(startMs: number, nowMs: number): string {
  return `in ${Math.max(1, Math.round((startMs - nowMs) / 60_000))} min`;
}

/* ── morning digest ─────────────────────────────────────────────────── */

/* The zone's calendar day and hour at an instant — the digest's "is it
   morning yet, and did today's already go out" check. */
export function localDayHour(nowMs: number, tz: string): { day: string; hour: number } {
  const opts = {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", hourCycle: "h23",
  } as const;
  let fmt: Intl.DateTimeFormat;
  try {
    fmt = new Intl.DateTimeFormat("en-CA", { ...opts, timeZone: tz });
  } catch {
    fmt = new Intl.DateTimeFormat("en-CA", { ...opts, timeZone: DEFAULT_TZ });
  }
  const p = Object.fromEntries(fmt.formatToParts(new Date(nowMs)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour) };
}

export type DigestFacts = {
  events: number;                 // timed events today
  allDay: number;
  firstStart?: string;            // earliest timed event
  firstTitle?: string;
  tasksOpen: number;
  tasksDay?: string;              // set when the open tasks are an earlier day's (to carry)
};

/* "☀️ Tuesday, Sep 2" in the pref's zone. */
export function digestTitle(nowMs: number, tz: string): string {
  const opts = { weekday: "long", month: "short", day: "numeric" } as const;
  let s: string;
  try {
    s = new Date(nowMs).toLocaleDateString("en-US", { ...opts, timeZone: tz });
  } catch {
    s = new Date(nowMs).toLocaleDateString("en-US", { ...opts, timeZone: DEFAULT_TZ });
  }
  return `☀️ ${s}`;
}

const MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/* "3 events · first 10:00 Stand-up · 5 tasks to carry from Aug 31" */
export function digestBody(f: DigestFacts, tz: string): string {
  const parts: string[] = [];
  const ev = f.events + f.allDay;
  parts.push(ev ? plural(ev, "event") : "no events");
  if (f.firstStart && f.firstTitle) parts.push(`first ${wallTime(f.firstStart, tz)} ${f.firstTitle}`);
  if (!f.tasksOpen) parts.push("no open tasks");
  else if (f.tasksDay) {
    const [, m, d] = f.tasksDay.split("-").map(Number);
    parts.push(`${plural(f.tasksOpen, "task")} to carry from ${MON3[m - 1]} ${d}`);
  } else parts.push(plural(f.tasksOpen, "open task"));
  return parts.join(" · ");
}
