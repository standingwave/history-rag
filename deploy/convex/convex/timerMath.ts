/* Timer time math (wip/SPEC-timers.md), shared by the mutations and the
   ticking UI. A timer stores either endsAt (running) or remainingMs
   (paused); everything else — done, and which cycle a repeating timer is
   in — is derived from the clock, so nothing ticks server-side. */

export const MIN_MS = 1_000;
export const MAX_MS = 24 * 3_600_000;

export type Timerish = {
  durationMs: number;
  repeat?: boolean;
  up?: boolean;      // stopwatch: endsAt is the (adjusted) start, left counts up
  endsAt?: number;
  remainingMs?: number;
};

export type TimerState = {
  st: "running" | "paused" | "done";
  left: number;    // countdown: ms remaining; stopwatch: ms elapsed
  cycle: number;   // 1-based once a repeating timer has rolled over, else 0
};

export function derive(t: Timerish, now: number): TimerState {
  if (t.up) {
    if (t.endsAt === undefined) return { st: "paused", left: t.remainingMs ?? 0, cycle: 0 };
    return { st: "running", left: Math.max(0, now - t.endsAt), cycle: 0 };
  }
  if (t.endsAt === undefined) return { st: "paused", left: t.remainingMs ?? t.durationMs, cycle: 0 };
  const left = t.endsAt - now;
  if (left > 0) return { st: "running", left, cycle: 0 };
  if (!t.repeat) return { st: "done", left: 0, cycle: 0 };
  // Current cycle end is endsAt + k·duration; left is always in (0, duration].
  const k = Math.floor((now - t.endsAt) / t.durationMs) + 1;
  return { st: "running", left: t.endsAt + k * t.durationMs - now, cycle: k };
}

/* Countdowns round up (never 0:00 while time remains); elapsed time on a
   stopwatch rounds down (0:00 until a full second has passed). */
export function fmt(ms: number, up = false): string {
  const s = Math.max(0, (up ? Math.floor : Math.ceil)(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m)) + ":" + String(r).padStart(2, "0");
}

/* "45m", "1h", "90s" — durations, for labels and unlabeled timers. */
export function durLabel(ms: number): string {
  if (ms % 3_600_000 === 0) return ms / 3_600_000 + "h";
  return ms % 60_000 ? Math.round(ms / 1000) + "s" : ms / 60_000 + "m";
}
