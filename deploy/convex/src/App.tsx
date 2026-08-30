import { Authenticated, Unauthenticated, AuthLoading, useConvexAuth } from "convex/react";
import { useAuthActions } from "@convex-dev/auth/react";
import { SignIn } from "./SignIn";
import { Today } from "./Today";

export function App() {
  const { signOut } = useAuthActions();
  const { isLoading } = useConvexAuth();
  return (
    <div className="col">
      <header className="hdr">
        <span className={`wordmark ${isLoading ? "pulse" : ""}`}>Oriel</span>
        <Authenticated>
          <button className="lnk" onClick={() => void signOut()}>sign out</button>
        </Authenticated>
      </header>
      <AuthLoading><p className="muted small">signing in…</p></AuthLoading>
      <Unauthenticated><SignIn /></Unauthenticated>
      <Authenticated><Today /></Authenticated>
    </div>
  );
}
