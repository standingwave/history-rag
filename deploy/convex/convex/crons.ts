/* Probe Mixedbread's embedding endpoint. Query embeds have taken 18–41 s
   intermittently (2026-08-28) against a 0.1–0.9 s baseline; the cron logs
   `warmEmbed <ms>` so the dashboard shows how often, and keeps it warm if
   idleness is part of it. Drop to a longer interval once measured. */
import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();
crons.interval("warm embedding endpoint", { minutes: 2 }, internal.search.warmEmbed, {});
export default crons;
