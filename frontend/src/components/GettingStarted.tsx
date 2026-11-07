import { useCallback, useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { ProgressBar } from "@clickhouse/click-ui/ProgressBar";
import { Select } from "@clickhouse/click-ui/Select";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import type { Identity, MyNamespaceEntry, TokenEntry } from "../api";
import { useStore } from "../lib/state";
import { paletteShortcut } from "../lib/platform";
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
import { ExternalLink, Kbd, Panel, TabTrigger } from "./ui";
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
  tokens: TokenEntry[] | null;
  /** The most recent MCP token minted on this page, for the connect line. */
  lastMint: { token: string; mcpUrl: string } | null;
  /** Scroll to and focus the token form. */
  onConnect: () => void;
  onMemoryAdded: () => void;
}

const SAMPLE_QUERY = "deploy window";

const STEP_META: Record<OnboardingStep, { title: string; why: string; icon: IconName }> = {
  connect: {
    title: "Connect a client",
    why: "Your AI tool talks to Loom through a token — mint one, paste one line into the tool, restart it.",
    icon: "plug",
  },
  write: {
    title: "Write your first memory",
    why: "A memory is one fact Loom keeps for you — a preference, a decision, a deadline your tools should not forget.",
    icon: "pencil",
  },
  find: {
    title: "Find it again",
    why: "Search is how your tools get memories back; try it yourself so you know what they see.",
    icon: "search",
  },
  invite: {
    title: "Invite a teammate",
    why: "Your space is private until you add someone — a viewer reads it, a writer adds to it.",
    icon: "users",
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
          <h2 id="getting-started-title">Getting started</h2>
          <p className="text-xs text-text-muted mt-0.5 max-w-xl">
            Loom remembers for your AI tools. Everything you save lands in your space (a <em>namespace</em>, in the
            docs)
            {spaceName ? (
              <>
                {" "}
                — <code className="code-inline text-xs">{spaceName}</code>
              </>
            ) : null}
            . Four steps show the whole loop; about five minutes.
          </p>
        </div>
        <Button type="empty" htmlType="button" onClick={dismiss} className="shrink-0">
          Skip the tour
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
          className="flex-1"
        />
        <span className="text-xs tabular text-text-muted">
          {n} of {total} done
        </span>
      </div>

      <ol className="space-y-2">
        {ONBOARDING_STEPS.map((step, i) => {
          const meta = STEP_META[step];
          const act = actions[step];
          const isDone = !!done[step];
          return (
            <li
              key={step}
              className={`rounded-sm border p-3 ${isDone ? "border-border bg-surface" : "border-border-strong bg-surface-2"}`}
            >
              <div className="flex items-start gap-3">
                <span className="mt-0.5 shrink-0" aria-hidden="true">
                  {isDone ? (
                    <Icon name="check-in-circle" size="md" color="var(--color-success)" />
                  ) : (
                    <Icon name="circle" size="md" color="var(--color-text-muted)" />
                  )}
                </span>
                <div className="flex-1 min-w-0">
                  <h3 className="flex items-center gap-2">
                    <span className="text-text-muted tabular">{i + 1}.</span>
                    <Icon name={meta.icon} size="xs" aria-hidden="true" />
                    <span>{meta.title}</span>
                    {isDone && <span className="sr-only">(done)</span>}
                  </h3>
                  <p className="text-xs text-text-muted mt-0.5">{meta.why}</p>
                  {step === "find" && !isDone && (
                    <p className="text-xs text-text-muted mt-1">
                      Or press <Kbd>{paletteShortcut()}</Kbd> anywhere and type a few words.
                    </p>
                  )}
                  {step === "connect" && lastMint && <ConnectLine mint={lastMint} />}
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <Button type={isDone ? "secondary" : "primary"} htmlType="button" onClick={act.run}>
                      {act.label}
                    </Button>
                    {act.docs && (
                      <ExternalLink href={act.docs.href} size="sm">
                        {act.docs.label}
                      </ExternalLink>
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
          className="mt-3 pt-3 border-t border-border flex flex-wrap items-center justify-between gap-2 text-xs"
        >
          <span className="text-success font-medium">All four done — Loom is remembering for you.</span>
          <Button type="secondary" htmlType="button" onClick={dismiss}>
            Hide this checklist
          </Button>
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
    <div className="mt-2">
      <Tabs defaultValue="claude-code">
        <Tabs.TriggersList aria-label="Client">
          <TabTrigger value="claude-code">Claude Code</TabTrigger>
          <TabTrigger value="cursor">Cursor</TabTrigger>
          <TabTrigger value="other">Other clients</TabTrigger>
        </Tabs.TriggersList>
        <Tabs.Content value="claude-code" className="mt-2">
          <p className="text-xs text-text-muted mb-1">
            Run this once in a terminal, then start a new Claude Code session — Loom&apos;s profile and context arrive
            when the client connects. For capture and hooks, install the plugin from Settings instead:
          </p>
          <CopyBlock text={claude.command ?? ""} label="Copy command" />
        </Tabs.Content>
        <Tabs.Content value="cursor" className="mt-2">
          <p className="text-xs text-text-muted mb-1">
            Paste into Cursor's MCP settings (<code className="code-inline">.cursor/mcp.json</code>), then restart
            Cursor:
          </p>
          <CopyBlock text={JSON.stringify(cursor.block, null, 2)} label="Copy config" />
        </Tabs.Content>
        <Tabs.Content value="other" className="mt-2 space-y-2">
          <Select
            label="Client"
            value={other}
            onSelect={(v) => {
              const picked = others.find((c) => c.id === v)?.id;
              if (picked) setOther(picked);
            }}
          >
            {others.map((c) => (
              <Select.Item key={c.id} value={c.id}>
                {c.label}
              </Select.Item>
            ))}
          </Select>
          <CopyBlock text={JSON.stringify(otherCfg.block, null, 2)} label="Copy config" />
        </Tabs.Content>
      </Tabs>
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
