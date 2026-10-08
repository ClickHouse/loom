import { useState } from "react";
import { Workflow, UserPlus, LogIn } from "lucide-react";
import * as Tabs from "@radix-ui/react-tabs";
import { selfSignup, setToken, loginWithPassword } from "../api";
import { errorRequestId, humanizeError } from "../lib/format";
import type { GateReason } from "../lib/state";

interface TokenGateProps {
  onLogin: () => void;
  /** Why the gate is showing: "expired" when a stored token was rejected by whoami;
   *  null/undefined on a first visit. */
  reason?: GateReason;
}

// No hosted docs site — the repo's Markdown is the documentation (README).
const REPO_DOCS = "https://github.com/ClickHouse/loom/blob/main/docs";
export const DOCS_TOKENS_URL = `${REPO_DOCS}/guides/tokens-and-grants.md`;
export const DOCS_QUICKSTART_URL = `${REPO_DOCS}/getting-started/quickstart.md`;
export const DOCS_MCP_URL = `${REPO_DOCS}/features/integrations/mcp-clients.md`;

/**
 * Sign-in / sign-up gate.
 *
 * Sign in takes an email or namespace plus a password. The Sign up tab
 * shows only while self-signup is open (the server's `__LOOM_SELF_SIGNUP`
 * boot flag). No token surfaces: the server mints the JWT and the client
 * stores it without showing it. Operators who need a raw token use the
 * `loom mint-token` CLI, which is not exposed here.
 */
export function TokenGate({ onLogin, reason = null }: TokenGateProps) {
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  // Sign-in fields
  const [signinIdent, setSigninIdent] = useState("");
  const [signinPassword, setSigninPassword] = useState("");
  // Sign-up fields
  const [name, setName] = useState("");
  const [signupEmail, setSignupEmail] = useState("");
  const [signupPassword, setSignupPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [errRequestId, setErrRequestId] = useState<string>("");
  // Read availability off the boot snippet the /me handler templates in. The server knows
  // the answer; probing with a real signup for a throwaway namespace would be a read that
  // writes (it claims the namespace on a fresh instance).
  const [signupAvailable] = useState<boolean>(() => window.__LOOM_SELF_SIGNUP !== false);

  const validEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim());

  // Enter reaches these without the button's disabled state, so they check busy themselves.
  const doSignin = async () => {
    if (busy) return;
    const ident = signinIdent.trim();
    if (!ident || !signinPassword) return;
    setBusy(true);
    setErr(null);
    setErrRequestId("");
    try {
      const r = await loginWithPassword(ident, signinPassword);
      setToken(r.token);
      onLogin();
    } catch (e) {
      setErr(humanizeError(e));
      setErrRequestId(errorRequestId(e));
    } finally {
      setBusy(false);
    }
  };

  const doSignup = async () => {
    if (busy) return;
    const ns = name.trim();
    const email = signupEmail.trim().toLowerCase();
    if (!ns) {
      setErr("Pick a name.");
      return;
    }
    if (!validEmail(email)) {
      setErr("Enter a valid email address.");
      return;
    }
    if (signupPassword.length < 8) {
      setErr("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    setErr(null);
    setErrRequestId("");
    try {
      const r = await selfSignup(ns, { email, password: signupPassword });
      if (r === null) {
        setErr("Sign-up is off on this instance. Ask your operator.");
        return;
      }
      setToken(r.token);
      onLogin();
    } catch (e) {
      setErr(humanizeError(e));
      setErrRequestId(errorRequestId(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="min-h-screen flex items-center justify-center px-4 bg-[var(--color-bg)]">
      <div className="w-full max-w-md">
        <header className="flex items-center gap-2 mb-6">
          <div className="w-10 h-10 rounded-xl bg-[var(--color-accent)] grid place-items-center" aria-hidden="true">
            <Workflow size={20} className="text-[var(--color-on-accent)]" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight m-0">Loom</h1>
            <div className="text-xs text-[var(--color-text-muted)]">memory for your AI agents</div>
          </div>
        </header>

        <div className="rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6 shadow-sm">
          {reason === "expired" && (
            <p
              role="status"
              className="mb-4 px-3 py-2 text-xs rounded-md bg-[var(--color-surface-2)] text-[var(--color-text)]"
            >
              Your session expired — sign in again.
            </p>
          )}
          <Tabs.Root
            value={mode}
            onValueChange={(v) => {
              setErr(null);
              setErrRequestId("");
              if (v === "signin" || v === "signup") setMode(v);
            }}
          >
            <Tabs.List className="inline-flex gap-1 p-1 bg-[var(--color-surface-2)] rounded-lg mb-5 text-xs w-full">
              <Tabs.Trigger
                value="signin"
                className="flex-1 px-3 py-1.5 rounded-md font-medium text-[var(--color-text-muted)] data-[state=active]:bg-[var(--color-surface)] data-[state=active]:text-[var(--color-text)] data-[state=active]:shadow-sm transition-colors"
              >
                <span className="inline-flex items-center gap-1.5 justify-center">
                  <LogIn size={12} /> Sign in
                </span>
              </Tabs.Trigger>
              {signupAvailable !== false && (
                <Tabs.Trigger
                  value="signup"
                  className="flex-1 px-3 py-1.5 rounded-md font-medium text-[var(--color-text-muted)] data-[state=active]:bg-[var(--color-surface)] data-[state=active]:text-[var(--color-text)] data-[state=active]:shadow-sm transition-colors"
                >
                  <span className="inline-flex items-center gap-1.5 justify-center">
                    <UserPlus size={12} /> Sign up
                  </span>
                </Tabs.Trigger>
              )}
            </Tabs.List>

            <Tabs.Content value="signin">
              <label htmlFor="gate-signin-ident" className="block text-sm font-medium mb-1">
                Email or namespace
              </label>
              <p id="gate-signin-ident-help" className="text-xs text-[var(--color-text-muted)] mb-2">
                The email you signed up with, or the name you picked at sign-up.
              </p>
              <input
                id="gate-signin-ident"
                aria-describedby="gate-signin-ident-help"
                title="The email you signed up with, or the name you picked at sign-up"
                value={signinIdent}
                onChange={(e) => setSigninIdent(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && doSignin()}
                placeholder="you@example.com"
                autoFocus
                className="w-full px-3 py-2.5 text-base rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] mb-3"
              />
              <label htmlFor="gate-signin-password" className="block text-sm font-medium mb-1">
                Password
              </label>
              <input
                id="gate-signin-password"
                type="password"
                value={signinPassword}
                onChange={(e) => setSigninPassword(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && doSignin()}
                placeholder="•••••••"
                className="w-full px-3 py-2.5 text-base rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
              />
              <button
                onClick={doSignin}
                disabled={busy || !signinIdent.trim() || !signinPassword}
                className="mt-4 w-full px-4 py-2.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-lg hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
              >
                {busy ? "Signing in…" : "Sign in"}
              </button>
            </Tabs.Content>

            <Tabs.Content value="signup">
              <label htmlFor="gate-signup-name" className="block text-sm font-medium mb-1">
                Pick a name
              </label>
              <p className="text-xs text-[var(--color-text-muted)] mb-2">
                Used as your identity. Examples: <span className="font-mono">alice</span>,{" "}
                <span className="font-mono">clickhouse-eng</span>.
              </p>
              <input
                id="gate-signup-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="alice"
                className="w-full px-3 py-2.5 text-base rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] mb-3"
              />
              <label htmlFor="gate-signup-email" className="block text-sm font-medium mb-1">
                Email
              </label>
              <input
                id="gate-signup-email"
                type="email"
                value={signupEmail}
                onChange={(e) => setSignupEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full px-3 py-2.5 text-base rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] mb-3"
              />
              <label htmlFor="gate-signup-password" className="block text-sm font-medium mb-1">
                Password
              </label>
              <p className="text-xs text-[var(--color-text-muted)] mb-2">
                At least 8 characters. Save it in your password manager — Loom has no reset flow.
              </p>
              <input
                id="gate-signup-password"
                type="password"
                value={signupPassword}
                onChange={(e) => setSignupPassword(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && doSignup()}
                placeholder="•••••••"
                className="w-full px-3 py-2.5 text-base rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
              />
              {name.trim() && (
                <div className="mt-3 px-3 py-2 text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] rounded-md">
                  You'll own <span className="font-mono text-[var(--color-text)]">{name.trim()}</span> — your private
                  space. You can create more namespaces later from{" "}
                  <span className="font-mono text-[var(--color-text)]">/me</span>.
                </div>
              )}
              <button
                onClick={doSignup}
                disabled={busy || !name.trim() || !signupEmail.trim() || signupPassword.length < 8}
                className="mt-4 w-full px-4 py-2.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-lg hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
              >
                {busy ? "Signing up…" : "Sign up"}
              </button>
            </Tabs.Content>
          </Tabs.Root>

          {err && (
            <div role="alert" className="mt-3 px-3 py-2 text-xs text-danger bg-danger/15 rounded-md">
              {err}
              {errRequestId && <div className="mt-1 font-mono opacity-80">request id {errRequestId}</div>}
            </div>
          )}
        </div>

        <p className="text-center mt-5 text-2xs text-[var(--color-text-subtle)]">
          Loom doesn't email-verify on signup. Run it behind a VPN, intranet, or auth proxy if exposed.
        </p>
        <p className="text-center mt-2 text-2xs text-[var(--color-text-subtle)] leading-relaxed">
          Running this yourself? Mint a token with <code>loom mint-token …</code> and open{" "}
          <code>/me#token=&lt;jwt&gt;</code>, or set <code>LOOM_AUTH_REQUIRED=false</code> for a single-user install.
          See{" "}
          <a
            href={DOCS_TOKENS_URL}
            target="_blank"
            rel="noreferrer"
            className="underline hover:text-[var(--color-text)]"
          >
            Tokens and grants
          </a>{" "}
          and the{" "}
          <a
            href={DOCS_QUICKSTART_URL}
            target="_blank"
            rel="noreferrer"
            className="underline hover:text-[var(--color-text)]"
          >
            Quickstart
          </a>
          .
        </p>
      </div>
    </main>
  );
}
