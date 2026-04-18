"use client";
import { useEffect, useState } from "react";
import {
  configureAmplify,
  currentUserEmail,
  signIn,
  signUp,
  confirmSignUp,
  resendCode,
} from "../lib/auth";

type Mode = "signin" | "signup" | "confirm";

export function AuthGate({
  children,
  onUser,
}: {
  children: React.ReactNode;
  onUser: (email: string | null) => void;
}) {
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    configureAmplify();
    currentUserEmail().then((u) => {
      setUser(u);
      onUser(u);
      setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handle = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const doSignIn = () =>
    handle(async () => {
      const res = await signIn(email, password);
      if (res.isSignedIn) {
        const u = await currentUserEmail();
        setUser(u);
        onUser(u);
      } else if (res.nextStep?.signInStep === "CONFIRM_SIGN_UP") {
        setMode("confirm");
      } else {
        throw new Error("Unexpected sign-in step: " + res.nextStep?.signInStep);
      }
    });

  const doSignUp = () =>
    handle(async () => {
      await signUp(email, password);
      setMode("confirm");
    });

  const doConfirm = () =>
    handle(async () => {
      await confirmSignUp(email, code);
      // Auto sign-in after confirmation.
      await signIn(email, password);
      const u = await currentUserEmail();
      setUser(u);
      onUser(u);
    });

  const doResend = () => handle(async () => { await resendCode(email); setErr("Code resent — check your email"); });

  if (loading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", color: "var(--muted)" }}>
        Loading…
      </div>
    );
  }

  if (user) return <>{children}</>;

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", padding: 20 }}>
      <div className="card" style={{ width: "100%", maxWidth: 380 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18 }}>
          <div style={{ width: 32, height: 32, background: "var(--accent)", borderRadius: 7, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 900, fontSize: 16, color: "#1a1a1a" }}>V</div>
          <h1 style={{ fontSize: 20, fontWeight: 800 }}>Vyas-Video</h1>
        </div>
        <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16 }}>
          {mode === "signin" ? "Sign in" : mode === "signup" ? "Create account" : "Confirm email"}
        </h2>

        {err && <div className="error-banner">{err}</div>}

        {mode !== "confirm" && (
          <>
            <label className="label">Email</label>
            <input
              className="input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
              style={{ marginBottom: 12 }}
            />
            <label className="label">Password</label>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === "signup" ? "At least 8 chars, 1 upper, 1 lower, 1 digit" : "Password"}
              autoComplete={mode === "signin" ? "current-password" : "new-password"}
              style={{ marginBottom: 16 }}
            />
            <button
              className="btn btn-primary"
              style={{ width: "100%", justifyContent: "center", padding: "10px 16px" }}
              disabled={busy || !email || !password}
              onClick={mode === "signin" ? doSignIn : doSignUp}
            >
              {busy ? "…" : mode === "signin" ? "Sign in" : "Create account"}
            </button>
            <div style={{ marginTop: 14, fontSize: 13, color: "var(--muted)", textAlign: "center" }}>
              {mode === "signin" ? (
                <>No account? <a onClick={() => { setMode("signup"); setErr(null); }} style={{ cursor: "pointer" }}>Sign up</a></>
              ) : (
                <>Already registered? <a onClick={() => { setMode("signin"); setErr(null); }} style={{ cursor: "pointer" }}>Sign in</a></>
              )}
            </div>
          </>
        )}

        {mode === "confirm" && (
          <>
            <div style={{ fontSize: 13, color: "var(--muted)", marginBottom: 12 }}>
              We sent a 6-digit code to <b style={{ color: "var(--text)" }}>{email}</b>
            </div>
            <label className="label">Confirmation code</label>
            <input
              className="input"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="123456"
              style={{ marginBottom: 16, letterSpacing: 6, fontFamily: "monospace" }}
            />
            <button
              className="btn btn-primary"
              style={{ width: "100%", justifyContent: "center", padding: "10px 16px" }}
              disabled={busy || !code}
              onClick={doConfirm}
            >
              {busy ? "…" : "Confirm & sign in"}
            </button>
            <div style={{ marginTop: 14, fontSize: 13, color: "var(--muted)", textAlign: "center" }}>
              Didn&apos;t get it? <a onClick={doResend} style={{ cursor: "pointer" }}>Resend code</a>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
