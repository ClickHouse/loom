import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  createToken,
  registerNamespace,
  revokeToken,
  type MyNamespaceEntry,
  type TokenEntry,
  type TokenCreated,
  type TokenNamespace,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { Skeleton } from "../components/Skeleton";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { relativeTime, humanizeError, parseChTime } from "../lib/format";
import { useAbortableEffect, useVisiblePoll } from "../lib/hooks";
import { canCallCodeRepos } from "../lib/canCall";
import { Alert, ListTime, LoadingSkeleton, Tip } from "../components/ui";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Modal } from "../components/ui/Modal";
import { IconButton } from "../components/IconButton";
import { isMemoryNamespace } from "../lib/namespace";
import {
  CLIENTS,
  configPreviewText,
  formatMcpConfig,
  isClientId,
  pluginInstallLines,
  type ClientId,
} from "../lib/clients";
import { describeTokenScope, everythingScope } from "../lib/tokenScope";
import { TokenReadScopePanel } from "../components/TokenReadScope";
import { CopyButton } from "../components/CopyButton";
import { invalidateShared, sharedCodeRepos, sharedMine } from "../lib/sharedReads";

const RESTRICTED_DOCS =
  "https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted";

// The roles a token may carry on a listed namespace, lowest first. The role picked per
// namespace is capped at your live grant on it (downgrade allowed, escalation not).
const GRANT_ROLES = ["viewer", "writer", "admin"] as const;
type GrantRole = (typeof GRANT_ROLES)[number];
const ROLE_LABELS: Record<GrantRole, string> = { viewer: "Viewer", writer: "Writer", admin: "Admin" };

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
const writes = (role: GrantRole) => role !== "viewer";

/** What the token reads: everything you belong to, or only the namespaces listed. */
type Reach = "everything" | "listed";

const REACH_OPTIONS = [
  {
    value: "everything",
    label: "Everything you belong to",
    hint: "Every namespace and repository you are a member of, including ones you join later, at your role in each.",
  },
  {
    value: "listed",
    label: "Only these namespaces",
    hint: "Exactly the ones you tick, and nothing you join later. For a script, a CI job or a shared bot.",
  },
] as const;

/** Where the result dialog's connect instructions are for: an MCP client, or a script. */
type ConnectTarget = ClientId | "script";
const CONNECT_KEY = "loom_last_mcp_client_v1";

function isConnectTarget(v: string | null): v is ConnectTarget {
  return v === "script" || isClientId(v);
}

/** The Python quick-start for a token, with the token inline. */
function scriptSnippet(m: { org: string; namespace: string; baseUrl: string; token: string }): string {
  return `import asyncio
from loom import Agent

async def main():
    async with Agent(
        org=${JSON.stringify(m.org)},
        namespace=${JSON.stringify(m.namespace)},
        agent="my-script",                        # names this run in the audit log
        base_url=${JSON.stringify(m.baseUrl)},
        api_key=${JSON.stringify(m.token)},     # or set LOOM_API_KEY in env
    ) as a:
        hits = await a.memory.search("when can we deploy?", top_k=5)
        for h in hits.results:
            print(h.value)

asyncio.run(main())`;
}

/** Parse the expiry field: blank is the default (90 days), 0 never expires. */
function parseTtl(raw: string): number | null | "invalid" {
  const t = raw.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isInteger(n) && n >= 0 && n <= 3650 ? n : "invalid";
}

export function TokensSection({
  tokens,
  reloadTokens,
  onMinted,
  formOpen,
  onFormOpenChange,
}: {
  /** The tokens list, fetched once by Setup and shared with the checklist. */
  tokens: TokenEntry[] | null;
  reloadTokens: () => void;
  /** A token was made — the checklist renders its connect line. */
  onMinted: (r: { token: string; mcpUrl: string }) => void;
  /** The form is folded once a live token exists; `null` follows the list. */
  formOpen: boolean | null;
  onFormOpenChange: (open: boolean) => void;
}) {
  const liveTokens = (tokens ?? []).filter((t) => !t.revoked_at);
  const formVisible = formOpen ?? (tokens !== null && liveTokens.length === 0);
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

  const [created, setCreated] = useState<TokenCreated | null>(null);
  // Poll while a token made IN THIS VISIT is waiting for its first call, for at most ten
  // minutes after it was made (so a stale never-used token does not keep every later
  // visit polling forever). Sleeps while the tab is hidden.
  const [mintedAtMs, setMintedAtMs] = useState<number | null>(null);
  const waitingForFirstCall =
    mintedAtMs !== null &&
    Date.now() - mintedAtMs < 10 * 60_000 &&
    (tokens || []).some((s) => !s.last_used_at && !s.revoked_at);
  // Each tick's setSa re-renders, which re-checks the ten-minute window.
  useVisiblePoll(reloadTokens, 15_000, waitingForFirstCall);

  const onRevoke = async (tokenName: string) => {
    if (
      !(await confirmAction({
        title: `Revoke ${tokenName}?`,
        message:
          "It stops working at once, in every tool and script that uses it. Remove it from their settings too, so they stop trying.",
        confirmLabel: "Revoke",
        danger: true,
      }))
    )
      return;
    try {
      await revokeToken(tokenName);
      toast("Revoked", "ok");
      reloadTokens();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    }
  };

  return (
    <>
      <section className="mb-8" id="setup-connect" aria-label="Tokens">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <p className="text-sm text-text-muted max-w-2xl">
            A token lets an AI tool, agent or script use Loom as you, never with more access than you have. Revoke it
            here at any time.
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
          <TokenForm
            formRef={tokenFormRef}
            onCancel={liveTokens.length > 0 ? () => toggleForm(false) : undefined}
            onCreated={(r) => {
              setCreated(r);
              onMinted({ token: r.token, mcpUrl: r.mcp_url });
              setMintedAtMs(Date.now());
              reloadTokens();
            }}
          />
        )}

        <TokenList tokens={tokens} onRevoke={onRevoke} />
      </section>

      <CreatedDialog created={created} onClose={() => setCreated(null)} />
    </>
  );
}

function TokenForm({
  formRef,
  onCancel,
  onCreated,
}: {
  formRef: React.RefObject<HTMLElement>;
  /** Absent while no live token exists: the form is the page's first step. */
  onCancel?: () => void;
  onCreated: (r: TokenCreated) => void;
}) {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const storedMine = useStore((s) => s.myNamespaces);
  const [mine, setMine] = useState<MyNamespaceEntry[] | null>(null);
  const [reach, setReach] = useState<Reach>("everything");
  const [name, setName] = useState("");
  const [ttlDays, setTtlDays] = useState("90");
  const [busy, setBusy] = useState(false);
  // Everything: the namespace it saves to follows the page's namespace until picked here,
  // and the restricted repositories (by repo name) added to it.
  const [homeEdited, setHomeEdited] = useState<string | null>(null);
  const [restrictedRepos, setRestrictedRepos] = useState<Set<string>>(() => new Set());
  // Only these: `selectedNs` order = read priority (index 0 = searched first, and where it
  // saves when it may write there). nsRole is the role per namespace, capped at your grant.
  const [selectedNs, setSelectedNs] = useState<string[]>([]);
  const [nsRole, setNsRole] = useState<Record<string, GrantRole>>({});
  const [newNs, setNewNs] = useState("");
  const [addingNs, setAddingNs] = useState(false);

  // Once per identity; an answer for an org or namespace the page has left is dropped.
  // The first answer ticks the page's namespace for "Only these" when nothing is ticked.
  useAbortableEffect(
    async (_signal, live) => {
      let active: MyNamespaceEntry[];
      try {
        active = (await sharedMine(identity.org)).filter((r) => r.status !== "pending");
      } catch {
        if (live()) setMine([]);
        return;
      }
      if (!live()) return;
      setMine(active);
      const seed = active.find((r) => r.namespace === identity.namespace && r.kind !== "code");
      if (!seed) return;
      setSelectedNs((prev) => (prev.length > 0 ? prev : [seed.namespace]));
      setNsRole((prev) =>
        Object.keys(prev).length > 0 ? prev : { [seed.namespace]: grantRoleOf(active, seed.namespace) },
      );
    },
    [identity.org, identity.namespace],
  );

  const eligibleNs = (mine ?? []).filter((n) => n.kind !== "code");
  const eligibleRepos = (mine ?? []).filter((n) => n.kind === "code");
  // Repository and branch per code namespace, from code.repos; a namespace the list does
  // not know keeps its identifier as the label. `code.repos` only when the caller has
  // standing on a code namespace or an admin claim.
  const reposReadable = canCallCodeRepos(mine ?? storedMine, whoami);
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
    const by = new Map<string, { repo: string; restricted: boolean; rows: { namespace: string; branch: string }[] }>();
    for (const n of eligibleRepos) {
      const info = repoInfo[n.namespace];
      const repo = info?.repo ?? n.namespace;
      const grp = by.get(repo) ?? { repo, restricted: info?.classification === "restricted", rows: [] };
      grp.rows.push({ namespace: n.namespace, branch: info?.branch ?? n.namespace });
      by.set(repo, grp);
    }
    // Branches in the order `code.repos` lists them: the default branch first.
    const listed = Object.keys(repoInfo);
    const at = (ns: string) => (listed.includes(ns) ? listed.indexOf(ns) : listed.length);
    for (const grp of by.values()) grp.rows.sort((a, b) => at(a.namespace) - at(b.namespace));
    return Array.from(by.values()).sort((a, b) => a.repo.localeCompare(b.repo));
  }, [eligibleRepos, repoInfo]);

  // Where it saves. Everything: the memory namespace picked here, the page's own by
  // default. Only these: the server picks (`caller_home`), from your saved pick, which
  // the page cannot see, so the form states the rule and the result names the namespace.
  const writableMemory = eligibleNs
    .filter((n) => isMemoryNamespace(n.namespace, mine) && writes(grantRoleOf(mine, n.namespace)))
    .map((n) => n.namespace);
  const listedWrites = selectedNs.some((ns) => isMemoryNamespace(ns, mine) && writes(nsRole[ns] ?? "viewer"));
  const everythingHome =
    homeEdited && writableMemory.includes(homeEdited)
      ? homeEdited
      : writableMemory.includes(identity.namespace)
        ? identity.namespace
        : (writableMemory[0] ?? "");
  const savesNowhere = reach === "everything" ? !everythingHome : !listedWrites;

  const scope = useMemo(
    () =>
      everythingScope(
        mine,
        Object.entries(repoInfo).map(([namespace, f]) => ({ namespace, ...f })),
        everythingHome,
      ),
    [mine, repoInfo, everythingHome],
  );

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
      setNsRole((m) => ({ ...m, [ns]: grantRoleOf(mine, ns) }));
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

  // A namespace of its own for a bot: register it (the server makes you its admin), then
  // it is listed and ticked at once.
  const onCreateNamespace = async () => {
    const ns = newNs.trim();
    if (!ns) return;
    setAddingNs(true);
    try {
      await registerNamespace(
        { org: identity.org, namespace: ns },
        { owner_email: whoami?.subject || identity.namespace, description: "Namespace for a token" },
      );
      // The new admin grant must reach every reader of list_mine at once: the store's
      // copy and the shared cache.
      invalidateShared("mine:");
      const rows = await sharedMine(identity.org);
      useStore.getState().setMyNamespaces(rows);
      setMine(rows.filter((r) => r.status !== "pending"));
      setSelectedNs((prev) => [...prev.filter((x) => x !== ns), ns]);
      setNsRole((m) => ({ ...m, [ns]: "admin" }));
      setNewNs("");
      toast(`Created ${ns}; you are its admin`, "ok");
    } catch (e) {
      toast(`Failed to create namespace: ${humanizeError(e)}`, "error");
    } finally {
      setAddingNs(false);
    }
  };

  const onCreate = async () => {
    const ttl = parseTtl(ttlDays);
    if (ttl === "invalid") {
      toast("Expiry must be 1-3650 days, 0 for never, or blank for 90", "error");
      return;
    }
    let namespaces: TokenNamespace[];
    if (reach === "everything") {
      const restricted = scope.excludedRestricted
        .filter((r) => restrictedRepos.has(r.repo))
        .flatMap((r) => r.branches.map((b) => b.namespace));
      namespaces = [...(everythingHome ? [everythingHome] : []), "*", ...restricted];
    } else {
      if (selectedNs.length === 0) return;
      // Ordered = read priority; repositories are read-only.
      namespaces = selectedNs.map((ns) => ({
        namespace: ns,
        role: eligibleRepos.some((r) => r.namespace === ns) ? "viewer" : (nsRole[ns] ?? grantRoleOf(mine, ns)),
      }));
    }
    setBusy(true);
    try {
      onCreated(await createToken({ name: name.trim() || undefined, namespaces, ttl_days: ttl }));
      setName("");
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const nothingListed = mine !== null && eligibleNs.length === 0 && eligibleRepos.length === 0;
  const canCreate = reach === "everything" || selectedNs.length > 0;

  return (
    <section
      ref={formRef}
      className="mb-4 rounded-2xl border border-border bg-surface p-4 space-y-4"
      aria-labelledby="new-token-title"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="new-token-title" className="text-text">
          New token
        </h2>
        {onCancel && (
          <Button type="empty" htmlType="button" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>

      <ChoiceGroup<Reach> label="Can read" value={reach} onChange={setReach} options={REACH_OPTIONS} />

      {reach === "everything" ? (
        <TokenReadScopePanel
          scope={scope}
          docsHref={RESTRICTED_DOCS}
          included={restrictedRepos}
          onToggleRepo={(r) =>
            setRestrictedRepos((prev) => {
              const next = new Set(prev);
              if (next.has(r.repo)) next.delete(r.repo);
              else next.add(r.repo);
              return next;
            })
          }
        />
      ) : nothingListed ? (
        <p className="text-sm text-text-muted">You are not a member of any namespace yet.</p>
      ) : (
        <fieldset className="space-y-3">
          <legend className="sr-only">Namespaces this token reads</legend>
          <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-surface-2 divide-y divide-border">
            {eligibleNs.map((n) => {
              const pos = selectedNs.indexOf(n.namespace);
              const isOn = pos >= 0;
              const grant = grantRoleOf(mine, n.namespace);
              return (
                <div key={n.namespace} className="flex items-center gap-2 px-3 py-1.5 text-sm">
                  <Checkbox
                    checked={isOn}
                    onCheckedChange={() => toggleNs(n.namespace)}
                    aria-label={`Read ${n.namespace}`}
                  />
                  <Tip content="Search order (1 = searched first)">
                    <div className="w-16 shrink-0">
                      <Select
                        triggerProps={{ "aria-label": `Search order for ${n.namespace}` }}
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
                  <Tip content={`At most your own role (${grant})`}>
                    <div className="w-28 shrink-0">
                      <Select
                        triggerProps={{ "aria-label": `Role on ${n.namespace}` }}
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
          {repoGroups.length > 0 && (
            <div>
              <div className="text-xs font-medium text-text-muted mb-1">Repositories (read-only)</div>
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
                          aria-label={`Read every branch of ${grp.repo}`}
                        />
                        <span className="font-medium flex-1 truncate">{grp.repo}</span>
                        {grp.restricted && (
                          <span className="inline-flex items-center gap-1 text-xs text-text-muted">
                            <Icon name="lock" size="xs" aria-hidden="true" />
                            restricted
                          </span>
                        )}
                        <span className="text-xs text-text-muted tabular">
                          {onCount}/{grp.rows.length} branch{grp.rows.length === 1 ? "" : "es"}
                        </span>
                      </div>
                      {grp.rows.map((n) => (
                        <div key={n.namespace} className="flex items-center gap-2 pl-8 pr-3 py-1 text-xs">
                          <Checkbox
                            checked={selectedNs.includes(n.namespace)}
                            onCheckedChange={() => toggleNs(n.namespace)}
                            aria-label={`Read ${grp.repo} branch ${n.branch}`}
                          />
                          <span className="font-mono flex-1 truncate">{n.branch}</span>
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          <div className="flex gap-2">
            <div className="min-w-0 flex-1">
              <TextField
                aria-label="New namespace name"
                value={newNs}
                onChange={(value) => setNewNs(value.replace(/[^a-zA-Z0-9_-]/g, ""))}
                placeholder="A namespace of its own, e.g. nightly-bot"
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
              Create namespace
            </Button>
          </div>
          <p className="text-xs text-text-muted">
            {selectedNs.length} ticked, and nothing else, whatever you join later. Search order 1 is searched first.
          </p>
        </fieldset>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {savesNowhere ? (
          <div className="text-xs" data-testid="token-saves-nowhere">
            <div className="font-medium text-text-muted mb-1">Saves memories to</div>
            <div className="text-text">Nowhere: it only reads.</div>
            <div className="text-text-muted mt-1">
              {reach === "everything"
                ? "You cannot write to any namespace."
                : "Give it Writer on a ticked namespace to let it save."}
            </div>
          </div>
        ) : reach === "everything" ? (
          <Field hint="Where memories and captured sessions go when a call names no namespace.">
            {(hintId) => (
              <Select
                label="Saves memories to"
                value={everythingHome}
                onSelect={setHomeEdited}
                triggerProps={{ "aria-describedby": hintId }}
              >
                {writableMemory.map((ns) => (
                  <Select.Item key={ns} value={ns}>
                    {ns}
                  </Select.Item>
                ))}
              </Select>
            )}
          </Field>
        ) : (
          <div className="text-xs" data-testid="token-saves-listed">
            <div className="font-medium text-text-muted mb-1">Saves memories to</div>
            <div className="text-text">The namespace you work in, when it is ticked and it can write there</div>
            <div className="text-text-muted mt-1">
              Else one named after you, else the first ticked one it can write. The new token names it.
            </div>
          </div>
        )}
        <Field hint="Optional. Shown in the list below; it cannot be changed later.">
          {(hintId) => (
            <TextField
              label="Name"
              value={name}
              onChange={setName}
              placeholder="e.g. laptop or nightly-cron"
              aria-describedby={hintId}
            />
          )}
        </Field>
        <Field hint="Blank is 90 days, 0 is never (needs a dashboard or `loom login` sign-in).">
          {(hintId) => (
            <TextField
              label="Expires in (days)"
              inputMode="numeric"
              value={ttlDays}
              onChange={(value) => setTtlDays(value.replace(/[^0-9]/g, ""))}
              placeholder="90"
              aria-describedby={hintId}
            />
          )}
        </Field>
      </div>

      <div className="flex justify-end">
        <Button
          id="setup-generate-token"
          type="primary"
          htmlType="button"
          iconLeft="plus"
          loading={busy}
          disabled={!canCreate}
          onClick={onCreate}
        >
          Create token
        </Button>
      </div>
    </section>
  );
}

/** One line on what a just-made token reads and where it saves, as the server minted it. */
function createdScope(c: TokenCreated, writesAnywhere: boolean): string {
  const named = c.namespaces.filter((n) => n !== "*");
  const reads = c.reads_everything
    ? `Reads everything you belong to, including namespaces you join later${
        c.restricted_namespaces.length ? `, plus ${c.restricted_namespaces.join(", ")}` : ""
      }.`
    : `Reads only ${named.join(", ")}.`;
  const roles = Object.values(c.ns_roles);
  const readOnly = c.reads_everything ? !writesAnywhere : roles.length > 0 && roles.every((r) => r === "viewer");
  const saves =
    c.saves_to === null
      ? "Where it saves is worked out on each call."
      : c.saves_to === ""
        ? "Each call must name a namespace."
        : readOnly
          ? `Read-only; calls that name no namespace read ${c.saves_to}.`
          : `Saves to ${c.saves_to}.`;
  return `${reads} ${saves}`;
}

function CreatedDialog({ created, onClose }: { created: TokenCreated | null; onClose: () => void }) {
  const identity = useIdentity();
  const writesAnywhere = useWritable().length > 0;
  // The last target picked; none yet: a script for a token that reads only some
  // namespaces, Claude Code for one that reads everything.
  const [stored, setStored] = useState<ConnectTarget | null>(() => {
    // Blocked storage throws on read; the default stands in.
    try {
      const v = localStorage.getItem(CONNECT_KEY);
      return isConnectTarget(v) ? v : null;
    } catch {
      return null;
    }
  });
  const target: ConnectTarget = stored ?? (created && !created.reads_everything ? "script" : "claude-code");
  const pick = (t: ConnectTarget) => {
    setStored(t);
    try {
      localStorage.setItem(CONNECT_KEY, t);
    } catch {
      /* quota */
    }
  };
  return (
    <Modal
      open={created !== null}
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title="Token created"
      className="max-w-[680px] w-full"
    >
      {created && (
        <div className="space-y-3">
          <Alert
            state="warning"
            size="small"
            text="Copy it now: this is the only time it is shown. The same token works in every tool."
          />
          <div className="text-xs text-text-muted" data-testid="minted-token-scope">
            {createdScope(created, writesAnywhere)}
          </div>
          <div className="relative">
            <div className="text-xs text-text-muted mb-1.5">Token</div>
            <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 break-all whitespace-pre-wrap">
              {created.token}
            </pre>
            <CopyButton text={created.token} label="Copy token" className={COPY_BELOW_CAPTION} />
          </div>
          <Select
            label="Connect"
            value={target}
            onSelect={(v) => {
              if (isConnectTarget(v)) pick(v);
            }}
          >
            {CLIENTS.map((c) => (
              <Select.Item key={c.id} value={c.id}>
                {c.label}
              </Select.Item>
            ))}
            <Select.Item value="script">A script (Python or HTTP)</Select.Item>
          </Select>
          {target === "script" ? (
            <ScriptConnect
              token={created.token}
              org={created.org}
              namespace={created.saves_to || created.namespaces.find((n) => n !== "*") || identity.namespace}
              baseUrl={created.mcp_url.replace(/\/mcp$/, "")}
            />
          ) : (
            <ClientConnect client={target} token={created.token} mcpUrl={created.mcp_url} />
          )}
          <div className="text-xs text-text-muted leading-relaxed">
            Revoke it in the list below to disconnect every tool that uses it at once.
          </div>
        </div>
      )}
    </Modal>
  );
}

function ClientConnect({ client, token, mcpUrl }: { client: ClientId; token: string; mcpUrl: string }) {
  const cfg = formatMcpConfig({ client, token, mcpUrl });
  const text = configPreviewText(cfg);
  const commandLines = cfg.command ? (cfg.envLines ? `${cfg.command}\n${cfg.envLines}` : cfg.command) : "";
  return (
    <div className="space-y-3">
      {client === "claude-code" && (
        <div className="space-y-2" data-testid="claude-code-recommended">
          <div className="text-xs font-medium text-text">Recommended: the plugin (tools, memory primer, capture)</div>
          <div className="text-xs text-text-muted leading-relaxed">
            1. Save the token where the plugin, the CLI and the SDK all read it — <code>~/.loom/config.json</code>{" "}
            (chmod 600):
          </div>
          <CodeBlock
            text={JSON.stringify({ url: mcpUrl.replace(/\/mcp$/, ""), token }, null, 2)}
            label="Copy"
            ariaLabel="Copy config.json"
          />
          <div className="text-xs text-text-muted leading-relaxed">
            2. Install the plugin; it mounts the Loom tools itself and reads that file. Leave the token prompt empty.
          </div>
          <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
            {pluginInstallLines("claude-code") || ""}
          </pre>
          <div className="text-xs text-text-muted leading-relaxed pt-1 border-t border-border">
            <span className="font-medium text-text">Tools only, no hooks:</span> register the server below instead. Pick
            one — a user-scope <code>loom</code> entry outranks the plugin's own server.
          </div>
        </div>
      )}
      <CodeBlock text={text} label="Copy config" />
      {commandLines && (
        <div className="space-y-2">
          <div className="text-xs text-text-muted leading-relaxed">
            Or register it from a terminal (the client writes its own config file):
          </div>
          <CodeBlock text={commandLines} label="Copy command" />
        </div>
      )}
      {client === "codex" && (
        <div className="space-y-2 pt-3 border-t border-border">
          <div className="text-xs font-medium text-text">Automatic capture and recall</div>
          <div className="text-xs text-text-muted leading-relaxed">
            The entry above gives Codex the Loom <em>tools</em>. To also load a memory primer at session start and
            record decisions as you work, install the capture plugin; its hooks read the token from{" "}
            <code>LOOM_API_KEY</code> or <code>~/.loom/config.json</code>.
          </div>
          <pre className="text-xs font-mono bg-surface-2 rounded-lg p-3 overflow-auto whitespace-pre-wrap">
            {pluginInstallLines("codex") || ""}
          </pre>
        </div>
      )}
    </div>
  );
}

function ScriptConnect(m: { token: string; org: string; namespace: string; baseUrl: string }) {
  const env = `export LOOM_API_KEY="${m.token}"`;
  return (
    <div className="space-y-2">
      <div className="text-xs text-text-muted">Set it in the environment of the job, or your CI's secret store:</div>
      <CodeBlock text={env} label="Copy" ariaLabel="Copy the export line" />
      <div className="text-xs text-text-muted">
        Python (<code className="code-inline text-xs">pip install clickhouse-loom</code>):
      </div>
      <CodeBlock text={scriptSnippet(m)} label="Copy code" scroll />
      <div className="text-xs text-text-muted leading-relaxed">
        Or over HTTP:{" "}
        <code className="code-inline text-xs">
          curl -H "Authorization: Bearer $LOOM_API_KEY" {m.baseUrl}/v1/memory.search
        </code>
      </div>
    </div>
  );
}

function CodeBlock({
  text,
  label,
  ariaLabel,
  scroll = false,
}: {
  text: string;
  label: string;
  ariaLabel?: string;
  /** Long code keeps its lines and scrolls sideways instead of wrapping. */
  scroll?: boolean;
}) {
  return (
    <div className="relative">
      <pre
        className={`text-xs font-mono bg-surface-2 rounded-lg p-3 pr-20 overflow-auto ${scroll ? "whitespace-pre" : "whitespace-pre-wrap break-all"}`}
      >
        {text}
      </pre>
      <CopyButton text={text} label={label} ariaLabel={ariaLabel} />
    </div>
  );
}

/** The memory namespaces you may write: a `*` token writes nowhere you do not. */
function useWritable(): string[] {
  const mine = useStore((s) => s.myNamespaces);
  return (mine ?? [])
    .filter(
      (n) => n.status !== "pending" && isMemoryNamespace(n.namespace, mine) && writes(grantRoleOf(mine, n.namespace)),
    )
    .map((n) => n.namespace);
}

function TokenList({ tokens, onRevoke }: { tokens: TokenEntry[] | null; onRevoke: (name: string) => void }) {
  const writable = useWritable();
  const me = useStore((s) => s.whoami?.subject ?? "");
  return (
    <div className="rounded-2xl border border-border bg-surface p-2">
      <TokensStatusSummary tokens={tokens} />
      {tokens === null && (
        <LoadingSkeleton label="Loading tokens">
          <Skeleton className="h-12 w-full" />
        </LoadingSkeleton>
      )}
      {tokens !== null && tokens.length === 0 && (
        <p className="text-sm text-text-muted px-2.5 py-3">
          No tokens yet. Create one above; you pick the tool to connect after it is made.
        </p>
      )}
      {tokens !== null && tokens.length > 0 && (
        <div className="space-y-1">
          {tokens.map((s) => {
            const created = parseChTime(s.created_at);
            const stranded =
              !s.revoked_at && !s.last_used_at && !!created && Date.now() - created.getTime() > 60 * 60 * 1000;
            return (
              <div key={s.sa_subject || s.name} className="flex items-center gap-3 p-2.5 rounded-lg hover:bg-surface-2">
                <div className="flex-1 min-w-0">
                  <div className="text-sm truncate flex flex-wrap items-center gap-2">
                    <span>{s.name}</span>
                    <ConnectionBadge token={s} />
                    {s.revoked_at && <Badge text="Revoked" state="danger" size="sm" />}
                    <ExpiryBadge expiresAt={s.expires_at} revoked={!!s.revoked_at} />
                  </div>
                  <div className="text-xs text-text-muted tabular">
                    {describeTokenScope(s, {
                      writesAnywhere: writable.length > 0,
                      yours: s.yours ?? (!me || s.created_by === me),
                      writable: (ns) => writable.includes(ns),
                    })}{" "}
                    · created <ListTime time={s.created_at} quiet />
                    {" · "}
                    {s.last_used_at ? (
                      <>
                        used <ListTime time={s.last_used_at} quiet />
                      </>
                    ) : (
                      <span className="text-text-muted">no calls in 30 days</span>
                    )}
                  </div>
                  {stranded && (
                    <div className="text-xs text-warn mt-0.5">
                      Not connecting? Check the tool's settings and restart it. If nothing calls within a minute, revoke
                      this and create a new token.
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
  );
}

function TokensStatusSummary({ tokens }: { tokens: TokenEntry[] | null }) {
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
    const exp = parseChTime(t.expires_at);
    if (exp && exp.getTime() > now && exp.getTime() - now <= FOURTEEN_DAYS) {
      expiringSoon += 1;
    }
    if (!t.last_used_at) {
      waiting += 1;
      continue;
    }
    const usedAt = parseChTime(t.last_used_at);
    if (usedAt && now - usedAt.getTime() <= SEVEN_DAYS) active += 1;
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
          text={`${expiringSoon} token${expiringSoon === 1 ? " expires" : "s expire"} in the next 14 days — create a replacement now to avoid downtime.`}
        />
      )}
    </div>
  );
}

function ExpiryBadge({ expiresAt, revoked }: { expiresAt: string | null; revoked: boolean }) {
  if (revoked) return null;
  if (!expiresAt) {
    return <span className="text-xs text-text-muted">never expires</span>;
  }
  const expiry = parseChTime(expiresAt);
  if (!expiry) return null;
  const now = Date.now();
  const FOURTEEN_DAYS = 14 * 24 * 60 * 60 * 1000;
  const expired = expiry.getTime() < now;
  const expiringSoon = !expired && expiry.getTime() - now <= FOURTEEN_DAYS;
  const tone = expired ? "text-danger" : expiringSoon ? "text-warn" : "text-text-muted";
  return <span className={`text-xs ${tone}`}>{expired ? "expired" : `expires ${relativeTime(expiresAt)}`}</span>;
}

// The copy button of a boxed block with a caption above it.
const COPY_BELOW_CAPTION = "absolute top-6 right-2";

function ConnectionBadge({ token }: { token: TokenEntry }) {
  if (token.revoked_at) return null;
  if (!token.last_used_at) return <Badge text="Waiting…" state="warning" size="sm" />;
  const usedAt = parseChTime(token.last_used_at);
  if (usedAt && Date.now() - usedAt.getTime() < 30 * 24 * 3600 * 1000) {
    return <Badge text="Connected" state="success" size="sm" />;
  }
  return null;
}

/** A form control (which carries its own label) with a hint line under it. `children`
 * gets the hint's id for the control's aria-describedby, so the hint is announced. */
function Field({ hint, children }: { hint: string; children: (hintId: string) => React.ReactNode }) {
  const hintId = useId();
  return (
    <div>
      {children(hintId)}
      <div id={hintId} className="text-xs text-text-muted mt-1">
        {hint}
      </div>
    </div>
  );
}
