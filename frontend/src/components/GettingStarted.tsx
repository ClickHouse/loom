import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { ProgressBar } from "@clickhouse/click-ui/ProgressBar";
import { listTokens, type TokenEntry } from "../api";
import { getModelStatus } from "../api/models";
import { useStore } from "../lib/state";
import { pluginInstallLines } from "../lib/clients";
import { parseChTime } from "../lib/format";
import { useVisiblePoll } from "../lib/hooks";
import { hasPlatformAdminClaim } from "../lib/role";
import {
  countDone,
  loadOnboarding,
  ONBOARDING_STEPS,
  saveOnboarding,
  type OnboardingState,
  type OnboardingStep,
} from "../lib/onboarding";
import { AddMemoryDialog } from "../flows/AddMemoryDialog";
import { DOCS_MCP_URL } from "../flows/TokenGate";
import { ExternalLink, Loading, Panel } from "./ui";
import { CopyButton } from "./CopyButton";
import type { Identity } from "../api";

/** The token form, with the dialog that shows each client's setup once a token is made:
 *  the Tokens page's own, loaded only when the step is opened. */
const QuickConnect = lazy(() => import("../views/Tokens").then((m) => ({ default: m.QuickConnect })));

/** The dashboard writes as this agent; a memory from any other agent means one connected. */
export const DASHBOARD_AGENT = "loom-ui";
/** A token made in this visit is polled for its first call this long, every 15 s. */
const FIRST_CALL_WAIT_MS = 10 * 60_000;

export interface OnboardingHandle {
  state: OnboardingState;
  mark: (step: OnboardingStep) => void;
  start: () => void;
  dismiss: () => void;
}

export function useOnboarding(subject: string, namespace: string): OnboardingHandle {
  const [state, setState] = useState<OnboardingState>(() => loadOnboarding(subject, namespace));
  useEffect(() => {
    setState(loadOnboarding(subject, namespace));
  }, [subject, namespace]);
  const update = useCallback(
    (fn: (prev: OnboardingState) => OnboardingState) => {
      setState((prev) => {
        const next = fn(prev);
        if (next !== prev) saveOnboarding(subject, namespace, next);
        return next;
      });
    },
    [subject, namespace],
  );
  const mark = useCallback(
    (step: OnboardingStep) =>
      update((prev) => (prev.done[step] ? prev : { ...prev, done: { ...prev.done, [step]: true } })),
    [update],
  );
  const start = useCallback(() => update((prev) => (prev.started ? prev : { ...prev, started: true })), [update]);
  const dismiss = useCallback(() => update((prev) => (prev.dismissed ? prev : { ...prev, dismissed: true })), [update]);
  return { state, mark, start, dismiss };
}

/** Where the connect step stands. */
type Connect = "none" | "waiting" | "connected";

/** A token of mine that reaches this namespace and can still make a call. */
function reachesHere(t: TokenEntry, namespace: string): boolean {
  const ends = parseChTime(t.expires_at);
  const expired = !!ends && ends.getTime() <= Date.now();
  return (
    t.yours !== false && !t.revoked_at && !expired && (t.namespaces.includes("*") || t.namespaces.includes(namespace))
  );
}

function connectState(args: {
  agentSeen: boolean;
  tokens: TokenEntry[] | null;
  namespace: string;
  authOff: boolean;
}): Connect {
  if (args.agentSeen) return "connected";
  if (args.authOff) return "waiting";
  const mine = (args.tokens ?? []).filter((t) => reachesHere(t, args.namespace));
  if (mine.some((t) => t.last_used_at)) return "connected";
  return mine.length > 0 ? "waiting" : "none";
}

export interface GettingStartedProps {
  onboarding: OnboardingHandle;
  identity: Identity;
  /** The namespace's own memory list (unfiltered, live, first page) holds a memory an agent
   *  other than the dashboard wrote. */
  agentSeen: boolean;
  /** The namespace holds at least one memory. */
  hasMemory: boolean;
  authOff: boolean;
  /** One line under the list's toolbar instead of the full checklist. */
  collapsed?: boolean;
  onExpand?: () => void;
  onMemoryAdded: () => void;
  /** Hidden: the checklist leaves the page, so focus goes where the caller names. */
  onHidden?: () => void;
}

const STEP_META: Record<OnboardingStep, { title: string; why: string; icon: IconName }> = {
  connect: {
    title: "Connect an agent",
    why: "Your coding agent or AI tool saves what it learns here, and reads it back in its next session.",
    icon: "plug",
  },
  write: {
    title: "First memory",
    why: "A memory is one fact Loom keeps: a preference, a decision, a deadline your tools should not forget. Your agent saves them as it works; you can add one too.",
    icon: "pencil",
  },
};

/**
 * GettingStarted — the first-run checklist, at the top of Memories (the bare root's
 * landing): connect an agent, see the first memory, and for whoever can set them, the
 * models. Each step ticks from what Loom holds — a memory an agent wrote, a token's first
 * call, the memory count — never from pressing its button; ticks persist per subject and
 * namespace (lib/onboarding.ts) so a later filter or a revoked token never un-ticks one.
 */
export function GettingStarted({
  onboarding,
  identity,
  agentSeen,
  hasMemory,
  authOff,
  collapsed = false,
  onExpand,
  onMemoryAdded,
  onHidden,
}: GettingStartedProps) {
  const { state, mark } = onboarding;
  const hide = () => {
    onboarding.dismiss();
    onHidden?.();
  };
  const createRef = useRef<HTMLButtonElement | null>(null);
  const focusCreate = useRef(false);
  // Continue replaces the bar with the checklist: focus goes to its heading.
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const focusHeading = useRef(false);
  useEffect(() => {
    if (collapsed || !focusHeading.current) return;
    focusHeading.current = false;
    headingRef.current?.focus();
  }, [collapsed]);
  const whoami = useStore((s) => s.whoami);
  const navigate = useStore((s) => s.navigate);
  const [addOpen, setAddOpen] = useState(false);
  const [minting, setMinting] = useState(false);
  useEffect(() => {
    if (minting || !focusCreate.current) return;
    focusCreate.current = false;
    createRef.current?.focus();
  }, [minting]);

  const [tokens, setTokens] = useState<TokenEntry[] | null>(null);
  const [mintedAt, setMintedAt] = useState<number | null>(null);
  const reloadTokens = useCallback(() => {
    if (authOff) return;
    listTokens()
      .then(setTokens)
      .catch(() => setTokens((t) => t ?? []));
  }, [authOff]);
  useEffect(reloadTokens, [reloadTokens]);

  const connect = connectState({ agentSeen, tokens, namespace: identity.namespace, authOff });
  // Only a token made in this visit is followed to its first call; an older one is read
  // again on the next visit.
  const following = connect === "waiting" && mintedAt !== null && Date.now() - mintedAt < FIRST_CALL_WAIT_MS;
  useVisiblePoll(reloadTokens, 15_000, following && !authOff);

  // The models row: only for whoever can set them, while they are not set.
  const canSetModels = authOff || hasPlatformAdminClaim(whoami);
  const [modelsUnset, setModelsUnset] = useState(false);
  useEffect(() => {
    if (!canSetModels) return;
    const ctl = new AbortController();
    getModelStatus(ctl.signal)
      .then((s) => setModelsUnset(s.requires_setup))
      .catch(() => setModelsUnset(false));
    return () => ctl.abort();
  }, [canSetModels]);

  useEffect(() => {
    if (connect === "connected") mark("connect");
  }, [connect, mark]);
  useEffect(() => {
    if (hasMemory) mark("write");
  }, [hasMemory, mark]);

  const done: OnboardingState["done"] = {
    ...state.done,
    ...(connect === "connected" ? { connect: true } : {}),
    ...(hasMemory ? { write: true } : {}),
  };
  const n = countDone(done);
  const total = ONBOARDING_STEPS.length;
  const allDone = n === total;

  if (collapsed) {
    return (
      <div
        className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-sm border border-border bg-surface px-3 py-2"
        data-testid="getting-started-bar"
      >
        <span className="text-sm">
          <span className="text-text font-medium">Getting started</span>
          <span className="text-text-muted">
            {" "}
            · {n} of {total} set up
          </span>
        </span>
        <span className="flex items-center gap-2">
          <Button
            type="secondary"
            htmlType="button"
            onClick={() => {
              focusHeading.current = true;
              onExpand?.();
            }}
          >
            Continue
          </Button>
          <Button type="empty" htmlType="button" aria-label="Hide getting started" onClick={hide}>
            Hide
          </Button>
        </span>
      </div>
    );
  }

  return (
    <Panel
      as="section"
      aria-labelledby="getting-started-title"
      className="mb-6 max-w-3xl"
      padding="md"
      data-testid="getting-started"
    >
      <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h2 id="getting-started-title" ref={headingRef} tabIndex={-1}>
            Getting started
          </h2>
          <p className="text-xs text-text-muted mt-0.5 max-w-xl">
            Loom keeps memories and documents for your AI agents. They live in a namespace —{" "}
            <code className="code-inline text-xs">{identity.namespace}</code> — that you and the people you add can
            read.
          </p>
        </div>
        <Button type="empty" htmlType="button" onClick={hide} className="shrink-0">
          Hide
        </Button>
      </div>

      <div className="flex items-center gap-3 mb-4">
        <ProgressBar
          type="small"
          progress={(n / total) * 100}
          role="progressbar"
          aria-label="Getting started progress"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={n}
          aria-valuetext={`${n} of ${total} steps done`}
        />
        {/* A tick that lands from a poll is announced here, once. */}
        <span className="text-xs tabular text-text-muted" role="status">
          {n} of {total} done
        </span>
      </div>

      <ol className="space-y-2">
        <Step step="connect" index={0} done={!!done.connect}>
          {connect === "connected" ? (
            <p className="text-xs text-text-muted mt-1">An agent is saving memories here.</p>
          ) : authOff ? (
            <AuthOffConnect />
          ) : connect === "waiting" ? (
            <p className="text-xs text-text-muted mt-1">
              {following
                ? "Waiting for the first call from your agent. This updates on its own."
                : "Waiting for your agent's first memory. Start a session in your agent, then reload this page."}
            </p>
          ) : null}
          {!authOff && connect !== "connected" && (
            <div className="mt-2">
              {minting ? (
                <Suspense fallback={<Loading layout="inline" label="Loading the token form" />}>
                  <QuickConnect
                    onMinted={() => {
                      setMintedAt(Date.now());
                      reloadTokens();
                    }}
                    onClose={() => {
                      // The form leaves: focus goes back to the button that opened it.
                      focusCreate.current = true;
                      setMinting(false);
                    }}
                  />
                </Suspense>
              ) : (
                <div className="flex flex-wrap items-center gap-3">
                  <Button
                    ref={createRef}
                    type={connect === "none" ? "primary" : "secondary"}
                    htmlType="button"
                    onClick={() => setMinting(true)}
                  >
                    {connect === "none" ? "Create a token" : "Create another token"}
                  </Button>
                  <ExternalLink href={DOCS_MCP_URL} size="sm">
                    Learn more: connecting an agent
                  </ExternalLink>
                </div>
              )}
            </div>
          )}
        </Step>
        <Step step="write" index={1} done={!!done.write}>
          {!done.write && (
            <div className="mt-2">
              <Button
                type={connect === "connected" ? "primary" : "secondary"}
                htmlType="button"
                onClick={() => setAddOpen(true)}
              >
                Add a memory yourself
              </Button>
            </div>
          )}
        </Step>
        {canSetModels && modelsUnset && (
          <li className="rounded-sm border border-border-strong bg-surface-2 p-3">
            <div className="flex items-start gap-3">
              <span className="mt-0.5 shrink-0" aria-hidden="true">
                <Icon name="gear" size="md" color="var(--color-text-muted)" />
              </span>
              <div className="flex-1 min-w-0">
                <h3>Set up models</h3>
                <p className="text-xs text-text-muted mt-0.5">
                  Loom uses a language model to pull facts out of conversations and an embedding model to search them.
                  Pick a provider and add its key.
                </p>
                <div className="mt-2">
                  <Button
                    type="secondary"
                    htmlType="button"
                    onClick={() => navigate("setup", { query: { tab: "models" } })}
                  >
                    Open Models &amp; keys
                  </Button>
                </div>
              </div>
            </div>
          </li>
        )}
      </ol>

      {allDone && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
          <span className="text-success font-medium">All set: your agent is saving memories here.</span>
          <Button type="secondary" htmlType="button" onClick={hide}>
            Hide this checklist
          </Button>
        </div>
      )}

      <AddMemoryDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        identity={identity}
        // The step ticks from the count the list reads again, not from this dialog.
        onAdded={onMemoryAdded}
      />
    </Panel>
  );
}

function Step({
  step,
  index,
  done,
  children,
}: {
  step: OnboardingStep;
  index: number;
  done: boolean;
  children: React.ReactNode;
}) {
  const meta = STEP_META[step];
  return (
    <li className={`rounded-sm border p-3 ${done ? "border-border bg-surface" : "border-border-strong bg-surface-2"}`}>
      <div className="flex items-start gap-3">
        <span className="mt-0.5 shrink-0" aria-hidden="true">
          {done ? (
            <Icon name="check-in-circle" size="md" color="var(--color-success)" />
          ) : (
            <Icon name="circle" size="md" color="var(--color-text-muted)" />
          )}
        </span>
        <div className="flex-1 min-w-0">
          <h3 className="flex items-center gap-2">
            <span className="text-text-muted tabular">{index + 1}.</span>
            <Icon name={meta.icon} size="xs" aria-hidden="true" />
            <span>{meta.title}</span>
            {done && <span className="sr-only">(done)</span>}
          </h3>
          <p className="text-xs text-text-muted mt-0.5">{meta.why}</p>
          {children}
        </div>
      </div>
    </li>
  );
}

/** A single-user server takes no token: the plugin needs only the address. */
function AuthOffConnect() {
  const lines = `${pluginInstallLines("claude-code")} --config server_url=${window.location.origin}`;
  return (
    <div className="mt-2">
      <p className="text-xs text-text-muted mb-1">
        This server runs for one person, so no token is needed. For Claude Code, run this once in a terminal, then start
        a new session; other agents connect from Settings › Tokens.
      </p>
      <CopyBlock text={lines} label="Copy commands" />
    </div>
  );
}

function CopyBlock({ text, label }: { text: string; label: string }) {
  return (
    <div className="relative">
      <pre className="text-xs font-mono bg-bg border border-border rounded-sm p-2.5 pr-32 max-h-40 overflow-auto whitespace-pre-wrap break-all">
        {text}
      </pre>
      <CopyButton text={text} label={label} className="absolute top-1.5 right-1.5" />
    </div>
  );
}
