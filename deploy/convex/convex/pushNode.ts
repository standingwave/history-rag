"use node";
/* The actual Web Push send (VAPID + payload encryption need node
   crypto). Env: VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_SUBJECT.
   Event reminders render their wall-clock text here too — full ICU is
   guaranteed in the node runtime, so timezone formatting lives in one
   place. */
import webpush from "web-push";
import { v } from "convex/values";
import { internalAction, type ActionCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import {
  reminderTitle, reminderBody, localDayHour, digestTitle, digestBody,
} from "./reminderMath";

async function fanout(ctx: ActionCtx,
                      payload: { title: string; body: string; tag: string; url?: string }) {
  const pub = process.env.VAPID_PUBLIC_KEY, priv = process.env.VAPID_PRIVATE_KEY;
  const subject = process.env.VAPID_SUBJECT;
  if (!pub || !priv || !subject) return;
  webpush.setVapidDetails(subject, pub, priv);
  const subs = await ctx.runQuery(internal.push.subs, {});
  await Promise.all(subs.map(async (s) => {
    try {
      await webpush.sendNotification(
        { endpoint: s.endpoint, keys: s.keys },
        JSON.stringify(payload),
      );
    } catch (e) {
      const code = (e as { statusCode?: number }).statusCode;
      if (code === 404 || code === 410) {
        await ctx.runMutation(internal.push.prune, { endpoint: s.endpoint });
      } else {
        console.error("push failed", s.endpoint.slice(0, 60), code, e);
      }
    }
  }));
}

export const send = internalAction({
  args: { title: v.string(), body: v.string(), tag: v.string(), url: v.optional(v.string()) },
  handler: (ctx, a) => fanout(ctx, a),
});

export const sendEvent = internalAction({
  args: { start: v.string(), title: v.string(), tz: v.string(), tag: v.string(), url: v.optional(v.string()) },
  handler: (ctx, a) => fanout(ctx, {
    title: reminderTitle(a.start, a.title, a.tz),
    body: reminderBody(Date.parse(a.start), Date.now()),
    tag: a.tag,
    url: a.url,
  }),
});

/* Morning digest: cron-ticked every 15 min; sends once per local day,
   in the 06:00–09:59 window (a phone enabled at noon waits for tomorrow
   rather than getting a "morning" digest at lunch). markSent lands
   before the send — a duplicate morning is worse than a missed one. */
export const digestTick = internalAction({
  args: {},
  handler: async (ctx) => {
    const st = await ctx.runQuery(internal.digest.state, {});
    if (!st.on || !st.subscribed) return;
    const now = Date.now();
    const { day, hour } = localDayHour(now, st.timezone);
    if (hour < 6 || hour >= 10 || st.sentDay === day) return;
    const facts = await ctx.runQuery(internal.digest.facts, { day });
    await ctx.runMutation(internal.digest.markSent, { day });
    await fanout(ctx, {
      title: digestTitle(now, st.timezone),
      body: digestBody(facts, st.timezone),
      tag: "digest",
      url: "/#w=digest",
    });
  },
});
