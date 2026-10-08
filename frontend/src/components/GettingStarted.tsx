import { useCallback, useEffect, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { CheckCircle2, Circle, ExternalLink, PenLine, Plug, Search, UserPlus } from "lucide-react";
import type { Identity, MyNamespaceEntry, ServiceAccountEntry } from "../api";
import { useStore } from "../lib/state";
import { CLIENTS, formatMcpConfig, type ClientId } from "../lib/clients";
import {
  countDone,
  loadOnboarding,
  ONBOARDING_STEPS,
  saveOnboarding,
  type OnboardingState,
  type OnboardingStep,
} from "../lib/onboarding";
import { AddMemoryDialog } from "../flows/AddMemoryDialog";
import { MembersDialog } from "../flows/MembersDialog";
import { DOCS_MCP_URL, DOCS_QUICKSTART_URL, DOCS_TOKENS_URL } from "../flows/TokenGate";
import { Kbd, Panel } from "./ui";
import { CopyButton } from "./CopyButton";

/**
 * GettingStarted — the four-step checklist a new account lands on. Each step is a card
 * with a one-sentence "why", a primary action that opens the EXISTING flow (the token
 * form, AddMemoryDialog, the ⌘K palette, MembersDialog), and a tick once done. Ticks come
 * from live data where the page already has it (tokens, memory total, palette opens) and
 * are persisted per subject in localStorage — see lib/onboarding.ts.
 */

export interface OnboardingHandle {
  state: OnboardingState;
  mark: (step: OnboardingStep) => void;
  start: () => void;
  dismiss: () => void;
}

export function useOnboarding(subject: string): OnboardingHandle {
  const [state, setState] = useState<OnboardingState>(() => loadOnboarding(subject));
  useEffect(() => {
    setState(loadOnboarding(subject));
  }, [subject]);
  const update = useCallback(
    (fn: (prev: OnboardingState) => OnboardingState) => {
      setState((prev) => {
        const next = fn(prev);
        if (next !== prev) saveOnboarding(subject, next);
        return next;
      });
    },
    [subject],
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

export interface GettingStartedProps {
  onboarding: OnboardingHandle;
  identity: Identity;
  mine: MyNamespaceEntry[] | null;
  /** Namespace-wide memory count from the one `memory.list` read Setup makes. */
  memoryTotal: number | null;
  /** The tokens list the Connect section already fetched. */
  tokens: ServiceAccountEntry[] | null;
  /** The most recent MCP token minted on this page, for the connect line. */
  lastMint: { token: string; mcpUrl: string } | null;
  /** Scroll to and focus the token form. */
  onConnect: () => void;
  onMemoryAdded: () => void;
}

const SAMPLE_QUERY = "deploy window";

const STEP_META: Record<OnboardingStep, { title: string; why: string; icon: typeof Plug }> = {
  connect: {
    title: "Connect a client",
    why: "Your AI tool talks to Loom through a token — mint one, paste one line into the tool, restart it.",
    icon: Plug,
  },
  write: {
    title: "Write your first memory",
    why: "A memory is one fact Loom keeps for you — a preference, a decision, a deadline your tools should not forget.",
    icon: PenLine,
  },
  find: {
    title: "Find it again",
    why: "Search is how your tools get memories back; try it yourself so you know what they see.",
    icon: Search,
  },
  invite: {
    title: "Invite a teammate",
    why: "Your space is private until you add someone — a viewer reads it, a writer adds to it.",
    icon: UserPlus,
  },
};

export function GettingStarted({
  onboarding,
  identity,
  mine,
  memoryTotal,
  tokens,
  lastMint,
  onConnect,
  onMemoryAdded,
}: GettingStartedProps) {
  const { state, mark, dismiss } = onboarding;
  const openPalette = useStore((s) => s.openPalette);
  const paletteOpens = useStore((s) => s.paletteOpens);
  const [addOpen, setAddOpen] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);

  // Live signals the page already holds tick a step and persist the tick,
  // so a revoked token or a switched namespace never un-ticks it later.
  const liveConnected = (tokens ?? []).some((t) => !t.revoked_at);
  const liveWrote = (memoryTotal ?? 0) > 0;
  const liveFound = paletteOpens > 0;
  useEffect(() => {
    if (liveConnected) mark("connect");
  }, [liveConnected, mark]);
  useEffect(() => {
    if (liveWrote) mark("write");
  }, [liveWrote, mark]);
  useEffect(() => {
    if (liveFound) mark("find");
  }, [liveFound, mark]);

  const done: OnboardingState["done"] = {
    ...state.done,
    ...(liveConnected ? { connect: true } : {}),
    ...(liveWrote ? { write: true } : {}),
    ...(liveFound ? { find: true } : {}),
  };
  const n = countDone(done);
  const total = ONBOARDING_STEPS.length;
  const allDone = n === total;
  const current = (mine ?? []).find((m) => m.namespace === identity.namespace);
  const spaceName = identity.namespace;

  const actions: Record<OnboardingStep, { label: string; run: () => void; docs?: { href: string; label: string } }> = {
    connect: {
      label: lastMint ? "Mint another token" : "Mint a token",
      run: onConnect,
      docs: { href: DOCS_MCP_URL, label: "Learn more: connecting a client" },
    },
    write: {
      label: "Write a memory",
      run: () => setAddOpen(true),
      docs: { href: DOCS_QUICKSTART_URL, label: "Learn more: quickstart" },
    },
    find: {
      label: "Search your memories",
      run: () => {
        openPalette(liveWrote ? "" : SAMPLE_QUERY);
        mark("find");
      },
    },
    invite: {
      label: "Add a teammate",
      run: () => setMembersOpen(true),
      docs: { href: DOCS_TOKENS_URL, label: "Learn more: roles and access" },
    },
  };

  return (
    <Panel
      as="section"
      aria-labelledby="getting-started-title"
      className="mb-8 max-w-3xl"
      padding="md"
      data-testid="getting-started"
    >
      <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <h2 id="getting-started-title" className="text-md font-semibold tracking-tight">
            Getting started
          </h2>
          <p className="text-xs text-[var(--color-text-muted)] mt-0.5 leading-relaxed max-w-xl">
            Loom remembers for your AI tools. Everything you save lands in your space (a <em>namespace</em>, in the
            docs)
            {spaceName ? (
              <>
                {" "}
                — <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 rounded">{spaceName}</code>
              </>
            ) : null}
            . Four steps show the whole loop; about five minutes.
          </p>
        </div>
        <button
          type="button"
          onClick={dismiss}
          className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline underline-offset-2 shrink-0"
        >
          Skip the tour
        </button>
      </div>

      <div className="flex items-center gap-3 mb-4">
        <div
          role="progressbar"
          aria-label="Getting started progress"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={n}
          aria-valuetext={`${n} of ${total} steps done`}
          className="flex-1 h-1.5 rounded-full bg-[var(--color-surface-2)] overflow-hidden"
        >
          <div
            className="h-full bg-[var(--color-accent)] transition-[width]"
            style={{ width: `${(n / total) * 100}%` }}
          />
        </div>
        <span className="text-xs tabular text-[var(--color-text-muted)]">
          {n} of {total} done
        </span>
      </div>

      <ol className="space-y-2">
        {ONBOARDING_STEPS.map((step, i) => {
          const meta = STEP_META[step];
          const act = actions[step];
          const isDone = !!done[step];
          const Icon = meta.icon;
          return (
            <li
              key={step}
              className={`rounded-lg border p-3 ${isDone ? "border-[var(--color-border)] bg-[var(--color-surface-2)]/60" : "border-[var(--color-border-strong)] bg-[var(--color-surface-2)]"}`}
            >
              <div className="flex items-start gap-3">
                <span className="mt-0.5 shrink-0" aria-hidden="true">
                  {isDone ? (
                    <CheckCircle2 size={18} className="text-success" />
                  ) : (
                    <Circle size={18} className="text-[var(--color-text-subtle)]" />
                  )}
                </span>
                <div className="flex-1 min-w-0">
                  <h3 className="text-sm font-semibold flex items-center gap-2">
                    <span className="text-[var(--color-text-muted)] tabular">{i + 1}.</span>
                    <Icon size={13} className="text-[var(--color-text-muted)]" aria-hidden="true" />
                    <span>{meta.title}</span>
                    {isDone && <span className="sr-only">(done)</span>}
                  </h3>
                  <p className="text-xs text-[var(--color-text-muted)] mt-0.5 leading-relaxed">{meta.why}</p>
                  {step === "find" && !isDone && (
                    <p className="text-2xs text-[var(--color-text-subtle)] mt-1">
                      Or press <Kbd>⌘K</Kbd> anywhere and type a few words.
                    </p>
                  )}
                  {step === "connect" && lastMint && <ConnectLine mint={lastMint} />}
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      onClick={act.run}
                      className={
                        isDone
                          ? "inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface)]"
                          : "inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)]"
                      }
                    >
                      {act.label}
                    </button>
                    {act.docs && (
                      <a
                        href={act.docs.href}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-2xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline underline-offset-2"
                      >
                        {act.docs.label} <ExternalLink size={10} aria-hidden="true" />
                      </a>
                    )}
                  </div>
                </div>
              </div>
            </li>
          );
        })}
      </ol>

      {allDone && (
        <div
          role="status"
          className="mt-3 pt-3 border-t border-[var(--color-border)] flex flex-wrap items-center justify-between gap-2 text-xs"
        >
          <span className="text-success font-medium">All four done — Loom is remembering for you.</span>
          <button
            type="button"
            onClick={dismiss}
            className="px-2.5 py-1 text-xs font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"
          >
            Hide this checklist
          </button>
        </div>
      )}

      <AddMemoryDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        identity={identity}
        onAdded={() => {
          mark("write");
          onMemoryAdded();
        }}
      />
      {membersOpen && (
        <MembersDialog
          open={true}
          onOpenChange={(v) => {
            if (!v) setMembersOpen(false);
          }}
          org={identity.org}
          namespace={identity.namespace}
          kind={current?.kind}
          onChanged={() => mark("invite")}
        />
      )}
    </Panel>
  );
}

/** After a mint: the one line that connects Claude Code (the config's `command`),
 *  Cursor's block, and a picker for every other client. */
function ConnectLine({ mint }: { mint: { token: string; mcpUrl: string } }) {
  const [other, setOther] = useState<ClientId>("claude-desktop");
  const claude = formatMcpConfig({ client: "claude-code", token: mint.token, mcpUrl: mint.mcpUrl });
  const cursor = formatMcpConfig({ client: "cursor", token: mint.token, mcpUrl: mint.mcpUrl });
  const otherCfg = formatMcpConfig({ client: other, token: mint.token, mcpUrl: mint.mcpUrl });
  const others = CLIENTS.filter((c) => c.id !== "claude-code" && c.id !== "cursor");
  return (
    <Tabs.Root defaultValue="claude-code" className="mt-2">
      <Tabs.List
        aria-label="Client"
        className="inline-flex p-0.5 bg-[var(--color-surface)] border border-[var(--color-border)] rounded-md gap-0.5"
      >
        {[
          ["claude-code", "Claude Code"],
          ["cursor", "Cursor"],
          ["other", "Other clients"],
        ].map(([v, label]) => (
          <Tabs.Trigger
            key={v}
            value={v}
            className="px-2.5 py-1 text-xs font-medium rounded-[5px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] data-[state=active]:bg-[var(--color-surface-2)] data-[state=active]:text-[var(--color-text)]"
          >
            {label}
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      <Tabs.Content value="claude-code" className="mt-2">
        <p className="text-2xs text-[var(--color-text-muted)] mb-1">
          Run this once in a terminal, then start a new Claude Code session — Loom&apos;s profile and context arrive
          when the client connects. For capture and hooks, install the plugin from Settings instead:
        </p>
        <CopyBlock text={claude.command ?? ""} label="Copy command" />
      </Tabs.Content>
      <Tabs.Content value="cursor" className="mt-2">
        <p className="text-2xs text-[var(--color-text-muted)] mb-1">
          Paste into Cursor's MCP settings (<code className="font-mono">.cursor/mcp.json</code>), then restart Cursor:
        </p>
        <CopyBlock text={JSON.stringify(cursor.block, null, 2)} label="Copy config" />
      </Tabs.Content>
      <Tabs.Content value="other" className="mt-2">
        <label className="block text-2xs text-[var(--color-text-muted)] mb-1">
          Client
          <select
            value={other}
            onChange={(e) => {
              const picked = others.find((c) => c.id === e.target.value)?.id;
              if (picked) setOther(picked);
            }}
            className="ml-2 px-2 py-1 text-xs rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
          >
            {others.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <CopyBlock text={JSON.stringify(otherCfg.block, null, 2)} label="Copy config" />
      </Tabs.Content>
    </Tabs.Root>
  );
}

function CopyBlock({ text, label }: { text: string; label: string }) {
  return (
    <div className="relative">
      <pre className="text-2xs font-mono bg-[var(--color-bg)] border border-[var(--color-border)] rounded-lg p-2.5 pr-24 max-h-40 overflow-auto whitespace-pre-wrap break-all">
        {text}
      </pre>
      <CopyButton
        text={text}
        label={label}
        className="absolute top-1.5 right-1.5 inline-flex items-center gap-1 px-2 py-1 text-2xs font-medium bg-[var(--color-surface)] border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-3)]"
      />
    </div>
  );
}
