import { useEffect, useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  listTokens,
  type TokenEntry,
  importCuratedDoc,
  type ImportCuratedResult,
  countMemories,
  isAuthDisabled,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { shouldShowGettingStarted } from "../lib/onboarding";
import { GettingStarted, useOnboarding } from "../components/GettingStarted";
import { toast, toastError } from "../components/Toast";
import { PrivacyFilters, type PrivacyFilterStarter } from "./admin/Blacklist";
import { canCall } from "../lib/canCall";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { Alert, PageHeader, TabTrigger } from "../components/ui";
import { Modal } from "../components/ui/Modal";
import { useNamespaceRole } from "../lib/namespaceRole";
import { isMemoryNamespace } from "../lib/namespace";
import { hasPlatformAdminClaim } from "../lib/role";
import { TokensSection } from "./Tokens";
import { ModelsSettings } from "./ModelsSettings";
import { WebhooksSection } from "./WebhooksSection";
import { StructureTab } from "./StructureTab";

/** The URL carries the open settings tab
 *  (`?tab=memory`). */
const SETTINGS_TABS = ["tokens", "memory", "structure", "models"] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number];
const TAB_LABELS: Record<SettingsTab, string> = {
  tokens: "Tokens",
  memory: "Memory",
  structure: "Structure",
  models: "Models & keys",
};

function isSettingsTab(v: string | null): v is SettingsTab {
  return SETTINGS_TABS.some((t) => t === v);
}

function readTabParam(): SettingsTab | null {
  if (typeof window === "undefined") return null;
  const v = new URLSearchParams(window.location.search).get("tab");
  return isSettingsTab(v) ? v : null;
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
  const [tokens, setTokens] = useState<TokenEntry[] | null>(null);
  const loadTokens = () => {
    listTokens()
      .then(setTokens)
      .catch(() => setTokens([]));
  };
  useEffect(loadTokens, []);
  const [lastMint, setLastMint] = useState<{ token: string; mcpUrl: string } | null>(null);

  // One `memory.list` with `count_only` for the namespace-wide total
  // (the two cached counts, no page — `limit 1` would read every preview in the
  // namespace) — the checklist's "is this account new" signal and its "wrote a memory"
  // tick. Only when the token's role here is known — an unknown role (no grant, no claim
  // on this namespace) sends nothing and the checklist stays hidden, as it does for a
  // null total.
  const role = useNamespaceRole(namespace);
  const canListMemories = canCall("memory.list", role, whoami);
  const showStructure = !!memoryNamespace && canCall("memory_schema.list", role, whoami);
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
      <PageHeader title="Settings" tab={TAB_LABELS[tab]}>
        {tab === "models" ? (
          <span className="text-sm text-text-muted">Deployment-wide</span>
        ) : (
          <ScopeBadge kind="namespace" value={namespace} />
        )}
      </PageHeader>
      {!namespace && (
        // No saved pick, no own namespace, no usable claim. Say so here — the landing
        // view — instead of inventing a name. The token reaches no namespace of ANY kind
        // (a code/QA-only token lands on Repositories instead), and the way in is a join
        // or a grant. `code.repos` and `tracker.source.list` are org-level reads at
        // viewer role: a known principal may make them, so the button shows once whoami
        // is known.
        <Alert
          role="status"
          className="mb-6 max-w-2xl"
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
          tokens={tokens}
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
            {TAB_LABELS.tokens}
          </TabTrigger>
          <TabTrigger value="memory" icon="light-bulb">
            {TAB_LABELS.memory}
          </TabTrigger>
          {showStructure && (
            <TabTrigger value="structure" icon="table">
              {TAB_LABELS.structure}
            </TabTrigger>
          )}
          {canManageModels && (
            <TabTrigger value="models" icon="gear">
              {TAB_LABELS.models}
            </TabTrigger>
          )}
        </Tabs.TriggersList>
        <Tabs.Content value="tokens">
          <TokensSection
            tokens={tokens}
            reloadTokens={loadTokens}
            onMinted={setLastMint}
            formOpen={formOpen}
            onFormOpenChange={setFormOpen}
          />
        </Tabs.Content>
        <Tabs.Content value="memory">
          <MemoryTab />
        </Tabs.Content>
        <Tabs.Content value="structure">
          {/* Keyed by scope: another namespace starts with no rows, draft or dialog of this one. */}
          {showStructure ? (
            <StructureTab key={`${identity.org}/${namespace}`} />
          ) : (
            <p>Structure is shown for a memory namespace you can read.</p>
          )}
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

/** The Memory tab: what is never saved, seeding from a document, and, for a namespace
 *  admin, the namespace's webhooks. */
function MemoryTab() {
  const [importing, setImporting] = useState(false);
  // Hidden below namespace ADMIN: the section reads webhook.list on mount.
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const showWebhooks = canCall("webhook.list", role, whoami);
  return (
    <div className="max-w-3xl space-y-8">
      <section aria-labelledby="settings-never-save">
        <h2 id="settings-never-save" className="text-text">
          Never save
        </h2>
        <p className="text-xs text-text-muted mt-0.5 mb-3 max-w-2xl">
          A write whose value matches a pattern is refused — extracted ones too.
        </p>
        <PrivacyFilters key={`${identity.org}/${identity.namespace}`} compact starters={PRIVACY_STARTERS} />
      </section>

      <section aria-labelledby="settings-seed" className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-2xl">
          <h2 id="settings-seed" className="text-text">
            Seed from a document
          </h2>
          <p className="text-xs text-text-muted mt-0.5">
            Turn an AGENTS.md, CLAUDE.md or rules file into memories — one per{" "}
            <code className="code-inline text-xs">##</code> section.
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

      {showWebhooks && <WebhooksSection key={`${identity.org}/${identity.namespace}`} />}
    </div>
  );
}

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
      toastError("Import failed", e);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="text-xs text-text-muted leading-relaxed">
        Paste a doc that represents reviewed knowledge — your AGENTS.md, CLAUDE.md, .cursor/rules, or any
        operating-principles doc. Each <code className="code-inline text-xs">##</code> section becomes one memory in{" "}
        <code className="code-inline text-xs">{identity.namespace}</code>. The import bypasses the review queue but
        still respects pinned memories: if a section contradicts a pinned memory it's rejected per-row.
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
