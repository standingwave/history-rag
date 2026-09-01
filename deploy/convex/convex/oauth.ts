/* Minimal single-user OAuth 2.1 authorization server for the MCP
   connector (wip/SPEC-convex-mcp.md D2). claude.ai discovers it via the
   RFC 8414/9728 metadata, self-registers (DCR), and runs code+PKCE. The
   authorize step trusts the already-signed-in app: Oriel mints an 8-digit
   approval code, the authorize page takes it, nothing else grants. */
import { v } from "convex/values";
import { httpAction, mutation, internalMutation, internalQuery } from "./_generated/server";
import { internal } from "./_generated/api";
import { requireUser } from "./auth";
import { randToken, sha256b64url, pkceOk } from "./mcpCore";

const CODE_TTL = 5 * 60_000;            // auth + approval codes
const TOKEN_TTL = 90 * 24 * 3600_000;   // bearer lifetime

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, mcp-protocol-version",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...CORS } });
export const preflight = httpAction(async () => new Response(null, { status: 204, headers: CORS }));

const origin = (req: Request) => new URL(req.url).origin;

/* ── discovery ───────────────────────────────────────────────────────── */

export const protectedResource = httpAction(async (_ctx, req) => {
  const o = origin(req);
  return json({ resource: `${o}/mcp`, authorization_servers: [o],
                bearer_methods_supported: ["header"] });
});

export const authServerMeta = httpAction(async (_ctx, req) => {
  const o = origin(req);
  return json({
    issuer: o,
    authorization_endpoint: `${o}/oauth/authorize`,
    token_endpoint: `${o}/oauth/token`,
    registration_endpoint: `${o}/oauth/register`,
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code"],
    code_challenge_methods_supported: ["S256"],
    token_endpoint_auth_methods_supported: ["none"],
    scopes_supported: ["history:read"],
  });
});

/* ── dynamic client registration ─────────────────────────────────────── */

export const register = httpAction(async (ctx, req) => {
  let body: { redirect_uris?: unknown; client_name?: unknown };
  try { body = await req.json(); } catch { return json({ error: "invalid_client_metadata" }, 400); }
  const uris = Array.isArray(body.redirect_uris) ? body.redirect_uris.map(String) : [];
  if (!uris.length || uris.some((u) => !u.startsWith("https://"))) {
    return json({ error: "invalid_redirect_uri" }, 400);
  }
  const clientId = randToken(16);
  await ctx.runMutation(internal.oauth.putClient, {
    clientId, redirectUris: uris,
    name: typeof body.client_name === "string" ? body.client_name.slice(0, 80) : undefined,
  });
  return json({
    client_id: clientId, redirect_uris: uris,
    client_name: body.client_name ?? undefined,
    token_endpoint_auth_method: "none", grant_types: ["authorization_code"],
    response_types: ["code"],
  }, 201);
});

/* ── authorize: the approval-code page ───────────────────────────────── */

function esc(s: string) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}

function page(fields: Record<string, string>, error = "") {
  const hidden = Object.entries(fields)
    .map(([k, val]) => `<input type="hidden" name="${esc(k)}" value="${esc(val)}">`).join("\n");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect to Oriel</title>
<style>:root{color-scheme:dark}body{margin:0;background:#121215;color:#e8e8eb;
font:16px/1.5 -apple-system,system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}
.card{max-width:22rem;padding:28px;text-align:center}
h1{font-size:1.1rem;margin:0 0 6px}p{color:#a6a6af;font-size:.9rem;margin:0 0 18px}
input.code{width:100%;text-align:center;font:600 1.4rem ui-monospace,Menlo,monospace;letter-spacing:.15em;
background:#0d0d10;border:1px solid #2a2a30;border-radius:10px;color:inherit;padding:12px;outline:0}
input.code:focus{border-color:#8ab4f8}
button{margin-top:14px;width:100%;background:#e8e8eb;color:#121215;border:0;border-radius:10px;
padding:12px;font:600 1rem inherit;cursor:pointer}
.err{color:#f87171;font-size:.88rem;margin:10px 0 0}</style></head><body>
<div class="card"><h1>Connect to your history</h1>
<p>Open <b>Oriel</b>, tap <i>connect claude.ai</i> in the Digest view, and enter the code it shows.</p>
<form method="POST">${hidden}
<input class="code" name="approval" inputmode="numeric" autocomplete="one-time-code"
 placeholder="0000-0000" autofocus>
<button>Approve</button>
${error ? `<p class="err">${esc(error)}</p>` : ""}</form></div></body></html>`;
}

const html = (s: string, status = 200) =>
  new Response(s, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });

async function checkAuthReq(ctx: { runQuery: Function }, p: URLSearchParams) {
  const clientId = p.get("client_id") ?? "";
  const redirect = p.get("redirect_uri") ?? "";
  const client = await (ctx.runQuery as any)(internal.oauth.getClient, { clientId });
  if (!client) return "unknown client_id";
  if (!client.redirectUris.includes(redirect)) return "redirect_uri not registered";
  if (p.get("response_type") !== "code") return "response_type must be code";
  if ((p.get("code_challenge_method") ?? "S256") !== "S256") return "only S256 is supported";
  if (!p.get("code_challenge")) return "code_challenge required";
  return "";
}

export const authorizeGet = httpAction(async (ctx, req) => {
  const p = new URL(req.url).searchParams;
  const bad = await checkAuthReq(ctx, p);
  if (bad) return html(`<!doctype html><p>${esc(bad)}</p>`, 400);
  const fields: Record<string, string> = {};
  for (const k of ["client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method"]) {
    fields[k] = p.get(k) ?? "";
  }
  return html(page(fields));
});

export const authorizePost = httpAction(async (ctx, req) => {
  const form = new URLSearchParams(await req.text());
  const bad = await checkAuthReq(ctx, form);
  if (bad) return html(`<!doctype html><p>${esc(bad)}</p>`, 400);
  const fields: Record<string, string> = {};
  for (const k of ["client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method"]) {
    fields[k] = form.get(k) ?? "";
  }
  const approval = (form.get("approval") ?? "").replace(/\D/g, "");
  const okd = await ctx.runMutation(internal.oauth.consumeApproval, { code: approval });
  if (!okd) return html(page(fields, "that code didn't match — codes are single-use and expire in 5 minutes"));
  const code = randToken(32);
  await ctx.runMutation(internal.oauth.putCode, {
    codeHash: await sha256b64url(code),
    clientId: fields.client_id, redirectUri: fields.redirect_uri,
    challenge: fields.code_challenge, expiresAt: Date.now() + CODE_TTL,
  });
  const to = new URL(fields.redirect_uri);
  to.searchParams.set("code", code);
  if (fields.state) to.searchParams.set("state", fields.state);
  return new Response(null, { status: 302, headers: { Location: to.toString() } });
});

/* ── token exchange ──────────────────────────────────────────────────── */

export const token = httpAction(async (ctx, req) => {
  const form = new URLSearchParams(await req.text());
  if (form.get("grant_type") !== "authorization_code") {
    return json({ error: "unsupported_grant_type" }, 400);
  }
  const row = await ctx.runMutation(internal.oauth.consumeCode, {
    codeHash: await sha256b64url(form.get("code") ?? ""),
  });
  if (!row) return json({ error: "invalid_grant" }, 400);
  if (form.get("client_id") && form.get("client_id") !== row.clientId) {
    return json({ error: "invalid_grant" }, 400);
  }
  if (form.get("redirect_uri") && form.get("redirect_uri") !== row.redirectUri) {
    return json({ error: "invalid_grant" }, 400);
  }
  if (!(await pkceOk(form.get("code_verifier") ?? "", row.challenge))) {
    return json({ error: "invalid_grant", error_description: "PKCE verification failed" }, 400);
  }
  const tok = randToken(32);
  await ctx.runMutation(internal.oauth.putToken, {
    tokenHash: await sha256b64url(tok), clientId: row.clientId,
    expiresAt: Date.now() + TOKEN_TTL,
  });
  return json({ access_token: tok, token_type: "Bearer",
                expires_in: Math.floor(TOKEN_TTL / 1000) });
});

/* ── storage (internal) ──────────────────────────────────────────────── */

export const putClient = internalMutation({
  args: { clientId: v.string(), redirectUris: v.array(v.string()), name: v.optional(v.string()) },
  handler: (ctx, a) => ctx.db.insert("oauthClients", a).then(() => null),
});
export const getClient = internalQuery({
  args: { clientId: v.string() },
  handler: (ctx, { clientId }) => ctx.db.query("oauthClients")
    .withIndex("by_clientId", (q) => q.eq("clientId", clientId)).unique(),
});
export const putCode = internalMutation({
  args: { codeHash: v.string(), clientId: v.string(), redirectUri: v.string(),
          challenge: v.string(), expiresAt: v.number() },
  handler: (ctx, a) => ctx.db.insert("oauthCodes", { ...a, used: false }).then(() => null),
});
export const consumeCode = internalMutation({
  args: { codeHash: v.string() },
  handler: async (ctx, { codeHash }) => {
    const row = await ctx.db.query("oauthCodes")
      .withIndex("by_codeHash", (q) => q.eq("codeHash", codeHash)).unique();
    if (!row || row.used || row.expiresAt < Date.now()) return null;
    await ctx.db.patch(row._id, { used: true });
    return { clientId: row.clientId, redirectUri: row.redirectUri, challenge: row.challenge };
  },
});
export const putToken = internalMutation({
  args: { tokenHash: v.string(), clientId: v.string(), expiresAt: v.number() },
  handler: (ctx, a) => ctx.db.insert("oauthTokens", a).then(() => null),
});
export const checkToken = internalQuery({
  args: { tokenHash: v.string() },
  handler: async (ctx, { tokenHash }) => {
    const row = await ctx.db.query("oauthTokens")
      .withIndex("by_tokenHash", (q) => q.eq("tokenHash", tokenHash)).unique();
    return !!row && row.expiresAt > Date.now();
  },
});
export const consumeApproval = internalMutation({
  args: { code: v.string() },
  handler: async (ctx, { code }) => {
    if (!/^\d{8}$/.test(code)) return false;
    const row = await ctx.db.query("mcpApprovals")
      .withIndex("by_code", (q) => q.eq("code", code)).unique();
    if (!row || row.used || row.expiresAt < Date.now()) return false;
    await ctx.db.patch(row._id, { used: true });
    return true;
  },
});

/* The app-side mint: signed-in Oriel shows this code; typing it into the
   authorize page is the approval. Old rows are swept on each mint. */
export const approvalCode = mutation({
  args: {},
  handler: async (ctx) => {
    await requireUser(ctx);
    for (const r of await ctx.db.query("mcpApprovals").collect()) {
      if (r.used || r.expiresAt < Date.now()) await ctx.db.delete(r._id);
    }
    const a = new Uint32Array(1);
    crypto.getRandomValues(a);
    const code = String(a[0] % 100_000_000).padStart(8, "0");
    const expiresAt = Date.now() + CODE_TTL;
    await ctx.db.insert("mcpApprovals", { code, expiresAt, used: false });
    return { code: `${code.slice(0, 4)}-${code.slice(4)}`, expiresAt };
  },
});
