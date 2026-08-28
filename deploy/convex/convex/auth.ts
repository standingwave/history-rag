/* Single-user auth: email + password, and only the one email in
   ALLOWED_EMAIL may sign up or sign in. No anonymous path exists; every
   public function calls requireUser(). */
import { Password } from "@convex-dev/auth/providers/Password";
import { convexAuth, getAuthUserId } from "@convex-dev/auth/server";
import type { QueryCtx, MutationCtx, ActionCtx } from "./_generated/server";

export const { auth, signIn, signOut, store, isAuthenticated } = convexAuth({
  providers: [
    Password({
      profile(params) {
        const email = String(params.email ?? "").trim().toLowerCase();
        const allowed = (process.env.ALLOWED_EMAIL ?? "").trim().toLowerCase();
        if (!allowed || email !== allowed) {
          throw new Error("sign-in not allowed for this address");
        }
        return { email };
      },
    }),
  ],
});

export async function requireUser(ctx: QueryCtx | MutationCtx) {
  const userId = await getAuthUserId(ctx);
  if (!userId) throw new Error("not signed in");
  return userId;
}

export async function requireUserAction(ctx: ActionCtx) {
  const identity = await ctx.auth.getUserIdentity();
  if (!identity) throw new Error("not signed in");
  return identity;
}
