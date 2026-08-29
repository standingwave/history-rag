/* Keep the embedding host warm. Mixedbread goes cold per client after
   ~1 min idle and the next embed takes 8–40 s; within 30 s of the last
   call it takes ~100 ms (measured 2026-08-28). 45 s ≈ 58k calls/month.
   Logs `warmEmbed <ms>` so the dashboard's Logs page shows it's holding. */
import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();
crons.interval("warm embedding endpoint", { seconds: 45 }, internal.search.warmEmbed, {});
export default crons;
