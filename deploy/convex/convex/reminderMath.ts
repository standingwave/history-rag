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

/* "⏰ 13:30 Dentist" in the pref's zone; an invalid zone falls back to
   Pacific rather than failing the send. */
export function reminderTitle(startIso: string, title: string, tz: string): string {
  const opts = { hour: "2-digit", minute: "2-digit" } as const;
  let hhmm: string;
  try {
    hhmm = new Date(startIso).toLocaleTimeString("en-GB", { ...opts, timeZone: tz });
  } catch {
    hhmm = new Date(startIso).toLocaleTimeString("en-GB", { ...opts, timeZone: DEFAULT_TZ });
  }
  return `⏰ ${hhmm} ${title}`;
}

export function reminderBody(startMs: number, nowMs: number): string {
  return `in ${Math.max(1, Math.round((startMs - nowMs) / 60_000))} min`;
}
