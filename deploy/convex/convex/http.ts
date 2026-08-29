/* Auth routes first (they must stay at the root: /.well-known/* is the
   JWT issuer), then the static site as the catch-all. */
import { httpRouter } from "convex/server";
import { registerStaticRoutes } from "@convex-dev/static-hosting";
import { components } from "./_generated/api";
import { auth } from "./auth";

const http = httpRouter();
auth.addHttpRoutes(http);
registerStaticRoutes(http, components.staticHosting);

export default http;
