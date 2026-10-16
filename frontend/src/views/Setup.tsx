import { useEffect, useMemo, useRef, useState } from "react";
import { Alert } from "@clickhouse/click-ui/Alert";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
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
import { TabTrigger, Tip, Unavailable } from "../components/ui";
import { Modal } from "../components/ui/Modal";
import { IconButton } from "../components/IconButton";
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
        <h1 className="text-2xl inline-flex items-center gap-2">
          <Icon name="gear" size="md" aria-hidden="true" /> Settings
        </h1>
        {tab === "models" ? (
          <span className="text-sm text-text-muted">Deployment-wide</span>
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
        <div role="status" className="mb-6 max-w-2xl">
          <Alert
            state="warning"
            size="medium"
            text={
              <>
                <strong>No namespace selected.</strong> Your token can reach no namespace yet. Join a repository or
                tracker source from Repositories, or ask for a grant.
                <span className="mt-2 flex flex-wrap items-center gap-3">
                  <Button type="empty" htmlType="button" onClick={() => openNamespacePicker(true)}>
                    Namespace menu
                  </Button>
                  {whoami && (
                    <Button type="primary" htmlType="button" iconLeft="code" onClick={() => setView("code")}>
                      Open repositories
                    </Button>
                  )}
                </span>
              </>
            }
          />
        </div>
      )}
      {canManageModels && showChecklist && tab !== "models" && (
        <Alert
          className="mb-4"
          state="info"
          size="medium"
          text={
            <>
              Set up Loom's LLM and embedding provider before evaluating memory and search quality.
              <span className="mt-2 flex">
                <Button type="empty" htmlType="button" onClick={() => selectTab("models")}>
                  Configure models and keys
                </Button>
              </span>
            </>
          }
        />
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

      <Tabs
        value={tab}
        onValueChange={(v) => {
          if (isSettingsTab(v)) selectTab(v);
        }}
      >
        <Tabs.TriggersList aria-label="Settings sections" className="mb-5">
          <TabTrigger value="tokens" icon="key">
            Tokens
          </TabTrigger>
          <TabTrigger value="memory" icon="light-bulb">
            Memory
          </TabTrigger>
          {canManageModels && (
            <TabTrigger value="models" icon="gear">
              Models &amp; keys
            </TabTrigger>
          )}
        </Tabs.TriggersList>
        <Tabs.Content value="tokens">
          <TokensSection
            tokens={sa}
            reloadTokens={loadSa}
            onMinted={setLastMint}
            formOpen={formOpen}
            onFormOpenChange={setFormOpen}
          />
        </Tabs.Content>
        <Tabs.Content value="memory">
          <MemoryTab />
        </Tabs.Content>
        <Tabs.Content value="models">
          {canManageModels ? <ModelsSettings /> : <p>Model settings require a platform administrator.</p>}
        </Tabs.Content>
      </Tabs>
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
        <h2 id="settings-never-save" className="text-text">
          Never save
        </h2>
        <p className="text-xs text-text-muted mt-0.5 mb-3 max-w-2xl">
          A write whose value matches a pattern is refused — extracted ones too.
        </p>
        <PrivacyFilters compact starters={PRIVACY_STARTERS} />
      </section>

      <section aria-labelledby="settings-seed" className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-2xl">
          <h2 id="settings-seed" className="text-text">
            Seed from a document
          </h2>
          <p className="text-xs text-text-muted mt-0.5">
            Turn an AGENTS.md, CLAUDE.md or rules file into memories — one per{" "}
            <code className="font-mono text-xs">##</code> section.
          </p>
        </div>
        <Button type="secondary" htmlType="button" iconLeft="upload" onClick={() => setImporting(true)}>
          Import a document
        </Button>
        <Modal
          open={importing}
          onOpenChange={setImporting}
          title="Import a document"
          description="Paste a Markdown document; each second-level section becomes one memory."
          className="max-w-[720px] w-full"
        >
          <ImportCuratedPanel />
        </Modal>
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
  // "New token" and the form's Cancel each remove themselves when pressed, so focus is
  // handed on once the other side renders: into the form's first control, or back to
  // "New token". Only a press sets it, so the form shown on load takes no focus.
  const focusAfterToggle = useRef<"form" | "trigger" | null>(null);
  const tokenFormRef = useRef<HTMLElement>(null);
  const newTokenRef = useRef<HTMLButtonElement>(null);
  const toggleForm = (open: boolean) => {
    focusAfterToggle.current = open ? "form" : "trigger";
    onFormOpenChange(open);
  };
  useEffect(() => {
    const target = focusAfterToggle.current;
    focusAfterToggle.current = null;
    if (target === "form") tokenFormRef.current?.querySelector<HTMLElement>("button, input, select, textarea")?.focus();
    if (target === "trigger") newTokenRef.current?.focus();
  }, [formVisible]);
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
      if (!Number.isInteger(n) || n < 1 || n > 3650) {
        toast("TTL must be 1-3650 days or blank for 90", "error");
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
      if (!Number.isInteger(n) || n < 1 || n > 3650) {
        toast("Expiry must be 1-3650 days or blank for 90", "error");
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
          <p className="text-sm text-text-muted max-w-2xl">
            A token is how a client talks to Loom. Your own tools get one that reads everything you belong to; a bot
            gets exactly the namespaces you list.
          </p>
          {!formVisible && (
            <Button
              ref={newTokenRef}
              id="setup-generate-token"
              type="primary"
              htmlType="button"
              iconLeft="plus"
              onClick={() => toggleForm(true)}
            >
              New token
            </Button>
          )}
        </div>

        {formVisible && (
          <section
            ref={tokenFormRef}
            className="mb-4 rounded-2xl border border-border bg-surface p-4 space-y-4"
            aria-label="New token"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <ButtonGroup
                aria-label="Token kind"
                options={[
                  { value: "agent", label: "For my own tools" },
                  { value: "script", label: "For a bot that sees only these" },
                ]}
                selected={tab}
                onClick={(value) => setTab(value === "script" ? "script" : "agent")}
              />
              {liveTokens.length > 0 && (
                <Button type="empty" htmlType="button" onClick={() => toggleForm(false)}>
                  Cancel
                </Button>
              )}
            </div>

            {tab === "agent" ? (
              <div className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Field hint="Where this client saves memories and captured sessions. It is not a read limit — see below.">
                    <Select label="Home namespace" value={mcpNamespace} onSelect={setMcpNamespaceEdited}>
                      {(eligibleNs.length > 0
                        ? eligibleNs
                        : [{ namespace: identity.namespace, role: "writer" as const }]
                      ).map((n) => (
                        <Select.Item key={n.namespace} value={n.namespace}>
                          {n.namespace} · {n.role}
                        </Select.Item>
                      ))}
                    </Select>
                  </Field>
                  <Field hint="Blank = 90 days. Max 3650.">
                    <TextField
                      label="Expires in (days)"
                      inputMode="numeric"
                      value={mcpTtlDays}
                      onChange={(value) => setMcpTtlDays(value.replace(/[^0-9]/g, ""))}
                      placeholder="90"
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
                  <Unavailable reason={mcpReadOnly ? READ_ONLY_TIP : null}>
                    <Button
                      id="setup-generate-token"
                      type="primary"
                      htmlType="button"
                      iconLeft="plus"
                      loading={mcpBusy}
                      onClick={onGenerateMcp}
                    >
                      Generate token
                    </Button>
                  </Unavailable>
                </div>
              </div>
            ) : noAdminNs ? (
              <div className="text-sm text-text-muted text-center py-3">
                No active namespace grants on your account.
              </div>
            ) : (
              <div className="space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Field hint="Short label; can't be changed later.">
                    <TextField label="Name" value={name} onChange={setName} placeholder="e.g. cron-cleanup" />
                  </Field>

                  <fieldset>
                    <legend className="text-xs font-medium text-text-muted mb-1">This token reads only these</legend>
                    <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-surface-2 divide-y divide-border">
                      {eligibleNs.map((n) => {
                        const pos = selectedNs.indexOf(n.namespace);
                        const isOn = pos >= 0;
                        const grant = grantRoleOf(adminNs, n.namespace);
                        return (
                          <div key={n.namespace} className="flex items-center gap-2 px-3 py-1.5 text-sm">
                            <Checkbox
                              checked={isOn}
                              onCheckedChange={() => toggleNs(n.namespace)}
                              aria-label={`Include ${n.namespace}`}
                            />
                            {/* Priority: search order (1 = first). */}
                            <Tip content="Search priority (1 = searched first)">
                              <div className="w-16 shrink-0">
                                <Select
                                  triggerProps={{ "aria-label": `Priority for ${n.namespace}` }}
                                  value={isOn ? String(pos + 1) : ""}
                                  placeholder="–"
                                  disabled={!isOn}
                                  onSelect={(value) => setPriority(n.namespace, Number(value))}
                                >
                                  {selectedNs.map((_, i) => (
                                    <Select.Item key={i} value={String(i + 1)}>
                                      {i + 1}
                                    </Select.Item>
                                  ))}
                                </Select>
                              </div>
                            </Tip>
                            <span className="font-mono flex-1 truncate">{n.namespace}</span>
                            {/* Role: capped at your grant (downgrade only). */}
                            <Tip content={`Capped at your grant (${grant})`}>
                              <div className="w-28 shrink-0">
                                <Select
                                  triggerProps={{ "aria-label": `Role for ${n.namespace}` }}
                                  value={isOn ? (nsRole[n.namespace] ?? grant) : grant}
                                  disabled={!isOn}
                                  onSelect={(r) => {
                                    if (isGrantRole(r)) setNsRole((m) => ({ ...m, [n.namespace]: r }));
                                  }}
                                >
                                  {rolesUpTo(grant).map((r) => (
                                    <Select.Item key={r} value={r}>
                                      {ROLE_LABELS[r]}
                                    </Select.Item>
                                  ))}
                                </Select>
                              </div>
                            </Tip>
                          </div>
                        );
                      })}
                    </div>
                    {eligibleRepos.length > 0 && (
                      <div className="mt-3">
                        <div className="text-xs font-medium text-text-muted mb-1">Repositories (read-only)</div>
                        {/* One row per repository, its branches beneath; an
                          "all branches" switch per repository. */}
                        <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-surface-2 divide-y divide-border">
                          {repoGroups.map((grp) => {
                            const onCount = grp.rows.filter((n) => selectedNs.includes(n.namespace)).length;
                            const all = onCount === grp.rows.length;
                            return (
                              <div key={grp.repo}>
                                <div className="flex items-center gap-2 px-3 py-1.5 text-sm bg-surface">
                                  <Checkbox
                                    checked={all ? true : onCount > 0 ? "indeterminate" : false}
                                    onCheckedChange={() =>
                                      grp.rows.forEach((n) => {
                                        if (selectedNs.includes(n.namespace) === all) toggleNs(n.namespace);
                                      })
                                    }
                                    aria-label={`Include all branches of ${grp.repo}`}
                                  />
                                  <span className="font-medium flex-1 truncate">{grp.repo}</span>
                                  <span className="text-xs text-text-muted tabular">
                                    {onCount}/{grp.rows.length} branch{grp.rows.length === 1 ? "" : "es"} · viewer
                                  </span>
                                </div>
                                {grp.rows.map((n) => (
                                  <div key={n.namespace} className="flex items-center gap-2 pl-8 pr-3 py-1 text-xs">
                                    <Checkbox
                                      checked={selectedNs.includes(n.namespace)}
                                      onCheckedChange={() => toggleNs(n.namespace)}
                                      aria-label={`Include ${grp.repo} branch ${n.branch}`}
                                    />
                                    <span className="font-mono flex-1 truncate">{n.branch}</span>
                                    <span className="font-mono text-xs text-text-muted truncate max-w-48">
                                      {n.namespace}
                                    </span>
                                  </div>
                                ))}
                              </div>
                            );
                          })}
                        </div>
                        <div className="text-xs text-text-muted mt-1">
                          A token reads a repository only if it is listed here; join more from Repositories.
                        </div>
                      </div>
                    )}
                    <div className="flex gap-2 pt-2">
                      <div className="min-w-0 flex-1">
                        <TextField
                          aria-label="Namespace to add to the token"
                          value={newNs}
                          onChange={(value) => setNewNs(value.replace(/[^a-zA-Z0-9_-]/g, ""))}
                          placeholder="new-robot-namespace"
                        />
                      </div>
                      <Button
                        type="secondary"
                        htmlType="button"
                        iconLeft="plus"
                        onClick={onCreateNamespace}
                        disabled={!newNs.trim()}
                        loading={addingNs}
                      >
                        New namespace
                      </Button>
                    </div>
                    <p className="text-xs text-text-muted mt-1">
                      {selectedNs.length} selected — and nothing else, whatever you join later. Priority = search order
                      (1 = searched first). Role is capped at your grant. Or create a new namespace below.
                    </p>
                  </fieldset>

                  <Field hint="Blank = 90 days. Max 3650.">
                    <TextField
                      label="Expires in (days)"
                      inputMode="numeric"
                      value={ttlDays}
                      onChange={(value) => setTtlDays(value.replace(/[^0-9]/g, ""))}
                      placeholder="90"
                    />
                  </Field>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  {/* The one Python example, next to the token it needs. */}
                  <details className="text-xs text-text-muted min-w-0 flex-1">
                    <summary className="cursor-pointer hover:text-text inline-flex items-center min-h-9 pointer-coarse:min-h-11">
                      Python example for this token
                    </summary>
                    <BotConnectPreview
                      org={identity.org}
                      namespace={selectedNs[0] ?? identity.namespace}
                      baseUrl={typeof window !== "undefined" ? window.location.origin : ""}
                    />
                  </details>
                  <Button
                    type="primary"
                    htmlType="button"
                    iconLeft="plus"
                    onClick={onCreate}
                    disabled={!name.trim() || selectedNs.length === 0}
                  >
                    Create token
                  </Button>
                </div>
              </div>
            )}
          </section>
        )}

        <div className="rounded-2xl border border-border bg-surface p-2">
          <TokensStatusSummary tokens={sa} />
          {sa === null && <Skeleton className="h-12 w-full" />}
          {sa !== null && sa.length === 0 && (
            <p className="text-sm text-text-muted px-2.5 py-3">
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
                    className="flex items-center gap-3 p-2.5 rounded-lg hover:bg-surface-2"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-sm truncate flex items-center gap-2">
                        <KindChip kind={s.kind} client={s.client} />
                        <span className={s.kind === "mcp" ? "" : "font-mono"}>{s.name}</span>
                        <ConnectionBadge token={s} />
                        {s.revoked_at && <Badge text="Revoked" state="danger" size="sm" />}
                        <SaExpiryBadge expiresAt={s.expires_at} revoked={!!s.revoked_at} />
                      </div>
                      <div className="text-xs text-text-muted tabular">
                        {describeTokenScope(s)} · created {relativeTime(s.created_at)}
                        {" · "}
                        {s.last_used_at ? (
                          <>used {relativeTime(s.last_used_at)}</>
                        ) : (
                          <span className="text-text-muted">no calls in 30 days</span>
                        )}
                      </div>
                      {stranded && (
                        <div className="text-xs text-warn mt-0.5">
                          Not connecting? Check the client's MCP config + restart it. If still nothing after a minute,
                          revoke this and generate a fresh token.
                        </div>
                      )}
                    </div>
                    {!s.revoked_at && (
                      <IconButton
                        icon="trash"
                        type="danger"
                        size="sm"
                        label={`Revoke ${s.name}`}
                        tooltip="Revoke"
                        onClick={() => onRevoke(s.name)}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>

      <Modal
        open={mcpResult !== null}
        onOpenChange={(o) => {
          if (!o) setMcpResult(null);
        }}
        title="Token generated"
        className="max-w-[640px] w-full"
      >
        <div className="space-y-3">
          <Alert
            state="warning"
            size="small"
            text="This is the only time the token is shown. The same token works in every client — switch clients below to copy each one's config block, then paste into each client's MCP config file and restart."
          />
          <div className="text-xs text-text-muted" data-testid="minted-token-scope">
            Writes to <span className="font-mono text-text">{mcpScope.home}</span>; reads every namespace and repository
            you belong to, including ones you join later
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
          <Field hint="The token works in every client — pick yours to copy the matching config block. Your last choice is remembered next time.">
            <Select
              label="MCP client"
              value={dialogClient}
              onSelect={(c) => {
                if (isClientId(c)) onDialogClientChange(c);
              }}
            >
              {CLIENTS.map((c) => (
                <Select.Item key={c.id} value={c.id}>
                  {c.label}
                </Select.Item>
              ))}
            </Select>
          </Field>
          {dialogClient === "claude-code" &&
            mcpResult &&
            (() => {
              const base = mcpResult.mcpUrl.replace(/\/mcp$/, "");
              const configJson = JSON.stringify({ url: base, token: mcpResult.token }, null, 2);
              const plugin = pluginInstallLines("claude-code") || "";
              return (
                <div className="space-y-2" data-testid="claude-code-recommended">
                  <div className="text-xs font-medium text-text">
                    Recommended: the plugin (tools, memory primer, capture)
                  </div>
                  <div className="text-xs text-text-muted leading-relaxed">
                    1. Save the token where the plugin, the CLI and the SDK all read it —{" "}
                    <code>~/.loom/config.json</code> (chmod 600):
                  </div>
                  <div className="relative">
                    <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                      {configJson}
                    </pre>
                    <CopyButton text={configJson} label="Copy" ariaLabel="Copy config.json" />
                  </div>
                  <div className="text-xs text-text-muted leading-relaxed">
                    2. Install the plugin; it mounts the Loom tools itself and reads that file. Leave the token prompt
                    empty.
                  </div>
                  <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                    {plugin}
                  </pre>
                  <div className="text-xs text-text-muted leading-relaxed pt-1 border-t border-border">
                    <span className="font-medium text-text">Tools only, no hooks:</span> register the server below
                    instead. Pick one — a user-scope <code>loom</code> entry outranks the plugin's own server.
                  </div>
                </div>
              );
            })()}
          <div className="relative">
            <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 whitespace-pre-wrap">
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
                  <div className="text-xs text-text-muted leading-relaxed">
                    Or register it from a terminal (the client writes its own config file):
                  </div>
                  <div className="relative">
                    <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
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
                <div className="space-y-2 pt-3 border-t border-border">
                  <div className="text-xs font-medium text-text">Automatic capture and recall</div>
                  <div className="text-xs text-text-muted leading-relaxed">
                    The entry above gives Codex the Loom <em>tools</em>. To also load a memory primer at session start
                    and record decisions as you work, install the capture plugin; its hooks read the token from{" "}
                    <code>LOOM_API_KEY</code> or <code>~/.loom/config.json</code>.
                  </div>
                  <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
                    {plugin}
                  </pre>
                </div>
              );
            })()}
          <div className="text-xs text-text-muted leading-relaxed">
            Token lives in the list below — revoke it to disconnect every client at once.
          </div>
        </div>
      </Modal>

      <Modal
        open={mintedToken !== null}
        onOpenChange={(o) => {
          if (!o) setMintedToken(null);
        }}
        title="Token created"
        className="max-w-[680px] w-full"
      >
        <div className="space-y-3">
          <Alert
            state="warning"
            size="small"
            text="This is the only time the token is shown. Copy it now and store it somewhere safe (a secrets manager, your CI's secret store). Lost tokens can't be recovered — only re-issued."
          />
          <div className="relative">
            <div className="text-xs text-text-muted mb-1.5">Token</div>
            <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 break-all whitespace-pre-wrap">
              {mintedToken?.token ?? ""}
            </pre>
            <CopyButton text={mintedToken?.token ?? ""} label="Copy token" className={COPY_BELOW_CAPTION} />
          </div>

          {mintedToken && (
            <div className="relative">
              <div className="text-xs text-text-muted mb-1.5">Connect your bot — Python</div>
              <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-x-auto whitespace-pre">
                {botSnippet(mintedToken)}
              </pre>
              <CopyButton text={botSnippet(mintedToken)} label="Copy code" className={COPY_BELOW_CAPTION} />
              <div className="mt-1.5 text-xs text-text-muted leading-relaxed">
                Install with{" "}
                <code className="font-mono text-xs bg-surface-2 px-1 rounded">pip install clickhouse-loom</code>. The
                same token also works on raw HTTP:{" "}
                <code className="font-mono text-xs bg-surface-2 px-1 rounded">
                  curl -H "Authorization: Bearer $LOOM_API_KEY" {mintedToken.baseUrl}/v1/memory.search
                </code>
              </div>
            </div>
          )}
        </div>
      </Modal>
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
      <div className="text-xs text-text-muted">
        <span className="text-text font-medium">{active}</span> active
        {" · "}
        <span className="text-text font-medium">{waiting}</span> waiting for first use
        {" · "}
        <span className="text-text font-medium">{idle}</span> idle (&gt;7d)
      </div>
      {expiringSoon > 0 && (
        <Alert
          state="warning"
          size="small"
          text={`${expiringSoon} token${expiringSoon === 1 ? " expires" : "s expire"} in the next 14 days — generate a replacement now to avoid downtime.`}
        />
      )}
    </div>
  );
}

function SaExpiryBadge({ expiresAt, revoked }: { expiresAt: string | null; revoked: boolean }) {
  if (revoked) return null;
  if (!expiresAt) {
    return <span className="ml-2 text-xs text-text-muted">never expires</span>;
  }
  const expiryMs = new Date(expiresAt).getTime();
  const now = Date.now();
  const FOURTEEN_DAYS = 14 * 24 * 60 * 60 * 1000;
  const expired = expiryMs < now;
  const expiringSoon = !expired && expiryMs - now <= FOURTEEN_DAYS;
  const tone = expired ? "text-danger" : expiringSoon ? "text-warn" : "text-text-muted";
  return <span className={`ml-2 text-xs ${tone}`}>{expired ? "expired" : `expires ${relativeTime(expiresAt)}`}</span>;
}

// The copy button of a boxed block with a caption above it.
const COPY_BELOW_CAPTION = "absolute top-6 right-2";

function BotConnectPreview({ org, namespace, baseUrl }: { org: string; namespace: string; baseUrl: string }) {
  const code = botSnippet({ org, namespace, baseUrl });
  return (
    <div className="mt-2 rounded-lg border border-border bg-surface-2 p-3">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="text-xs text-text-muted leading-relaxed">
          Export the minted token as <code className="font-mono">LOOM_API_KEY</code>; the namespace follows the first
          one you tick.
        </div>
        <CopyButton
          text={code}
          label="Copy code"
          className="shrink-0 inline-flex items-center gap-1 px-2 py-1 text-xs font-medium bg-surface border border-border rounded-md hover:bg-surface-3"
        />
      </div>
      <pre className="text-xs font-mono bg-bg rounded-lg p-3 max-h-72 overflow-auto whitespace-pre border border-border">
        {code}
      </pre>
      <div className="mt-2 text-xs text-text-muted leading-relaxed">
        Install with <code className="font-mono text-xs bg-surface px-1 rounded">pip install clickhouse-loom</code>.
        Same JWT also works on raw HTTP — set{" "}
        <code className="font-mono text-xs bg-surface px-1 rounded">Authorization: Bearer $LOOM_API_KEY</code>.
      </div>
    </div>
  );
}

function ConnectionBadge({ token }: { token: ServiceAccountEntry }) {
  if (token.revoked_at) return null;
  if (!token.last_used_at) return <Badge text="Waiting…" state="warning" size="sm" />;
  const ageMs = Date.now() - new Date(token.last_used_at).getTime();
  if (ageMs < 30 * 24 * 3600 * 1000) return <Badge text="Connected" state="success" size="sm" />;
  return null;
}

function KindChip({ kind, client }: { kind?: string; client?: string }) {
  if (kind === "mcp") {
    const label = CLIENTS.find((c) => c.id === client)?.label || "MCP";
    return <Badge text={label} state="info" size="sm" />;
  }
  return <Badge text="Script" state="neutral" size="sm" />;
}

/** A form control (which carries its own label) with a hint line under it. */
function Field({ hint, children }: { hint?: string; children: React.ReactNode }) {
  return (
    <div>
      {children}
      {hint && <div className="text-xs text-text-muted mt-1">{hint}</div>}
    </div>
  );
}

const ROLE_LABELS: Record<GrantRole, string> = { viewer: "Viewer", writer: "Writer", admin: "Admin" };

const VERDICT_STATES: Record<string, BadgeState> = {
  written: "success",
  merged: "info",
  duplicate: "info",
  rejected_pinned: "danger",
};

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
    <div className="space-y-3">
      <div className="text-xs text-text-muted leading-relaxed">
        Paste a doc that represents reviewed knowledge — your AGENTS.md, CLAUDE.md, .cursor/rules, or any
        operating-principles doc. Each <code className="font-mono text-xs">##</code> section becomes one memory in{" "}
        <code className="font-mono text-xs">{identity.namespace}</code>. The import bypasses the review queue but still
        respects pinned memories: if a section contradicts a pinned memory it's rejected per-row.
      </div>
      <div className="grid grid-cols-[1fr_auto] gap-2">
        <TextField
          aria-label="Source URI for the imported document"
          value={sourceUri}
          onChange={setSourceUri}
          placeholder="source_uri (e.g. AGENTS.md)"
        />
        <Button type="primary" htmlType="button" iconLeft="upload" loading={submitting} onClick={run}>
          Import
        </Button>
      </div>
      <TextAreaField
        aria-label="Markdown to import"
        rows={10}
        value={content}
        onChange={setContent}
        placeholder={`# My Operating Principles\n\n## Backup discipline\n\nI run nightly backups via Borg.\n\n## Coffee preference\n\nBlack, no sugar.`}
      />
      {result && (
        <div className="border-t border-border pt-3">
          <div className="text-xs text-text-muted mb-2">
            Result · {result.rows.length} section{result.rows.length === 1 ? "" : "s"}
          </div>
          <div className="space-y-1.5">
            {result.rows.map((r, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <Badge text={r.verdict} state={VERDICT_STATES[r.verdict] ?? "neutral"} size="sm" />
                <span className="font-mono text-xs truncate">{r.memory_key}</span>
                <span className="text-text-muted text-xs truncate flex-1">{r.section_heading}</span>
                {r.rejection_reason && (
                  <span className="text-warn text-xs min-w-0 break-words">{r.rejection_reason}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
