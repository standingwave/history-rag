import { useState } from "react";
import { useAuthActions } from "@convex-dev/auth/react";

/* Email + password; the backend only accepts ALLOWED_EMAIL. First run is
   "sign up" (creates the one account), every run after is "sign in". */
export function SignIn() {
  const { signIn } = useAuthActions();
  const [flow, setFlow] = useState<"signIn" | "signUp">("signIn");
  const [error, setError] = useState("");
  return (
    <form
      className="signin"
      onSubmit={(e) => {
        e.preventDefault();
        setError("");
        const fd = new FormData(e.currentTarget);
        fd.set("flow", flow);
        void signIn("password", fd).catch((err) =>
          setError(err instanceof Error ? err.message : String(err)));
      }}
    >
      <input name="email" type="email" placeholder="email" autoComplete="username" required />
      <input name="password" type="password" placeholder="password"
        autoComplete={flow === "signUp" ? "new-password" : "current-password"} required />
      <button type="submit" className="primary">
        {flow === "signIn" ? "sign in" : "create account"}
      </button>
      <button type="button" className="lnk"
        onClick={() => setFlow(flow === "signIn" ? "signUp" : "signIn")}>
        {flow === "signIn" ? "first time? create the account" : "have an account? sign in"}
      </button>
      {error && <p className="err">{error}</p>}
    </form>
  );
}
