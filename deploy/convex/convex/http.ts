/* Auth routes first (they must stay at the root: /.well-known/* is the
   JWT issuer), then the MCP connector's OAuth + endpoint (exact paths,
   none colliding with Convex Auth's .well-known routes), then the static
   site as the catch-all. */
import { httpRouter } from "convex/server";
import { registerStaticRoutes } from "@convex-dev/static-hosting";
import { components } from "./_generated/api";
import { auth } from "./auth";
import { endpoint, methodNotAllowed } from "./mcp";
import {
  protectedResource, authServerMeta, register, authorizeGet, authorizePost,
  token, preflight,
} from "./oauth";

const http = httpRouter();
auth.addHttpRoutes(http);

http.route({ path: "/mcp", method: "POST", handler: endpoint });
http.route({ path: "/mcp", method: "GET", handler: methodNotAllowed });
http.route({ path: "/mcp", method: "OPTIONS", handler: preflight });
http.route({ path: "/.well-known/oauth-protected-resource", method: "GET", handler: protectedResource });
// RFC 9728 path-suffix variant some clients request for a resource with a path.
http.route({ path: "/.well-known/oauth-protected-resource/mcp", method: "GET", handler: protectedResource });
http.route({ path: "/.well-known/oauth-authorization-server", method: "GET", handler: authServerMeta });
http.route({ path: "/oauth/register", method: "POST", handler: register });
http.route({ path: "/oauth/register", method: "OPTIONS", handler: preflight });
http.route({ path: "/oauth/authorize", method: "GET", handler: authorizeGet });
http.route({ path: "/oauth/authorize", method: "POST", handler: authorizePost });
http.route({ path: "/oauth/token", method: "POST", handler: token });
http.route({ path: "/oauth/token", method: "OPTIONS", handler: preflight });

registerStaticRoutes(http, components.staticHosting);

export default http;
