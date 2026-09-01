"use node";
/* The actual Web Push send (VAPID + payload encryption need node
   crypto). Env: VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_SUBJECT. */
import webpush from "web-push";
import { v } from "convex/values";
import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";

export const send = internalAction({
  args: { title: v.string(), body: v.string(), tag: v.string() },
  handler: async (ctx, { title, body, tag }) => {
    const pub = process.env.VAPID_PUBLIC_KEY, priv = process.env.VAPID_PRIVATE_KEY;
    const subject = process.env.VAPID_SUBJECT;
    if (!pub || !priv || !subject) return;
    webpush.setVapidDetails(subject, pub, priv);
    const subs = await ctx.runQuery(internal.push.subs, {});
    await Promise.all(subs.map(async (s) => {
      try {
        await webpush.sendNotification(
          { endpoint: s.endpoint, keys: s.keys },
          JSON.stringify({ title, body, tag }),
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
  },
});
