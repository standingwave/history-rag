/* npm test — the MCP endpoint's JSON-RPC framing and the OAuth PKCE
   helpers, both pure (convex/mcpCore.ts). */
import test from "node:test";
import assert from "node:assert/strict";
import {
  handleRpc, TOOLS, PROTOCOL_VERSIONS, b64url, sha256b64url, pkceOk, randToken,
} from "../convex/mcpCore.ts";

const noCall = async () => { throw new Error("no tool call expected"); };
const rpc = (method: string, params?: unknown, id: unknown = 1) =>
  ({ jsonrpc: "2.0", id, method, params }) as never;

test("initialize echoes a supported version, else offers the newest", async () => {
  let out = await handleRpc(rpc("initialize", { protocolVersion: "2025-03-26" }), noCall);
  assert.equal((out.body as any).result.protocolVersion, "2025-03-26");
  out = await handleRpc(rpc("initialize", { protocolVersion: "1999-01-01" }), noCall);
  assert.equal((out.body as any).result.protocolVersion, PROTOCOL_VERSIONS[0]);
  assert.deepEqual((out.body as any).result.capabilities, { tools: { listChanged: false } });
});

test("tools/list carries the four tools with schemas", async () => {
  const out = await handleRpc(rpc("tools/list"), noCall);
  const tools = (out.body as any).result.tools;
  assert.deepEqual(tools.map((t: any) => t.name).sort(),
    ["expand", "history_stats", "list_window", "search_history"]);
  for (const t of tools) {
    assert.equal(t.inputSchema.type, "object");
    assert.ok(t.description.length > 100);
  }
  assert.deepEqual(TOOLS.find((t) => t.name === "search_history")!.inputSchema.required, ["query"]);
});

test("notifications get 202 and no body; unknown methods -32601", async () => {
  const note = await handleRpc({ jsonrpc: "2.0", method: "notifications/initialized" } as never, noCall);
  assert.deepEqual(note, { status: 202, body: null });
  const out = await handleRpc(rpc("resources/list"), noCall);
  assert.equal((out.body as any).error.code, -32601);
});

test("tools/call dispatches, wraps results, and maps throws to isError", async () => {
  const out = await handleRpc(rpc("tools/call", { name: "history_stats", arguments: {} }),
    async (name, args) => ({ got: name, args }));
  const res = (out.body as any).result;
  assert.equal(res.isError, false);
  assert.deepEqual(JSON.parse(res.content[0].text), { got: "history_stats", args: {} });

  const bad = await handleRpc(rpc("tools/call", { name: "expand", arguments: { id: "x" } }),
    async () => { throw new Error("no chunk with id x"); });
  const badRes = (bad.body as any).result;
  assert.equal(badRes.isError, true);
  assert.match(badRes.content[0].text, /no chunk/);

  const unk = await handleRpc(rpc("tools/call", { name: "drop_tables", arguments: {} }), noCall);
  assert.equal((unk.body as any).error.code, -32602);
});

test("malformed requests are -32600", async () => {
  const out = await handleRpc({ method: "tools/list" } as never, noCall);
  assert.equal((out.body as any).error.code, -32600);
});

test("PKCE S256 round-trips and rejects a wrong verifier", async () => {
  const verifier = randToken(32);
  const challenge = await sha256b64url(verifier);
  assert.equal(await pkceOk(verifier, challenge), true);
  assert.equal(await pkceOk(verifier + "x", challenge), false);
  assert.equal(await pkceOk("", challenge), false);
  // RFC 7636 appendix B known answer
  assert.equal(await sha256b64url("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
    "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
});

test("b64url is unpadded and url-safe", () => {
  const v = b64url(new Uint8Array([251, 239, 190, 250, 251]));
  assert.doesNotMatch(v, /[+/=]/);
});
