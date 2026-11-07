import { useEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextField } from "@clickhouse/click-ui/TextField";
import { ExternalLink } from "../components/ui/ExternalLink";
import { Panel } from "../components/ui/Panel";
import { TabTrigger } from "../components/ui/TabTrigger";
import { selfSignup, setToken, loginWithPassword } from "../api";
import { SIGN_IN_FROM, markSignInStarted, type TokenClaims } from "../lib/boot";
import { goToSso } from "../lib/navigation";
import { errorRequestId, humanizeError } from "../lib/format";
import type { GateReason } from "../lib/state";
import { Alert } from "../components/ui";

interface TokenGateProps {
  onLogin: () => void;
  /** Why the gate is showing: "expired" when the server refused a sign-in session's stored
   *  token, "ended" any other, "refused" one a sign-in link brought; null on a first visit. */
  reason?: GateReason;
  /** A Loom page sent the browser here to start SSO (`takeSsoAsk` in `lib/boot.ts`). */
  ssoAsked?: boolean;
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
 *
 * With SSO on (the `__LOOM_SSO` boot flag) the gate is one button instead:
 * the server refuses password sign-in and sign-up there.
 */
export function TokenGate({ onLogin, reason = null, ssoAsked = false }: TokenGateProps) {
  if (window.__LOOM_SSO === true) return <SsoGate reason={reason} ssoAsked={ssoAsked} />;
  return <PasswordGate onLogin={onLogin} reason={reason} />;
}

/** Remember that this tab starts a sign-in, and the page it starts on: the token that
 *  comes back is stored without asking (`lib/boot.ts`), and the identity provider sends
 *  the browser back to the root, where boot returns here within ten minutes
 *  (`lib/linkToken.ts`). From the bare root there is nothing to come back to; from the
 *  landing (`/me`, where a restart begins) the page saved by the first start stays. */
function rememberSignInStart(): string {
  const nonce = markSignInStarted();
  const path = window.location.pathname + window.location.search;
  try {
    // A restart from the landing keeps the page the sign-in started on.
    if (window.location.pathname !== "/me") {
      if (path === "/") sessionStorage.removeItem(SIGN_IN_FROM);
      else sessionStorage.setItem(SIGN_IN_FROM, JSON.stringify({ path, at: Date.now() }));
    }
  } catch {
    /* no session storage: the sign-in lands on the root */
  }
  return nonce;
}

/** The SSO button's plain click starts this tab's own marked sign-in; a click that opens a
 *  new tab or window follows the bare link, whose token there asks to be confirmed. */
function startSso(e: MouseEvent<HTMLAnchorElement>): void {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  goToSso(rememberSignInStart());
}

// When the gate last started an SSO sign-in on its own in this tab, so a provider that
// sends the browser straight back with a token the server refuses cannot loop.
const SSO_RESUMED_AT = "loom_sso_resumed_v1";
const SSO_RESUME_GAP_MS = 5 * 60 * 1000;

/** Start one SSO sign-in without a click after an expired session; false when one was
 *  started in the last few minutes (the button is shown instead). */
export function resumeSsoOnce(now: number = Date.now()): boolean {
  try {
    const last = Number(sessionStorage.getItem(SSO_RESUMED_AT)) || 0;
    if (now - last < SSO_RESUME_GAP_MS) return false;
    sessionStorage.setItem(SSO_RESUMED_AT, String(now));
  } catch {
    return false; // no session storage: no loop guard, so no automatic sign-in
  }
  return true;
}

/** Sends the browser to the identity provider; it comes back to `/me?n=…#token=…`. After an
 *  expired session (the gate shows only while the app boots), and in the tab the
 *  session-expired card opens, it goes there by itself, so a person still signed in at the
 *  provider returns to the page they were on. */
function SsoGate({ reason, ssoAsked }: { reason?: GateReason; ssoAsked: boolean }) {
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    // A counted ask (`takeSsoAsk` in `lib/boot.ts`) follows a click on a Loom page, so it needs
    // no loop guard. The resume after an expired session is the guarded one (boot gives no
    // "expired" reason after a sign-out).
    if (!ssoAsked && (reason !== "expired" || !resumeSsoOnce())) return;
    started.current = true;
    goToSso(rememberSignInStart());
  }, [reason, ssoAsked]);
  return (
    <GateShell>
      <Panel className="space-y-4">
        <GateNotice reason={reason} />
        <a
          href="/oauth/sso"
          onClick={startSso}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-on-accent hover:opacity-90"
        >
          <Icon name="lock" size="sm" aria-hidden="true" /> Sign in with SSO
        </a>
      </Panel>
    </GateShell>
  );
}

/** Why the gate is showing, when it is not a first visit. */
function GateNotice({ reason }: { reason?: GateReason }) {
  if (reason === "expired" || reason === "ended")
    return <Alert role="status" state="neutral" text="Your session ended — sign in again." />;
  if (reason === "refused")
    return <Alert role="status" state="neutral" text="The token this sign-in brought was refused — sign in again." />;
  return null;
}

/** Asks before a `#token=` link this tab did not ask for signs the browser in: anyone
 *  holding a token can send one, and storing it unasked would put what this browser
 *  writes next in the sender's account. */
export function LinkTokenConfirm({
  subject,
  org,
  current,
  onConfirm,
  onCancel,
}: {
  /** The account the server says the link's token signs in as. */
  subject: string | null;
  org: string | null;
  /** The account of the token this browser holds, when it holds one. */
  current: TokenClaims | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <GateShell>
      <Panel className="space-y-4">
        <h2 className="m-0 text-base">Sign in from a link?</h2>
        {current?.subject ? (
          <p className="m-0 text-sm">
            You are signed in as <strong>{current.subject}</strong>
            {current.org ? ` (${current.org})` : ""}.
          </p>
        ) : null}
        <p className="m-0 text-sm">
          This link signs this browser in as {subject ? <strong>{subject}</strong> : "an account it does not name"}
          {org ? (
            <>
              {" "}
              in org <strong>{org}</strong>
            </>
          ) : null}
          . What you upload, write or mint next goes to that account. Continue only if you opened a link you asked for.
        </p>
        <div className="flex gap-2">
          <Button
            type="primary"
            htmlType="button"
            label={subject ? `Sign in as ${subject}` : "Sign in"}
            onClick={onConfirm}
          />
          <Button type="secondary" htmlType="button" label="Cancel" onClick={onCancel} />
        </div>
      </Panel>
    </GateShell>
  );
}

/** The sign-in page around either gate: the logo and its tagline. */
function GateShell({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-bg px-4">
      <div className="w-full max-w-md">
        <header className="mb-6 flex items-center gap-2">
          <div className="grid h-10 w-10 place-items-center rounded-lg bg-accent text-on-accent" aria-hidden="true">
            <Icon name="tree-structure" size="md" aria-hidden="true" />
          </div>
          <div>
            <h1 className="m-0 text-xl">Loom</h1>
            <div className="text-xs text-text-muted">memory for your AI agents</div>
          </div>
        </header>
        {children}
      </div>
    </main>
  );
}

function PasswordGate({ onLogin, reason = null }: TokenGateProps) {
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
  // Read availability off the boot snippet the dashboard handler templates in. The server knows
  // the answer; probing with a real signup for a throwaway namespace would be a read that
  // writes (it claims the namespace on a fresh instance).
  const [signupAvailable] = useState<boolean>(() => window.__LOOM_SELF_SIGNUP !== false);

  const validEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v.trim());

  // Enter submits the form without the button's disabled state, so these check busy themselves.
  const doSignin = async (e: FormEvent) => {
    e.preventDefault();
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

  const doSignup = async (e: FormEvent) => {
    e.preventDefault();
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
    <GateShell>
      <Panel className="space-y-4">
        <GateNotice reason={reason} />
        <Tabs
          value={mode}
          onValueChange={(v) => {
            setErr(null);
            setErrRequestId("");
            if (v === "signin" || v === "signup") setMode(v);
          }}
        >
          <Tabs.TriggersList aria-label="Sign in or sign up">
            <TabTrigger value="signin" icon="enter">
              Sign in
            </TabTrigger>
            {signupAvailable !== false && (
              <TabTrigger value="signup" icon="users">
                Sign up
              </TabTrigger>
            )}
          </Tabs.TriggersList>

          <Tabs.Content value="signin">
            <form onSubmit={doSignin} className="space-y-3 pt-5">
              <div>
                <TextField
                  id="gate-signin-ident"
                  label="Email or namespace"
                  aria-describedby="gate-signin-ident-help"
                  value={signinIdent}
                  onChange={setSigninIdent}
                  placeholder="you@example.com"
                  autoComplete="username"
                  autoFocus
                />
                <p id="gate-signin-ident-help" className="mt-1 text-xs text-text-muted">
                  The email you signed up with, or the name you picked at sign-up.
                </p>
              </div>
              <PasswordField
                id="gate-signin-password"
                label="Password"
                value={signinPassword}
                onChange={setSigninPassword}
                placeholder="•••••••"
                autoComplete="current-password"
              />
              <Button
                type="primary"
                htmlType="submit"
                fillWidth
                loading={busy}
                disabled={busy || !signinIdent.trim() || !signinPassword}
              >
                Sign in
              </Button>
            </form>
          </Tabs.Content>

          <Tabs.Content value="signup">
            <form onSubmit={doSignup} className="space-y-3 pt-5">
              <div>
                <TextField
                  id="gate-signup-name"
                  label="Pick a name"
                  aria-describedby="gate-signup-name-help"
                  value={name}
                  onChange={setName}
                  placeholder="alice"
                />
                <p id="gate-signup-name-help" className="mt-1 text-xs text-text-muted">
                  Used as your identity. Examples: <span className="font-mono">alice</span>,{" "}
                  <span className="font-mono">clickhouse-eng</span>.
                </p>
              </div>
              <TextField
                id="gate-signup-email"
                label="Email"
                type="email"
                value={signupEmail}
                onChange={setSignupEmail}
                placeholder="you@example.com"
                autoComplete="email"
              />
              <div>
                <PasswordField
                  id="gate-signup-password"
                  label="Password"
                  aria-describedby="gate-signup-password-help"
                  value={signupPassword}
                  onChange={setSignupPassword}
                  placeholder="•••••••"
                  autoComplete="new-password"
                />
                <p id="gate-signup-password-help" className="mt-1 text-xs text-text-muted">
                  At least 8 characters. Save it in your password manager — Loom has no reset flow.
                </p>
              </div>
              {name.trim() && (
                <p className="rounded bg-surface-2 px-3 py-2 text-xs text-text-muted">
                  You'll own <span className="font-mono text-text">{name.trim()}</span> — your private space. You can
                  create more namespaces later from the dashboard.
                </p>
              )}
              <Button
                type="primary"
                htmlType="submit"
                fillWidth
                loading={busy}
                disabled={busy || !name.trim() || !signupEmail.trim() || signupPassword.length < 8}
              >
                Sign up
              </Button>
            </form>
          </Tabs.Content>
        </Tabs>

        {err && (
          <Alert
            role="alert"
            state="danger"
            text={
              <>
                {err}
                {errRequestId && <div className="mt-1 font-mono">request id {errRequestId}</div>}
              </>
            }
          />
        )}
      </Panel>

      <p className="mt-5 text-center text-xs text-text-muted">
        Loom doesn't email-verify on signup. Run it behind a VPN, intranet, or auth proxy if exposed.
      </p>
      <div className="mt-2 text-center text-xs leading-relaxed text-text-muted">
        Running this yourself? Mint a token with <code>loom mint-token …</code> and open{" "}
        <code>&lt;server&gt;/me#token=&lt;jwt&gt;</code>, or set <code>LOOM_AUTH_REQUIRED=false</code> for a single-user
        install. See{" "}
        <ExternalLink href={DOCS_TOKENS_URL} size="sm">
          Tokens and grants
        </ExternalLink>{" "}
        and the{" "}
        <ExternalLink href={DOCS_QUICKSTART_URL} size="sm">
          Quickstart
        </ExternalLink>
        .
      </div>
    </GateShell>
  );
}
