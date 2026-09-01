/* Keep the embedding host warm (measured 2026-08-28). HF Inference: ~170 ms
   warm, ~3.5 s after 8+ min idle, still warm at 4 min — so every 3 min
   (~14k calls/month). Mixedbread needed 45 s (cold after ~1 min, 8–40 s
   to recover). Logs `warmEmbed <ms> <provider>` for the dashboard. */
import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();
crons.interval("warm embedding endpoint", { minutes: 3 }, internal.search.warmEmbed, {});
/* "What did I do over the last day?" for the dashboard tile: a haiku
   loop is a few cents, so every 3 hours keeps it fresh without waste;
   the sheet's refresh runs it on demand. */
crons.interval("last-day brief", { hours: 3 }, internal.brief.generate, {});
/* Event reminders: push ~10 min before agenda events (SPEC-event-reminders). */
crons.interval("event reminders", { minutes: 5 }, internal.reminders.sweep, {});
/* Morning digest: fires once per local morning; the tick itself decides
   whether it's 06:00 yet in the timezone pref (SPEC-morning-digest). */
crons.interval("morning digest", { minutes: 15 }, internal.pushNode.digestTick, {});
export default crons;
