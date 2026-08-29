/* Keep Mixedbread's embedding endpoint warm. Idle, it cold-starts the
   model and the first query embedding takes 15–40 s (measured 2026-08-28);
   warm, ~0.5 s. A 4-token embed every few minutes keeps searches fast. */
import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();
crons.interval("warm embedding endpoint", { minutes: 5 }, internal.search.warmEmbed, {});
export default crons;
