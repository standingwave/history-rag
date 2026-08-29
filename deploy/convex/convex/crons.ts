/* Probe the embedding host. Logs `warmEmbed <ms>` so the dashboard's Logs
   page shows its latency over time (Mixedbread: 25 s stalls, 2026-08-28). */
import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();
crons.interval("warm embedding endpoint", { minutes: 15 }, internal.search.warmEmbed, {});
export default crons;
