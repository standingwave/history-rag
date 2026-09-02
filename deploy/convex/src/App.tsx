import { useEffect, useState } from "react";
import { Authenticated, Unauthenticated, AuthLoading, useConvexAuth } from "convex/react";
import { useAuthActions } from "@convex-dev/auth/react";
import { SignIn } from "./SignIn";
import { Today } from "./Today";

declare const __BUILD__: string;

/* True when the host serves a newer build than the one running; checked on
   load and whenever the app comes back to the foreground (the standalone
   PWA has no other way to hear about a deploy). */
function useStale() {
  const [stale, setStale] = useState(false);
  useEffect(() => {
    const check = () => fetch("/version.json", { cache: "no-store" })
      .then((r) => r.json())
      .then((v) => { if (v.build && v.build !== __BUILD__) setStale(true); })
      .catch(() => {});
    check();
    const onVis = () => document.visibilityState === "visible" && check();
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  return stale;
}

export function App() {
  const { signOut } = useAuthActions();
  const { isLoading } = useConvexAuth();
  const stale = useStale();
  return (
    <div className="col">
      <header className="hdr">
        {/* The wordmark is "go to the start": closes any open sheet, and
            reloads when already on the dashboard. */}
        <button className={`wordmark ${isLoading ? "pulse" : ""}`} title="home / reload"
          onClick={() => {
            if (new URLSearchParams(location.hash.slice(1)).get("w")) {
              history.pushState(null, "", "#");
              dispatchEvent(new PopStateEvent("popstate"));
            } else location.reload();
          }}><img src="/icon.svg" alt="" />Oriel</button>
        <Authenticated>
          <span style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <button className="lnk" onClick={() => void signOut()}>sign out</button>
            <button className="gear" aria-label="settings" title="settings"
              onClick={() => { history.pushState(null, "", "#w=settings"); dispatchEvent(new PopStateEvent("popstate")); }}>⚙</button>
          </span>
        </Authenticated>
      </header>
      {stale && <button className="wait" style={{ width: "100%", textAlign: "left", font: "inherit", cursor: "pointer" }}
        onClick={() => location.reload()}><span>new version available</span><span className="muted">tap to reload</span></button>}
      <AuthLoading><p className="muted small">signing in…</p></AuthLoading>
      <Unauthenticated><SignIn /></Unauthenticated>
      <Authenticated><Today /></Authenticated>
    </div>
  );
}
