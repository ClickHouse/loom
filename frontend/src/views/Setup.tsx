import { useEffect, useMemo, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import type { LucideIcon } from "lucide-react";
import {
  Check,
  Settings,
  Plus,
  Trash2,
  FileUp,
  AlertTriangle,
  X as XIcon,
  Code as CodeIcon,
  KeyRound,
  Brain,
} from "lucide-react";
import {
  generateMcpConfig,
  listServiceAccounts,
  createServiceAccount,
  revokeServiceAccount,
  type ServiceAccountEntry,
  importCuratedDoc,
  type ImportCuratedResult,
  registerNamespace,
  type MyNamespaceEntry,
  countMemories,
  isAuthDisabled,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { shouldShowGettingStarted } from "../lib/onboarding";
import { GettingStarted, useOnboarding } from "../components/GettingStarted";
import { Skeleton } from "../components/Skeleton";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { relativeTime, humanizeError } from "../lib/format";
import { useAbortableEffect, useVisiblePoll } from "../lib/hooks";
import { PrivacyFilters, type PrivacyFilterStarter } from "./admin/Blacklist";
import { canCall, canCallCodeRepos } from "../lib/canCall";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { Tip } from "../components/ui";
import { READ_ONLY_TIP, canWrite, roleOnNamespace, useNamespaceRole } from "../lib/namespaceRole";
import { isMemoryNamespace } from "../lib/namespace";
import { hasPlatformAdminClaim } from "../lib/role";

import {
  CLIENTS,
  configPreviewText,
  formatMcpConfig,
  isClientId,
  pluginInstallLines,
  type ClientId,
} from "../lib/clients";
import { agentTokenScope, describeTokenScope } from "../lib/tokenScope";
import { TokenReadScopePanel } from "../components/TokenReadScope";
import { CopyButton } from "../components/CopyButton";
import { invalidateShared, sharedCodeRepos, sharedMine } from "../lib/sharedReads";
import { ModelsSettings } from "./ModelsSettings";

/** The URL carries the open settings tab
 *  (`?tab=memory`). */
const SETTINGS_TABS = ["tokens", "memory", "models"] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number];

function isSettingsTab(v: string | null): v is SettingsTab {
  return SETTINGS_TABS.some((t) => t === v);
}

function readTabParam(): SettingsTab | null {
  if (typeof window === "undefined") return null;
  const v = new URLSearchParams(window.location.search).get("tab");
  return isSettingsTab(v) ? v : null;
}

// The roles a script token may carry on a namespace, lowest first. The role picked per
// namespace is capped at your live grant on it (downgrade allowed, escalation not).
const GRANT_ROLES = ["viewer", "writer", "admin"] as const;
type GrantRole = (typeof GRANT_ROLES)[number];

function isGrantRole(v: string | undefined): v is GrantRole {
  return GRANT_ROLES.some((r) => r === v);
}

/** Your grant on `ns` in the list_mine rows; viewer when the row is missing or its role is
 *  not a grant (a JWT-claim row). */
function grantRoleOf(rows: MyNamespaceEntry[] | null, ns: string): GrantRole {
  const r = rows?.find((x) => x.namespace === ns)?.role;
  return isGrantRole(r) ? r : "viewer";
}

const rolesUpTo = (grant: GrantRole): GrantRole[] => GRANT_ROLES.slice(0, GRANT_ROLES.indexOf(grant) + 1);

/** The Python quick-start for a bot token: the minted token inline, or read from
 *  `LOOM_API_KEY` when none is given (the preview before a mint). */
function botSnippet(m: { org: string; namespace: string; baseUrl: string; token?: string }): string {
  const apiKey =
    m.token === undefined
      ? `os.environ["LOOM_API_KEY"],         # paste your minted token here`
      : `${JSON.stringify(m.token)},     # or set LOOM_API_KEY in env`;
  return `import asyncio${m.token === undefined ? ", os" : ""}
from loom import Agent

async def main():
    async with Agent(
        org=${JSON.stringify(m.org)},
        namespace=${JSON.stringify(m.namespace)},
        agent="my-bot",                           # names this run in audit_log
        base_url=${JSON.stringify(m.baseUrl)},
        api_key=${apiKey}
    ) as a:
        await a.memory.set(
            memory_key="deploy_window",
            value="Tue and Thu only; never Friday.",
            memory_type="semantic",
        )
        hits = await a.memory.search("when can we deploy?", top_k=5)
        for h in hits.results:
            print(h.value)

asyncio.run(main())`;
}

function writeTabParam(tab: SettingsTab) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (tab === "tokens") url.searchParams.delete("tab");
  else url.searchParams.set("tab", tab);
  window.history.replaceState(null, "", url.toString());
}

function SettingsTabTrigger({
  value,
  icon: Icon,
  children,
}: {
  value: SettingsTab;
  icon: LucideIcon;
  children: React.ReactNode;
}) {
  return (
    <Tabs.Trigger
      value={value}
      className="inline-flex items-center gap-1.5 px-3 py-2 -mb-px text-sm border-b-2 border-transparent text-[var(--color-text-muted)] hover:text-[var(--color-text)] data-[state=active]:border-[var(--color-accent)] data-[state=active]:text-[var(--color-text)] data-[state=active]:font-medium outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] rounded-t transition-colors pointer-coarse:min-h-11"
    >
      <Icon size={13} aria-hidden="true" /> {children}
    </Tabs.Trigger>
  );
}

export function Setup() {
  const identity = useIdentity();
  const namespace = identity.namespace ?? "";
  const openNamespacePicker = useStore((s) => s.openNamespacePicker);
  const setView = useStore((s) => s.setView);
  const whoami = useStore((s) => s.whoami);
  const canManageModels = isAuthDisabled() || hasPlatformAdminClaim(whoami);
  const subject = whoami?.subject ?? "";
  // list_mine was fetched once at boot (App.tsx) — reuse it, never re-read.
  const mine = useStore((s) => s.myNamespaces);
  // A code index or a tracker namespace holds no memories, so the memory-plane reads
  // below are skipped there — the checklist and the total are about a memory namespace
  // only.
  const memoryNamespace = isMemoryNamespace(namespace, mine) ? namespace : "";
  const memoriesVersion = useStore((s) => s.memoriesVersion);
  const bumpMemoriesVersion = useStore((s) => s.bumpMemoriesVersion);

  // The tokens list is owned here so the Connect section and the checklist
  // read the same fetch (a list is one request).
  const [sa, setSa] = useState<ServiceAccountEntry[] | null>(null);
  const loadSa = () => {
    listServiceAccounts()
      .then(setSa)
      .catch(() => setSa([]));
  };
  useEffect(loadSa, []);
  const [lastMint, setLastMint] = useState<{ token: string; mcpUrl: string } | null>(null);

  // One `memory.list` with `count_only` for the namespace-wide total
  // (the two cached counts, no page — `limit 1` would read every preview in the
  // namespace) — the checklist's "is this account new" signal and its "wrote a memory"
  // tick. Only when the token's role here is known — an unknown role (no grant, no claim
  // on this namespace) sends nothing and the checklist stays hidden, as it does for a
  // null total.
  const role = useNamespaceRole(namespace);
  const canListMemories = canCall("memory.list", role, whoami);
  const [memoryTotal, setMemoryTotal] = useState<number | null>(null);
  useEffect(() => {
    if (!memoryNamespace || !canListMemories) {
      setMemoryTotal(null);
      return;
    }
    let live = true;
    countMemories(identity)
      .then((n) => {
        if (live) setMemoryTotal(n);
      })
      .catch(() => {
        if (live) setMemoryTotal(null);
      });
    return () => {
      live = false;
    };
  }, [identity, memoryNamespace, memoriesVersion, canListMemories]);

  // A first visit is the four-step checklist. Once shown it stays (persisted `started`)
  // until "Skip the tour" or "Hide", so ticking step two — the first memory — never hides
  // the list mid-tour.
  const onboarding = useOnboarding(subject);
  const isNew = shouldShowGettingStarted({ mine, memoryTotal });
  useEffect(() => {
    if (isNew) onboarding.start();
  }, [isNew, onboarding]);
  const showChecklist = !!memoryNamespace && !onboarding.state.dismissed && (isNew || onboarding.state.started);

  const [tab, setTab] = useState<SettingsTab>(() => readTabParam() ?? "tokens");
  const selectTab = (t: SettingsTab) => {
    setTab(t);
    writeTabParam(t);
  };
  // The token form is folded once a live token exists; `null` follows the list.
  const [formOpen, setFormOpen] = useState<boolean | null>(null);

  // The checklist's "Mint a token": open the Tokens tab and the form, then
  // scroll and focus once they have rendered (an effect, not a timer).
  const [focusRequest, setFocusRequest] = useState(0);
  const focusConnect = () => {
    selectTab("tokens");
    setFormOpen(true);
    setFocusRequest((n) => n + 1);
  };
  useEffect(() => {
    if (!focusRequest) return;
    document.getElementById("setup-connect")?.scrollIntoView({ behavior: "smooth", block: "start" });
    document.getElementById("setup-generate-token")?.focus({ preventScroll: true });
  }, [focusRequest]);

  return (
    <PageContainer>
      <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <Settings size={18} aria-hidden="true" /> Settings
        </h1>
        {tab === "models" ? (
          <span className="text-sm text-[var(--color-text-muted)]">Deployment-wide</span>
        ) : (
          <ScopeBadge kind="namespace" value={namespace} />
        )}
      </div>
      {!namespace && (
        // No saved pick, no own namespace, no usable claim. Say so here — the landing
        // view — instead of inventing a name. The token reaches no namespace of ANY kind
        // (a code/QA-only token lands on Repositories instead), and the way in is a join
        // or a grant. `code.repos` and `tracker.source.list` are org-level reads at
        // viewer role: a known principal may make them, so the button shows once whoami
        // is known.
        <div
          role="status"
          className="mb-6 max-w-2xl flex items-start gap-2 p-3 rounded-lg border border-[var(--color-border-strong)] bg-[var(--color-surface)] text-sm text-[var(--color-text)]"
        >
          <AlertTriangle size={15} className="text-[var(--color-warn)] shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <div className="font-semibold">No namespace selected</div>
            <div className="text-[var(--color-text-muted)] mt-0.5">
              Your token can reach no namespace yet. Join a repository or tracker source from Repositories, or ask for a
              grant.
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-3">
              {whoami && (
                <button
                  type="button"
                  onClick={() => setView("code")}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] transition-colors"
                >
                  <CodeIcon size={13} aria-hidden="true" /> Open Repositories
                </button>
              )}
              <button
                type="button"
                onClick={() => openNamespacePicker(true)}
                className="text-[var(--color-accent)] hover:underline"
              >
                Namespace menu
              </button>
            </div>
          </div>
        </div>
      )}
      {canManageModels && showChecklist && tab !== "models" && (
        <div className="mb-4 rounded-lg border border-[var(--color-border)] p-4 text-sm">
          <p>Set up Loom's LLM and embedding provider before evaluating memory and search quality.</p>
          <button
            type="button"
            className="mt-2 text-[var(--color-accent)] hover:underline"
            onClick={() => selectTab("models")}
          >
            Configure models and keys
          </button>
        </div>
      )}
      {showChecklist && tab !== "models" && (
        <GettingStarted
          onboarding={onboarding}
          identity={identity}
          mine={mine}
          memoryTotal={memoryTotal}
          tokens={sa}
          lastMint={lastMint}
          onConnect={focusConnect}
          onMemoryAdded={bumpMemoriesVersion}
        />
      )}

      <Tabs.Root
        value={tab}
        onValueChange={(v) => {
          if (isSettingsTab(v)) selectTab(v);
        }}
      >
        <Tabs.List
          aria-label="Settings sections"
          className="mb-5 flex items-center gap-1 border-b border-[var(--color-border)]"
        >
          <SettingsTabTrigger value="tokens" icon={KeyRound}>
            Tokens
          </SettingsTabTrigger>
          <SettingsTabTrigger value="memory" icon={Brain}>
            Memory
          </SettingsTabTrigger>
          {canManageModels && (
            <SettingsTabTrigger value="models" icon={Settings}>
              Models &amp; keys
            </SettingsTabTrigger>
          )}
        </Tabs.List>
        <Tabs.Content value="tokens" className="outline-none">
          <TokensSection
            tokens={sa}
            reloadTokens={loadSa}
            onMinted={setLastMint}
            formOpen={formOpen}
            onFormOpenChange={setFormOpen}
          />
        </Tabs.Content>
        <Tabs.Content value="memory" className="outline-none">
          <MemoryTab />
        </Tabs.Content>
        <Tabs.Content value="models" className="outline-none">
          {canManageModels ? <ModelsSettings /> : <p>Model settings require a platform administrator.</p>}
        </Tabs.Content>
      </Tabs.Root>
    </PageContainer>
  );
}

const PRIVACY_STARTERS: PrivacyFilterStarter[] = [
  { pattern: "sk-[A-Za-z0-9]{20,}", label: "API keys" },
  { pattern: "AKIA[0-9A-Z]{16}", label: "AWS key IDs" },
  { pattern: "xoxb-[0-9A-Za-z-]{20,}", label: "Slack bot tokens" },
  { pattern: "\\b\\d{3}-\\d{2}-\\d{4}\\b", label: "US SSNs" },
];

/** The Memory tab — two settings, each one row: what is never saved, and seeding from a
 *  document. */
function MemoryTab() {
  const [importing, setImporting] = useState(false);
  return (
    <div className="max-w-3xl space-y-8">
      <section aria-labelledby="settings-never-save">
        <h2 id="settings-never-save" className="text-sm! font-semibold text-[var(--color-text)]">
          Never save
        </h2>
        <p className="text-xs text-[var(--color-text-muted)] mt-0.5 mb-3 max-w-2xl">
          A write whose value matches a pattern is refused — extracted ones too.
        </p>
        <PrivacyFilters compact starters={PRIVACY_STARTERS} />
      </section>

      <section aria-labelledby="settings-seed" className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-2xl">
          <h2 id="settings-seed" className="text-sm! font-semibold text-[var(--color-text)]">
            Seed from a document
          </h2>
          <p className="text-xs text-[var(--color-text-muted)] mt-0.5">
            Turn an AGENTS.md, CLAUDE.md or rules file into memories — one per{" "}
            <code className="font-mono text-2xs">##</code> section.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setImporting(true)}
          className="inline-flex items-center gap-1.5 min-h-9 px-3 text-sm font-medium rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] hover:bg-[var(--color-surface-2)] pointer-coarse:min-h-11"
        >
          <FileUp size={13} aria-hidden="true" /> Import a document
        </button>
        <Dialog.Root open={importing} onOpenChange={setImporting}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
            <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(720px,92vw)] max-h-[90vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
              <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--color-border)]">
                <Dialog.Title className="text-md font-semibold tracking-tight flex items-center gap-2">
                  <FileUp size={14} aria-hidden="true" /> Import a document
                </Dialog.Title>
                <Dialog.Close
                  className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                  aria-label="Close"
                >
                  <XIcon size={14} />
                </Dialog.Close>
              </div>
              <Dialog.Description className="sr-only">
                Paste a Markdown document; each second-level section becomes one memory.
              </Dialog.Description>
              <div className="p-5">
                <ImportCuratedPanel />
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </section>
    </div>
  );
}

function TokensSection({
  tokens: sa,
  reloadTokens: loadSa,
  onMinted,
  formOpen,
  onFormOpenChange,
}: {
  /** The tokens list, fetched once by Setup and shared with the checklist. */
  tokens: ServiceAccountEntry[] | null;
  reloadTokens: () => void;
  /** An MCP token was minted — the checklist renders its connect line. */
  onMinted: (r: { token: string; mcpUrl: string }) => void;
  /** The form is folded once a live token exists; `null` follows the list. */
  formOpen: boolean | null;
  onFormOpenChange: (open: boolean) => void;
}) {
  const liveTokens = (sa ?? []).filter((t) => !t.revoked_at);
  const formVisible = formOpen ?? (sa !== null && liveTokens.length === 0);
  const identity = useIdentity();
  const [adminNs, setAdminNs] = useState<MyNamespaceEntry[] | null>(null);
  const whoamiForRole = useStore((s) => s.whoami);
  const [tab, setTab] = useState<"agent" | "script">("agent");

  const [name, setName] = useState("");
  const [ttlDays, setTtlDays] = useState<string>("90");
  // selectedNs order = read priority (index 0 = highest). nsRole holds the
  // chosen per-namespace role (defaults to, and is capped at, your grant).
  const [selectedNs, setSelectedNs] = useState<string[]>([]);
  const [nsRole, setNsRole] = useState<Record<string, GrantRole>>({});
  const [newNs, setNewNs] = useState("");
  const [addingNs, setAddingNs] = useState(false);

  const MINT_CLIENT_PLACEHOLDER: ClientId = "claude-desktop";
  // The home namespace follows the page's namespace until the person picks one here.
  const [mcpNamespaceEdited, setMcpNamespaceEdited] = useState<string | null>(null);
  const mcpNamespace = mcpNamespaceEdited ?? identity.namespace;
  // Restricted repositories (by repo name) the person adds to the agent token; sent as
  // their branch namespaces.
  const [mcpRestrictedRepos, setMcpRestrictedRepos] = useState<Set<string>>(() => new Set());
  const [mcpTtlDays, setMcpTtlDays] = useState<string>("90");
  const [mcpResult, setMcpResult] = useState<{ token: string; mcpUrl: string; restricted?: string[] } | null>(null);
  const [dialogClient, setDialogClient] = useState<ClientId>(() => {
    // Blocked storage throws on read; the default client stands in.
    try {
      const stored = localStorage.getItem("loom_last_mcp_client_v1");
      return isClientId(stored) ? stored : "claude-desktop";
    } catch {
      return "claude-desktop";
    }
  });
  const onDialogClientChange = (c: ClientId) => {
    setDialogClient(c);
    try {
      localStorage.setItem("loom_last_mcp_client_v1", c);
    } catch {
      /* quota */
    }
  };
  const [mcpBusy, setMcpBusy] = useState(false);

  // Captured at mint time so the post-mint dialog snippet survives form reset.
  const [mintedToken, setMintedToken] = useState<{
    token: string;
    org: string;
    namespace: string;
    baseUrl: string;
  } | null>(null);

  // Poll while an MCP token minted IN THIS VISIT is waiting for its first
  // call, for at most ten minutes after the mint (so a stale never-used token does not
  // keep every later Setup visit polling at 5 s forever). Sleeps while the tab is hidden.
  const [mintedAtMs, setMintedAtMs] = useState<number | null>(null);
  const waitingForFirstCall =
    mintedAtMs !== null &&
    Date.now() - mintedAtMs < 10 * 60_000 &&
    (sa || []).some((s) => s.kind === "mcp" && !s.last_used_at && !s.revoked_at);
  // Each tick's setSa re-renders, which re-checks the ten-minute window.
  useVisiblePoll(loadSa, 15_000, waitingForFirstCall);
  // Once per identity; no refetch on every window focus — the mint flow alt-tabs
  // constantly and each return paid a list_mine. An answer for an org or namespace the
  // page has left is dropped. The first answer seeds the pick when nothing is picked yet
  // (selectedNs and nsRole are emptied and filled together).
  useAbortableEffect(
    async (_signal, live) => {
      let active: MyNamespaceEntry[];
      try {
        active = (await sharedMine(identity.org)).filter((r) => r.status !== "pending");
      } catch {
        if (live()) setAdminNs([]);
        return;
      }
      if (!live()) return;
      setAdminNs(active);
      const seed = active.find((r) => r.namespace === identity.namespace) ?? active[0];
      if (!seed) return;
      const grant = grantRoleOf(active, seed.namespace);
      setSelectedNs((prev) => (prev.length > 0 ? prev : [seed.namespace]));
      setNsRole((prev) => (Object.keys(prev).length > 0 ? prev : { [seed.namespace]: grant }));
    },
    [identity.org, identity.namespace],
  );

  // Sign up a brand-new robot: register a fresh namespace (the server
  // auto-grants the creator admin at register time), then it's immediately
  // eligible to mint a token for — no operator/CLI step. Token minting stays
  // the existing onCreate below.
  const onCreateNamespace = async () => {
    const ns = newNs.trim();
    if (!ns) return;
    setAddingNs(true);
    try {
      const ownerEmail = whoamiForRole?.subject || identity.namespace;
      await registerNamespace(
        { org: identity.org, namespace: ns },
        { owner_email: ownerEmail, description: "Robot namespace" },
      );
      // The new admin grant must reach every reader of list_mine at once. Most read the
      // store's copy (ScopeBadge, RoleChip, useNamespaceRole, this view); the rest read
      // the shared cache (HeaderSearch, Browse, Sharing). Refresh both.
      invalidateShared("mine:");
      const rows = await sharedMine(identity.org);
      useStore.getState().setMyNamespaces(rows);
      setAdminNs(rows.filter((r) => r.status !== "pending"));
      setSelectedNs([ns]);
      setNsRole({ [ns]: "admin" }); // creator is auto-granted admin
      setNewNs("");
      toast(`Namespace "${ns}" created — you're its admin. Now click Create token.`, "ok");
    } catch (e) {
      toast(`Failed to create namespace: ${humanizeError(e)}`, "error");
    } finally {
      setAddingNs(false);
    }
  };

  // Priority is the order in `selectedNs` (index 0 = highest), set via the per-row number.
  const toggleNs = (ns: string) => {
    setSelectedNs((prev) => {
      if (prev.includes(ns)) {
        setNsRole((m) => {
          const c = { ...m };
          delete c[ns];
          return c;
        });
        return prev.filter((x) => x !== ns);
      }
      setNsRole((m) => ({ ...m, [ns]: grantRoleOf(adminNs, ns) }));
      return [...prev, ns];
    });
  };
  const setPriority = (ns: string, pos: number) => {
    setSelectedNs((prev) => {
      const without = prev.filter((x) => x !== ns);
      const idx = Math.max(0, Math.min(without.length, pos - 1));
      without.splice(idx, 0, ns);
      return without;
    });
  };

  const onCreate = async () => {
    const trimmed = name.trim();
    if (!trimmed || selectedNs.length === 0) return;
    const ttlRaw = ttlDays.trim();
    let ttlValue: number | null = 90;
    if (ttlRaw === "") {
      ttlValue = null;
    } else {
      const n = Number(ttlRaw);
      if (!Number.isInteger(n) || n < 0 || n > 3650) {
        toast("TTL must be 1-3650 days, 0 for never, or blank for 90", "error");
        return;
      }
      ttlValue = n;
    }
    try {
      const created = await createServiceAccount({
        name: trimmed,
        // Ordered = read priority; role per namespace (capped server-side).
        namespaces: selectedNs.map((ns) => ({
          namespace: ns,
          role: eligibleRepos.some((r) => r.namespace === ns) ? "viewer" : (nsRole[ns] ?? grantRoleOf(adminNs, ns)),
        })),
        ttl_days: ttlValue,
      });
      setMintedToken({
        token: created.token,
        org: identity.org,
        namespace: selectedNs[0],
        baseUrl: window.location.origin,
      });
      setName("");
      loadSa();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  const onRevoke = async (saName: string) => {
    if (
      !(await confirmAction({
        title: `Revoke ${saName}?`,
        message:
          "Token stops authenticating immediately. If this is an MCP token for Claude Desktop / Cursor / etc., you'll also want to remove it from the client's config file so the agent stops trying to use it.",
        confirmLabel: "Revoke",
        danger: true,
      }))
    )
      return;
    try {
      await revokeServiceAccount(saName);
      toast("Revoked", "ok");
      loadSa();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  const onGenerateMcp = async () => {
    const ttlRaw = mcpTtlDays.trim();
    let ttlValue: number | null = 90;
    if (ttlRaw === "") {
      ttlValue = null;
    } else {
      const n = Number(ttlRaw);
      if (!Number.isInteger(n) || n < 0 || n > 3650) {
        toast("Expiry must be 1-3650 days, 0 for never, or blank for 90", "error");
        return;
      }
      ttlValue = n;
    }
    setMcpBusy(true);
    try {
      const restricted_namespaces = mcpScope.excludedRestricted
        .filter((r) => mcpRestrictedRepos.has(r.repo))
        .flatMap((r) => r.branches.map((b) => b.namespace));
      const r = await generateMcpConfig(MINT_CLIENT_PLACEHOLDER, {
        namespace: mcpNamespace || undefined,
        ttl_days: ttlValue,
        persist: true,
        restricted_namespaces,
      });
      setMcpResult({
        token: r.token,
        mcpUrl: r.mcp_url,
        restricted: r.restricted_namespaces,
      });
      onMinted({ token: r.token, mcpUrl: r.mcp_url });
      setMintedAtMs(Date.now());
      loadSa();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setMcpBusy(false);
    }
  };

  // Repositories (kind = code) are listed apart from memory namespaces and can only be
  // read — the role is fixed at viewer.
  const eligibleNs = (adminNs ?? []).filter((n) => n.kind !== "code");
  const eligibleRepos = (adminNs ?? []).filter((n) => n.kind === "code");
  // Repository and branch per code namespace, from code.repos; a namespace the list does
  // not know keeps its identifier as the label. `code.repos` only when the caller has
  // standing on a code namespace (an active code-kind row in the list this section
  // already fetched, or the boot copy) or an admin claim.
  const storedMine = useStore((s) => s.myNamespaces);
  const reposReadable = canCallCodeRepos(adminNs ?? storedMine, whoamiForRole);
  // The classification rides along so the read-scope preview can say which repositories a
  // token minted here will never read.
  const [repoInfo, setRepoInfo] = useState<Record<string, { repo: string; branch: string; classification?: string }>>(
    {},
  );
  useAbortableEffect(
    async (_signal, live) => {
      if (!identity.org || !reposReadable) return;
      try {
        const rows = await sharedCodeRepos(identity.org);
        if (!live()) return;
        setRepoInfo(
          Object.fromEntries(
            rows.map((r) => [r.namespace, { repo: r.repo, branch: r.branch, classification: r.classification }]),
          ),
        );
      } catch {
        if (live()) setRepoInfo({});
      }
    },
    [identity.org, reposReadable],
  );
  const repoGroups = useMemo(() => {
    const by = new Map<string, { repo: string; rows: { namespace: string; branch: string }[] }>();
    for (const n of eligibleRepos) {
      const info = repoInfo[n.namespace];
      const repo = info?.repo ?? n.namespace;
      const grp = by.get(repo) ?? { repo, rows: [] };
      grp.rows.push({ namespace: n.namespace, branch: info?.branch ?? n.namespace });
      by.set(repo, grp);
    }
    return Array.from(by.values()).sort((a, b) => a.repo.localeCompare(b.repo));
  }, [eligibleRepos, repoInfo]);
  const noAdminNs = adminNs !== null && eligibleNs.length === 0 && eligibleRepos.length === 0;
  // A viewer cannot mint a token that writes to the picked namespace — say so on the
  // button instead of failing after the click.
  const mcpRole = roleOnNamespace(whoamiForRole, adminNs, mcpNamespace);
  const mcpReadOnly = !canWrite(mcpRole);
  // What the agent token will read and write, listed before the click. The pick is one
  // namespace; the reads are every membership.
  const mcpScope = useMemo(
    () =>
      agentTokenScope(
        adminNs,
        Object.entries(repoInfo).map(([namespace, f]) => ({ namespace, ...f })),
        mcpNamespace,
      ),
    [adminNs, repoInfo, mcpNamespace],
  );

  return (
    <>
      <section className="mb-8" id="setup-connect" aria-label="Tokens">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <p className="text-sm text-[var(--color-text-muted)] max-w-2xl">
            A token is how a client talks to Loom. Your own tools get one that reads everything you belong to; a bot
            gets exactly the namespaces you list.
          </p>
          {!formVisible && (
            <button
              id="setup-generate-token"
              type="button"
              onClick={() => onFormOpenChange(true)}
              className="inline-flex items-center gap-1.5 min-h-9 px-3.5 text-sm font-medium rounded-md bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] shadow-sm pointer-coarse:min-h-11"
            >
              <Plus size={13} aria-hidden="true" /> New token
            </button>
          )}
        </div>

        {formVisible && (
          <div
            className="mb-4 rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4 space-y-4"
            aria-label="New token"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="inline-flex p-0.5 bg-[var(--color-surface-2)] rounded-md gap-0.5">
                {(["agent", "script"] as const).map((t) => (
                  <button
                    key={t}
                    onClick={() => setTab(t)}
                    className={`px-3 py-1.5 text-sm font-medium rounded-[5px] ${
                      tab === t
                        ? "bg-[var(--color-surface)] text-[var(--color-text)] shadow-sm"
                        : "text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                    }`}
                  >
                    {t === "agent" ? "For my own tools" : "For a bot that sees only these"}
                  </button>
                ))}
              </div>
              {liveTokens.length > 0 && (
                <button
                  type="button"
                  onClick={() => onFormOpenChange(false)}
                  className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:underline min-h-9 px-1 pointer-coarse:min-h-11"
                >
                  Cancel
                </button>
              )}
            </div>

            {tab === "agent" ? (
              <div className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Field
                    label="Home namespace"
                    hint="Where this client saves memories and captured sessions. It is not a read limit — see below."
                  >
                    <select
                      value={mcpNamespace}
                      onChange={(e) => setMcpNamespaceEdited(e.target.value)}
                      className="w-full px-3 py-2 text-sm font-mono rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                    >
                      {(eligibleNs.length > 0
                        ? eligibleNs
                        : [{ namespace: identity.namespace, role: "writer" as const }]
                      ).map((n) => (
                        <option key={n.namespace} value={n.namespace}>
                          {n.namespace} · {n.role}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label="Expires in (days)"
                    hint="Blank = 90 days. 0 = never expires (needs an SSO or `loom login` session). Max 3650, less if your own sign-in expires sooner."
                  >
                    <input
                      type="text"
                      inputMode="numeric"
                      value={mcpTtlDays}
                      onChange={(e) => setMcpTtlDays(e.target.value.replace(/[^0-9]/g, ""))}
                      placeholder="90"
                      className="w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                    />
                  </Field>
                </div>
                <TokenReadScopePanel
                  scope={mcpScope}
                  docsHref="https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted"
                  included={mcpRestrictedRepos}
                  onToggleRepo={(r) =>
                    setMcpRestrictedRepos((prev) => {
                      const next = new Set(prev);
                      if (next.has(r.repo)) next.delete(r.repo);
                      else next.add(r.repo);
                      return next;
                    })
                  }
                />
                <div className="flex justify-end">
                  <Tip content={READ_ONLY_TIP} disabled={!mcpReadOnly}>
                    <span
                      tabIndex={mcpReadOnly ? 0 : undefined}
                      className="inline-flex rounded-md outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
                      aria-label={mcpReadOnly ? `Generate token: ${READ_ONLY_TIP}` : undefined}
                    >
                      <button
                        id="setup-generate-token"
                        type="button"
                        onClick={onGenerateMcp}
                        disabled={mcpBusy || mcpReadOnly}
                        className="inline-flex items-center gap-1 px-3 py-2 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        <Plus size={12} aria-hidden="true" /> {mcpBusy ? "Generating…" : "Generate token"}
                      </button>
                    </span>
                  </Tip>
                </div>
              </div>
            ) : noAdminNs ? (
              <div className="text-sm text-[var(--color-text-muted)] text-center py-3">
                No active namespace grants on your account.
              </div>
            ) : (
              <div className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Field label="Name" hint="Short label; can't be changed later.">
                    <input
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      placeholder="e.g. cron-cleanup"
                      className="w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                    />
                  </Field>

                  <Field
                    label="This token reads only these"
                    hint={`${selectedNs.length} selected — and nothing else, whatever you join later. Priority = search order (1 = searched first). Role is capped at your grant. Or create a new namespace below.`}
                  >
                    <div className="max-h-48 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] divide-y divide-[var(--color-border)]">
                      {eligibleNs.map((n) => {
                        const pos = selectedNs.indexOf(n.namespace);
                        const isOn = pos >= 0;
                        const grant = grantRoleOf(adminNs, n.namespace);
                        const selectCls =
                          "px-1 py-0.5 text-2xs rounded border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] disabled:opacity-40";
                        return (
                          <div
                            key={n.namespace}
                            className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-[var(--color-surface)]"
                          >
                            <input
                              type="checkbox"
                              checked={isOn}
                              onChange={() => toggleNs(n.namespace)}
                              className="accent-[var(--color-accent)]"
                              aria-label={`Include ${n.namespace}`}
                            />
                            {/* Priority: search order (1 = first). */}
                            <select
                              value={isOn ? pos + 1 : ""}
                              disabled={!isOn}
                              onChange={(e) => setPriority(n.namespace, Number(e.target.value))}
                              className={`${selectCls} w-12`}
                              aria-label={`Priority for ${n.namespace}`}
                              title="Search priority (1 = searched first)"
                            >
                              {isOn ? (
                                selectedNs.map((_, i) => (
                                  <option key={i} value={i + 1}>
                                    {i + 1}
                                  </option>
                                ))
                              ) : (
                                <option value="">–</option>
                              )}
                            </select>
                            <span className="font-mono flex-1 truncate">{n.namespace}</span>
                            {/* Role: capped at your grant (downgrade only). */}
                            <select
                              value={isOn ? (nsRole[n.namespace] ?? grant) : grant}
                              disabled={!isOn}
                              onChange={(e) => {
                                const r = e.target.value;
                                if (isGrantRole(r)) setNsRole((m) => ({ ...m, [n.namespace]: r }));
                              }}
                              className={`${selectCls} capitalize`}
                              aria-label={`Role for ${n.namespace}`}
                              title={`Capped at your grant (${grant})`}
                            >
                              {rolesUpTo(grant).map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </select>
                          </div>
                        );
                      })}
                    </div>
                    {eligibleRepos.length > 0 && (
                      <div className="mt-3">
                        <div className="text-2xs font-medium text-[var(--color-text-muted)] mb-1">
                          Repositories (read-only)
                        </div>
                        {/* One row per repository, its branches beneath; an
                          "all branches" switch per repository. */}
                        <div className="max-h-56 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] divide-y divide-[var(--color-border)]">
                          {repoGroups.map((grp) => {
                            const onCount = grp.rows.filter((n) => selectedNs.includes(n.namespace)).length;
                            const all = onCount === grp.rows.length;
                            return (
                              <div key={grp.repo}>
                                <div className="flex items-center gap-2 px-3 py-1.5 text-sm bg-[var(--color-surface)]">
                                  <input
                                    type="checkbox"
                                    checked={all}
                                    ref={(el) => {
                                      if (el) el.indeterminate = onCount > 0 && !all;
                                    }}
                                    onChange={() =>
                                      grp.rows.forEach((n) => {
                                        if (selectedNs.includes(n.namespace) === all) toggleNs(n.namespace);
                                      })
                                    }
                                    className="accent-[var(--color-accent)]"
                                    aria-label={`Include all branches of ${grp.repo}`}
                                  />
                                  <span className="font-medium flex-1 truncate">{grp.repo}</span>
                                  <span className="text-2xs text-[var(--color-text-muted)] tabular">
                                    {onCount}/{grp.rows.length} branch{grp.rows.length === 1 ? "" : "es"} · viewer
                                  </span>
                                </div>
                                {grp.rows.map((n) => (
                                  <div
                                    key={n.namespace}
                                    className="flex items-center gap-2 pl-8 pr-3 py-1 text-xs hover:bg-[var(--color-surface)]"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={selectedNs.includes(n.namespace)}
                                      onChange={() => toggleNs(n.namespace)}
                                      className="accent-[var(--color-accent)]"
                                      aria-label={`Include ${grp.repo} branch ${n.branch}`}
                                    />
                                    <span className="font-mono flex-1 truncate" title={n.namespace}>
                                      {n.branch}
                                    </span>
                                    <span className="font-mono text-2xs text-[var(--color-text-subtle)] truncate max-w-[12rem]">
                                      {n.namespace}
                                    </span>
                                  </div>
                                ))}
                              </div>
                            );
                          })}
                        </div>
                        <div className="text-2xs text-[var(--color-text-muted)] mt-1">
                          A token reads a repository only if it is listed here; join more from Repositories.
                        </div>
                      </div>
                    )}
                    <div className="flex gap-2 pt-2">
                      <input
                        type="text"
                        aria-label="Namespace to add to the token"
                        value={newNs}
                        onChange={(e) => setNewNs(e.target.value.replace(/[^a-zA-Z0-9_-]/g, ""))}
                        placeholder="new-robot-namespace"
                        className="flex-1 px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                      />
                      <button
                        onClick={onCreateNamespace}
                        disabled={!newNs.trim() || addingNs}
                        className="inline-flex items-center gap-1 px-3 py-2 text-sm font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface)] disabled:opacity-50 whitespace-nowrap"
                      >
                        <Plus size={12} /> New namespace
                      </button>
                    </div>
                  </Field>

                  <Field
                    label="Expires in (days)"
                    hint="Blank = 90 days. 0 = never expires (needs an SSO or `loom login` session). Max 3650, less if your own sign-in expires sooner."
                  >
                    <input
                      type="text"
                      inputMode="numeric"
                      value={ttlDays}
                      onChange={(e) => setTtlDays(e.target.value.replace(/[^0-9]/g, ""))}
                      placeholder="90"
                      className="w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                    />
                  </Field>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  {/* The one Python example, next to the token it needs. */}
                  <details className="text-xs text-[var(--color-text-muted)] min-w-0 flex-1">
                    <summary className="cursor-pointer hover:text-[var(--color-text)] inline-flex items-center min-h-9 pointer-coarse:min-h-11">
                      Python example for this token
                    </summary>
                    <BotConnectPreview
                      org={identity.org}
                      namespace={selectedNs[0] ?? identity.namespace}
                      baseUrl={typeof window !== "undefined" ? window.location.origin : ""}
                    />
                  </details>
                  <button
                    onClick={onCreate}
                    disabled={!name.trim() || selectedNs.length === 0}
                    className="inline-flex items-center gap-1 px-3 py-2 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
                  >
                    <Plus size={12} /> Create token
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-2">
          <TokensStatusSummary tokens={sa} />
          {sa === null && <Skeleton className="h-12 w-full" />}
          {sa !== null && sa.length === 0 && (
            <p className="text-sm text-[var(--color-text-muted)] px-2.5 py-3">
              No tokens yet. Generate one above; you pick the client (Claude Code, Cursor, …) after it is minted, and
              the same token works in every client.
            </p>
          )}
          {sa !== null && sa.length > 0 && (
            <div className="space-y-1">
              {sa.map((s) => {
                const createdMs = s.created_at ? Date.parse(s.created_at) : NaN;
                const stranded =
                  !s.revoked_at &&
                  !s.last_used_at &&
                  Number.isFinite(createdMs) &&
                  Date.now() - createdMs > 60 * 60 * 1000;
                return (
                  <div
                    key={s.sa_subject || s.name}
                    className="flex items-center gap-3 p-2.5 rounded-lg hover:bg-[var(--color-surface-2)]"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-sm truncate flex items-center gap-2">
                        <KindChip kind={s.kind} client={s.client} />
                        <span className={s.kind === "mcp" ? "" : "font-mono"}>{s.name}</span>
                        <ConnectionBadge token={s} />
                        {s.revoked_at && <span className="text-2xs text-danger">revoked</span>}
                        <SaExpiryBadge expiresAt={s.expires_at} revoked={!!s.revoked_at} />
                      </div>
                      <div className="text-2xs text-[var(--color-text-muted)] tabular">
                        {describeTokenScope(s)} · created {relativeTime(s.created_at)}
                        {" · "}
                        {s.last_used_at ? (
                          <>used {relativeTime(s.last_used_at)}</>
                        ) : (
                          <span className="text-[var(--color-text-subtle)]">no calls in 30 days</span>
                        )}
                      </div>
                      {stranded && (
                        <div className="text-2xs text-warn mt-0.5">
                          Not connecting? Check the client's MCP config + restart it. If still nothing after a minute,
                          revoke this and generate a fresh token.
                        </div>
                      )}
                    </div>
                    {!s.revoked_at && (
                      <button
                        onClick={() => onRevoke(s.name)}
                        className="p-1.5 text-danger hover:bg-danger/15 rounded-md"
                        aria-label={`Revoke ${s.name}`}
                        title="Revoke"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>

      <Dialog.Root
        open={mcpResult !== null}
        onOpenChange={(o) => {
          if (!o) setMcpResult(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(640px,92vw)] max-h-[90vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
            <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--color-border)]">
              <Dialog.Title className="text-md font-semibold tracking-tight flex items-center gap-2">
                <Check size={14} className="text-success" /> Token generated
              </Dialog.Title>
              <Dialog.Close
                className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Close"
              >
                <XIcon size={14} />
              </Dialog.Close>
            </div>
            <div className="p-5 space-y-3">
              <div className="flex items-start gap-2 px-3 py-2 rounded-md bg-warn/15 border border-warn/40 text-xs text-warn">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <div>
                  This is the only time the token is shown. The same token works in every client — switch clients below
                  to copy each one's config block, then paste into each client's MCP config file and restart.
                </div>
              </div>
              <div className="text-xs text-[var(--color-text-muted)]" data-testid="minted-token-scope">
                Writes to <span className="font-mono text-[var(--color-text)]">{mcpScope.home}</span>; reads every
                namespace and repository you belong to, including ones you join later
                {(() => {
                  const added = mcpScope.excludedRestricted
                    .filter((r) => mcpRestrictedRepos.has(r.repo))
                    .map((r) => r.repo);
                  const left = mcpScope.excludedRestricted
                    .filter((r) => !mcpRestrictedRepos.has(r.repo))
                    .map((r) => r.repo);
                  // The server adds a restricted repository's bound QA record; anything
                  // granted beyond the ticked branches is that.
                  const ticked = new Set(
                    mcpScope.excludedRestricted
                      .filter((r) => mcpRestrictedRepos.has(r.repo))
                      .flatMap((r) => r.branches.map((b) => b.namespace)),
                  );
                  const extra = (mcpResult?.restricted ?? []).filter((ns) => !ticked.has(ns));
                  return (
                    <>
                      {added.length > 0 ? <>, plus the restricted {added.join(", ")} with a grant of its own</> : null}
                      {extra.length > 0 ? (
                        <>
                          {" "}
                          and {extra.length === 1 ? "its tracker" : "their trackers"} {extra.join(", ")}
                        </>
                      ) : null}
                      {left.length > 0 ? <> — except restricted repositories ({left.join(", ")})</> : null}
                    </>
                  );
                })()}.
              </div>
              <Field
                label="MCP client"
                hint="The token works in every client — pick yours to copy the matching config block. Your last choice is remembered next time."
              >
                <select
                  value={dialogClient}
                  onChange={(e) => {
                    const c = e.target.value;
                    if (isClientId(c)) onDialogClientChange(c);
                  }}
                  className="w-full px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
                >
                  {CLIENTS.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </Field>
              {dialogClient === "claude-code" &&
                mcpResult &&
                (() => {
                  const base = mcpResult.mcpUrl.replace(/\/mcp$/, "");
                  const configJson = JSON.stringify({ url: base, token: mcpResult.token }, null, 2);
                  const plugin = pluginInstallLines("claude-code") || "";
                  return (
                    <div className="space-y-2" data-testid="claude-code-recommended">
                      <div className="text-xs font-medium text-[var(--color-text)]">
                        Recommended: the plugin (tools, memory primer, capture)
                      </div>
                      <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
                        1. Save the token where the plugin, the CLI and the SDK all read it —{" "}
                        <code>~/.loom/config.json</code> (chmod 600):
                      </div>
                      <div className="relative">
                        <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                          {configJson}
                        </pre>
                        <CopyButton text={configJson} label="Copy" ariaLabel="Copy config.json" />
                      </div>
                      <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
                        2. Install the plugin; it mounts the Loom tools itself and reads that file. Leave the token
                        prompt empty.
                      </div>
                      <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                        {plugin}
                      </pre>
                      <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed pt-1 border-t border-[var(--color-border)]">
                        <span className="font-medium text-[var(--color-text)]">Tools only, no hooks:</span> register the
                        server below instead. Pick one — a user-scope <code>loom</code> entry outranks the plugin's own
                        server.
                      </div>
                    </div>
                  );
                })()}
              <div className="relative">
                <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 max-h-72 overflow-auto whitespace-pre-wrap">
                  {mcpResult
                    ? configPreviewText(
                        formatMcpConfig({
                          client: dialogClient,
                          token: mcpResult.token,
                          mcpUrl: mcpResult.mcpUrl,
                        }),
                      )
                    : ""}
                </pre>
                <CopyButton
                  label="Copy config"
                  text={() =>
                    mcpResult
                      ? configPreviewText(
                          formatMcpConfig({ client: dialogClient, token: mcpResult.token, mcpUrl: mcpResult.mcpUrl }),
                        )
                      : ""
                  }
                />
              </div>
              {mcpResult &&
                (() => {
                  const cfg = formatMcpConfig({
                    client: dialogClient,
                    token: mcpResult.token,
                    mcpUrl: mcpResult.mcpUrl,
                  });
                  if (!cfg.command) return null;
                  const lines = cfg.envLines ? `${cfg.command}\n${cfg.envLines}` : cfg.command;
                  return (
                    <div className="space-y-2">
                      <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
                        Or register it from a terminal (the client writes its own config file):
                      </div>
                      <div className="relative">
                        <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                          {lines}
                        </pre>
                        <CopyButton text={lines} label="Copy command" />
                      </div>
                    </div>
                  );
                })()}
              {dialogClient === "codex" &&
                mcpResult &&
                (() => {
                  const plugin = pluginInstallLines("codex") || "";
                  return (
                    <div className="space-y-2 pt-3 border-t border-[var(--color-border)]">
                      <div className="text-xs font-medium text-[var(--color-text)]">Automatic capture and recall</div>
                      <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
                        The entry above gives Codex the Loom <em>tools</em>. To also load a memory primer at session
                        start and record decisions as you work, install the capture plugin; its hooks read the token
                        from <code>LOOM_API_KEY</code> or <code>~/.loom/config.json</code>.
                      </div>
                      <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                        {plugin}
                      </pre>
                    </div>
                  );
                })()}
              <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
                Token lives in the list below — revoke it to disconnect every client at once.
              </div>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root
        open={mintedToken !== null}
        onOpenChange={(o) => {
          if (!o) setMintedToken(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(680px,92vw)] max-h-[90vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
            <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--color-border)]">
              <Dialog.Title className="text-md font-semibold tracking-tight flex items-center gap-2">
                <Check size={14} className="text-success" /> Token created
              </Dialog.Title>
              <Dialog.Close
                className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Close"
              >
                <XIcon size={14} />
              </Dialog.Close>
            </div>
            <div className="p-5 space-y-3">
              <div className="flex items-start gap-2 px-3 py-2 rounded-md bg-warn/15 border border-warn/40 text-xs text-warn">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <div>
                  This is the only time the token is shown. Copy it now and store it somewhere safe (a secrets manager,
                  your CI's secret store). Lost tokens can't be recovered — only re-issued.
                </div>
              </div>
              <div className="relative">
                <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5">Token</div>
                <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 max-h-32 overflow-auto break-all whitespace-pre-wrap">
                  {mintedToken?.token ?? ""}
                </pre>
                <CopyButton text={mintedToken?.token ?? ""} label="Copy token" className={COPY_BELOW_CAPTION} />
              </div>

              {mintedToken && (
                <div className="relative">
                  <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5">
                    Connect your bot — Python
                  </div>
                  <pre className="text-2xs font-mono bg-[var(--color-surface-2)] rounded-lg p-3 max-h-72 overflow-auto whitespace-pre">
                    {botSnippet(mintedToken)}
                  </pre>
                  <CopyButton text={botSnippet(mintedToken)} label="Copy code" className={COPY_BELOW_CAPTION} />
                  <div className="mt-1.5 text-2xs text-[var(--color-text-muted)] leading-relaxed">
                    Install with{" "}
                    <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 rounded">
                      pip install clickhouse-loom
                    </code>
                    . The same token also works on raw HTTP:{" "}
                    <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 rounded">
                      curl -H "Authorization: Bearer $LOOM_API_KEY" {mintedToken.baseUrl}/v1/memory.search
                    </code>
                  </div>
                </div>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}

function TokensStatusSummary({ tokens }: { tokens: ServiceAccountEntry[] | null }) {
  if (!tokens || tokens.length === 0) return null;
  const live = tokens.filter((t) => !t.revoked_at);
  if (live.length === 0) return null;

  const now = Date.now();
  const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000;
  const FOURTEEN_DAYS = 14 * 24 * 60 * 60 * 1000;
  let active = 0;
  let waiting = 0;
  let idle = 0;
  let expiringSoon = 0;
  for (const t of live) {
    if (t.expires_at) {
      const exp = Date.parse(t.expires_at);
      if (Number.isFinite(exp) && exp > now && exp - now <= FOURTEEN_DAYS) {
        expiringSoon += 1;
      }
    }
    if (!t.last_used_at) {
      waiting += 1;
      continue;
    }
    const usedAt = Date.parse(t.last_used_at);
    if (!Number.isFinite(usedAt)) {
      idle += 1;
      continue;
    }
    if (now - usedAt <= SEVEN_DAYS) active += 1;
    else idle += 1;
  }

  return (
    <div className="px-2.5 pt-1.5 pb-2 space-y-1.5">
      <div className="text-xs text-[var(--color-text-muted)]">
        <span className="text-[var(--color-text)] font-medium">{active}</span> active
        {" · "}
        <span className="text-[var(--color-text)] font-medium">{waiting}</span> waiting for first use
        {" · "}
        <span className="text-[var(--color-text)] font-medium">{idle}</span> idle (&gt;7d)
      </div>
      {expiringSoon > 0 && (
        <div className="inline-flex items-center gap-2 text-xs px-2.5 py-1 rounded-md bg-warn/15 border border-warn/40 text-warn">
          <AlertTriangle size={12} />
          <span>
            <span className="font-medium">{expiringSoon}</span> token{expiringSoon === 1 ? "" : "s"} expir
            {expiringSoon === 1 ? "es" : "e"} in the next 14 days — generate a replacement now to avoid downtime.
          </span>
        </div>
      )}
    </div>
  );
}

function SaExpiryBadge({ expiresAt, revoked }: { expiresAt: string | null; revoked: boolean }) {
  if (revoked) return null;
  if (!expiresAt) {
    return <span className="ml-2 text-2xs text-[var(--color-text-subtle)]">never expires</span>;
  }
  const expiryMs = new Date(expiresAt).getTime();
  const now = Date.now();
  const FOURTEEN_DAYS = 14 * 24 * 60 * 60 * 1000;
  const expired = expiryMs < now;
  const expiringSoon = !expired && expiryMs - now <= FOURTEEN_DAYS;
  const tone = expired ? "text-danger" : expiringSoon ? "text-warn" : "text-[var(--color-text-muted)]";
  return <span className={`ml-2 text-2xs ${tone}`}>{expired ? "expired" : `expires ${relativeTime(expiresAt)}`}</span>;
}

// The copy button of a boxed block with a caption above it.
const COPY_BELOW_CAPTION =
  "absolute top-6 right-2 inline-flex items-center gap-1 px-2 py-1 text-2xs font-medium bg-[var(--color-surface)] border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-3)]";

function BotConnectPreview({ org, namespace, baseUrl }: { org: string; namespace: string; baseUrl: string }) {
  const code = botSnippet({ org, namespace, baseUrl });
  return (
    <div className="mt-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] p-3">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="text-2xs text-[var(--color-text-muted)] leading-relaxed">
          Export the minted token as <code className="font-mono">LOOM_API_KEY</code>; the namespace follows the first
          one you tick.
        </div>
        <CopyButton
          text={code}
          label="Copy code"
          className="shrink-0 inline-flex items-center gap-1 px-2 py-1 text-2xs font-medium bg-[var(--color-surface)] border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-3)]"
        />
      </div>
      <pre className="text-2xs font-mono bg-[var(--color-bg)] rounded-lg p-3 max-h-72 overflow-auto whitespace-pre border border-[var(--color-border)]">
        {code}
      </pre>
      <div className="mt-2 text-2xs text-[var(--color-text-muted)] leading-relaxed">
        Install with{" "}
        <code className="font-mono text-2xs bg-[var(--color-surface)] px-1 rounded">pip install clickhouse-loom</code>.
        Same JWT also works on raw HTTP — set{" "}
        <code className="font-mono text-2xs bg-[var(--color-surface)] px-1 rounded">
          Authorization: Bearer $LOOM_API_KEY
        </code>
        .
      </div>
    </div>
  );
}

function ConnectionBadge({ token }: { token: ServiceAccountEntry }) {
  if (token.revoked_at) return null;
  if (!token.last_used_at) {
    return (
      <span className="inline-flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded bg-warn/15 text-warn font-medium">
        <span className="w-1.5 h-1.5 rounded-full bg-warn animate-pulse motion-reduce:animate-none!" />
        Waiting…
      </span>
    );
  }
  const ageMs = Date.now() - new Date(token.last_used_at).getTime();
  if (ageMs < 30 * 24 * 3600 * 1000) {
    return (
      <span className="inline-flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded bg-success/15 text-success font-medium">
        <span className="w-1.5 h-1.5 rounded-full bg-success" />
        Connected
      </span>
    );
  }
  return null;
}

function KindChip({ kind, client }: { kind?: string; client?: string }) {
  if (kind === "mcp") {
    const label = CLIENTS.find((c) => c.id === client)?.label || "MCP";
    return (
      <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--color-accent-soft)] text-[var(--color-accent)] font-medium uppercase tracking-wider">
        {label}
      </span>
    );
  }
  return (
    <span className="text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)] font-medium uppercase tracking-wider">
      Script
    </span>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-2xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
      {children}
      {hint && <div className="text-2xs text-[var(--color-text-subtle)] mt-1">{hint}</div>}
    </label>
  );
}

function ImportCuratedPanel() {
  const identity = useIdentity();
  const [content, setContent] = useState("");
  const [sourceUri, setSourceUri] = useState("AGENTS.md");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ImportCuratedResult | null>(null);

  const run = async () => {
    if (!content.trim()) {
      toast("Paste a doc body first", "info");
      return;
    }
    setSubmitting(true);
    setResult(null);
    try {
      const r = await importCuratedDoc(
        { org: identity.org, namespace: identity.namespace },
        { content, source_uri: sourceUri.trim() || "curated_doc" },
      );
      setResult(r);
      const tone = r.rejected > 0 ? "error" : r.written > 0 ? "ok" : "info";
      toast(`Import: ${r.written} written · ${r.merged} merged · ${r.rejected} rejected · ${r.skipped} skipped`, tone);
    } catch (e) {
      toast(`Import failed: ${humanizeError(e)}`, "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 space-y-3">
      <div className="text-xs text-[var(--color-text-muted)] leading-relaxed">
        Paste a doc that represents reviewed knowledge — your AGENTS.md, CLAUDE.md, .cursor/rules, or any
        operating-principles doc. Each <code className="font-mono text-2xs">##</code> section becomes one memory in{" "}
        <code className="font-mono text-2xs">{identity.namespace}</code>. The import bypasses the review queue but still
        respects pinned memories: if a section contradicts a pinned memory it's rejected per-row.
      </div>
      <div className="grid grid-cols-[1fr_auto] gap-2">
        <input
          aria-label="Source URI for the imported document"
          value={sourceUri}
          onChange={(e) => setSourceUri(e.target.value)}
          placeholder="source_uri (e.g. AGENTS.md)"
          className="px-3 py-1.5 text-sm font-mono bg-[var(--color-bg)] border border-[var(--color-border)] rounded-md"
        />
        <button
          onClick={run}
          disabled={submitting}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md disabled:opacity-50"
        >
          <FileUp size={12} /> {submitting ? "Importing…" : "Import"}
        </button>
      </div>
      <textarea
        aria-label="Markdown to import"
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder={`# My Operating Principles\n\n## Backup discipline\n\nI run nightly backups via Borg.\n\n## Coffee preference\n\nBlack, no sugar.`}
        className="w-full h-48 px-3 py-2 text-xs font-mono bg-[var(--color-bg)] border border-[var(--color-border)] rounded-md resize-y"
      />
      {result && (
        <div className="border-t border-[var(--color-border)] pt-3">
          <div className="text-2xs uppercase tracking-wider text-[var(--color-text-muted)] mb-2">
            Result · {result.rows.length} section{result.rows.length === 1 ? "" : "s"}
          </div>
          <div className="space-y-1.5 max-h-56 overflow-y-auto">
            {result.rows.map((r, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span
                  className={`inline-block w-16 text-2xs font-mono px-1.5 py-0.5 rounded ${
                    r.verdict === "written"
                      ? "bg-[var(--color-success)]/20 text-[var(--color-success)]"
                      : r.verdict === "merged" || r.verdict === "duplicate"
                        ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
                        : r.verdict === "rejected_pinned"
                          ? "bg-[var(--color-danger)]/20 text-[var(--color-danger)]"
                          : "bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
                  }`}
                >
                  {r.verdict}
                </span>
                <span className="font-mono text-2xs truncate">{r.memory_key}</span>
                <span className="text-[var(--color-text-muted)] text-2xs truncate flex-1">{r.section_heading}</span>
                {r.rejection_reason && (
                  <span className="text-[var(--color-warn)] text-2xs truncate" title={r.rejection_reason}>
                    {r.rejection_reason}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
