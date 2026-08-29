import { defineApp } from "convex/server";
import rag from "@convex-dev/rag/convex.config.js";
import staticHosting from "@convex-dev/static-hosting/convex.config";

const app = defineApp();
app.use(rag);
app.use(staticHosting);   // app-owned routing: auth stays at the root

export default app;
