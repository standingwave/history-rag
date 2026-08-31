import { writeFileSync } from "node:fs";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/* The build's identity, baked into the bundle and written next to it as
   /version.json — the app compares the two to offer a reload. */
const BUILD = Date.now().toString(36);
const versionFile = (): Plugin => ({
  name: "version-file",
  closeBundle() { writeFileSync("dist/version.json", JSON.stringify({ build: BUILD }) + "\n"); },
});

export default defineConfig({
  plugins: [react(), versionFile()],
  define: { __BUILD__: JSON.stringify(BUILD) },
  server: { host: true },   // reachable from the phone on the LAN during dev
});
