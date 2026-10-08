import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Globe,
  Users,
  Check,
  Eye,
  UserRound,
  Crown,
  X as XIcon,
  Clock as ClockIcon,
  UserPlus,
  Settings as SettingsIcon,
  Plus,
} from "lucide-react";
import {
  browseNamespaces,
  joinNamespace,
  cancelNamespaceRequest,
  type BrowseEntry,
  type MyNamespaceEntry,
} from "../api";
import { sharedMine } from "../lib/sharedReads";
import { pendingKey } from "../lib/pendingKey";
import { MembersDialog } from "../flows/MembersDialog";
import { NamespaceSettingsDialog } from "../flows/NamespaceSettingsDialog";
import { CreateNamespaceDialog } from "../flows/CreateNamespaceDialog";
import { useStore, useIdentity } from "../lib/state";
import { SkeletonCard } from "../components/Skeleton";
import { Panel } from "../components/ui";
import { EmptyState } from "../components/EmptyState";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { relativeTime, humanizeError, displaySubject, isServiceSubject, isSystemKind } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { oneOf } from "../lib/oneOf";

type JoinRole = "viewer" | "writer" | "admin";

const JOIN_ROLES: Array<{
  value: JoinRole;
  label: string;
  blurb: string;
  icon: typeof Eye;
  pending: "always" | "if-required" | "never";
}> = [
  {
    value: "viewer",
    label: "Viewer",
    blurb: "Read memories. Cannot write or manage. Auto-active for most namespaces.",
    icon: Eye,
    pending: "if-required",
  },
  {
    value: "writer",
    label: "Writer",
    blurb: "Read and contribute memories. Requires approval from an existing admin.",
    icon: UserRound,
    pending: "always",
  },
  {
    value: "admin",
    label: "Admin",
    blurb: "Manage members, settings, deletion. Requires approval from an existing admin.",
    icon: Crown,
    pending: "always",
  },
];

/**
 * Browse — public namespaces in the caller's org.
 *
 * Surfaces `namespace.browse` (list public namespaces) and
 * `namespace.join` (claim a viewer grant on one) so users can discover
 * and opt into shared team workspaces without leaving the dashboard.
 * Namespaces the caller already belongs to are shown as "Joined" with
 * no Join button.
 */
export function Browse() {
  const identity = useIdentity();
  const [rows, setRows] = useState<BrowseEntry[] | null>(null);
  const [mine, setMine] = useState<Set<string>>(new Set());
  // Per-namespace current role so the row can offer "Request upgrade"
  // when the caller is already a member but at a role below admin.
  const [myRole, setMyRole] = useState<Record<string, "viewer" | "writer" | "admin" | "jwt">>({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [adminNs, setAdminNs] = useState<MyNamespaceEntry[]>([]);
  const [query, setQuery] = useState("");
  const [joining, setJoining] = useState<string | null>(null);
  const [requireMap, setRequireMap] = useState<Record<string, boolean>>({});
  const [joinDialog, setJoinDialog] = useState<{ ns: string; row: BrowseEntry } | null>(null);
  const [manageNs, setManageNs] = useState<string | null>(null);
  const [settingsNs, setSettingsNs] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [repoCount, setRepoCount] = useState(0);
  const setView = useStore((s) => s.setView);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    setRows(null);
    Promise.all([
      browseNamespaces(identity.org, { limit: 200, signal }).catch(() => [] as BrowseEntry[]),
      // The shared membership read, not a second list_mine
      sharedMine(identity.org).catch(() => [] as MyNamespaceEntry[]),
    ]).then(([browseRows, mineRows]) => {
      if (!live()) return;
      // Code namespaces are surfaced in the Code section, not as joinable memory.
      setRows(browseRows.filter((r) => r.kind !== "code"));
      setRepoCount(browseRows.filter((r) => r.kind === "code").length);
      setMine(new Set(mineRows.map((r) => r.namespace)));
      setMyRole(Object.fromEntries(mineRows.map((r) => [r.namespace, r.role])));
      setPending(new Set(mineRows.filter((r) => r.pending_role || r.status === "pending").map((r) => r.namespace)));
      // Namespaces I can invite to (i.e. ones I'm admin on, active).
      // Excludes pending-admin requests — until approved I can't act.
      setAdminNs(mineRows.filter((r) => r.role === "admin" && r.status === "active"));
      const map: Record<string, boolean> = {};
      for (const b of browseRows) map[b.namespace] = !!b.requires_approval;
      setRequireMap(map);
    });
  };

  useAbortableEffect(load, [identity.org]);

  const onJoin = async (ns: string, role: JoinRole) => {
    setJoining(ns);
    try {
      const r = await joinNamespace(identity.org, ns, { role });
      const isPending = r.status === "pending";
      if (isPending) {
        const admins = (r.pending_admins || []).slice(0, 3).join(", ") || "an admin";
        toast(
          `You're in as viewer; your ${r.requested_role || role} request on ${ns} is awaiting approval from ${admins}.`,
          "ok",
        );
        setMine((prev) => new Set(prev).add(ns));
        setPending((prev) => new Set(prev).add(ns));
        // Persist to the approval-notification cache so even if the
        // user never visits /me/memories before the admin approves,
        // the next /me bootstrap diff still fires the "approved by …"
        // toast. Same key the App.tsx bootstrap reads/writes.
        try {
          const whoami = useStore.getState().whoami;
          const k = pendingKey(whoami?.subject, identity.org);
          const cur: string[] = JSON.parse(localStorage.getItem(k) || "[]");
          const entry = `${ns}::${r.requested_role || role}`;
          if (!cur.includes(entry)) cur.push(entry);
          localStorage.setItem(k, JSON.stringify(cur));
        } catch {
          /* localStorage may be blocked; not load-bearing */
        }
      } else if (r.joined) {
        toast(`Joined ${ns} as ${role}`, "ok");
        setMine((prev) => new Set(prev).add(ns));
      } else {
        toast(`Already a member of ${ns}`, "info");
      }
      setJoinDialog(null);
    } catch (e) {
      toast(`Join failed: ${humanizeError(e)}`, "error");
    } finally {
      setJoining(null);
    }
  };

  const filtered = (rows || []).filter(
    (r) =>
      !query ||
      r.namespace.toLowerCase().includes(query.toLowerCase()) ||
      (r.description || "").toLowerCase().includes(query.toLowerCase()),
  );

  return (
    <PageContainer>
      <div className="flex items-baseline justify-between mb-2">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <Globe size={22} /> Spaces
        </h1>
      </div>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <p className="text-sm text-[var(--color-text-muted)] mb-6">
        Connect people and namespaces. Browse public namespaces to join, or invite members into namespaces you manage.
      </p>
      {repoCount > 0 && (
        <div className="mb-6 -mt-3 text-xs text-[var(--color-text-muted)]">
          Looking for a code index or a tracker source? Both are joined from{" "}
          <button type="button" onClick={() => setView("code")} className="text-[var(--color-accent)] hover:underline">
            Repositories
          </button>{" "}
          — {repoCount} repositor{repoCount === 1 ? "y" : "ies"} available, each with its issues & PRs when a record
          covers it, read-only.
        </div>
      )}

      {/* SECTION 1: namespaces you admin — proactive invitation surface.
          Hidden when the caller doesn't admin anything. */}
      {adminNs.length > 0 && (
        <section className="mb-8">
          <div className="flex items-baseline justify-between mb-3">
            <h2 className="text-md font-semibold tracking-tight inline-flex items-center gap-2">
              <Crown size={14} className="text-warn" /> Namespaces you manage
            </h2>
            <span className="text-xs text-[var(--color-text-muted)] tabular">
              {adminNs.length} {adminNs.length === 1 ? "namespace" : "namespaces"}
            </span>
          </div>
          <div className="space-y-2">
            {adminNs.map((r) => (
              <div
                key={r.namespace}
                className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-center gap-3"
              >
                <Crown size={16} className="text-warn shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="text-base font-mono text-[var(--color-text)]">{r.namespace}</div>
                  {r.description && (
                    <div className="text-xs text-[var(--color-text-muted)] mt-0.5 truncate">{r.description}</div>
                  )}
                  <div className="mt-1.5 flex items-center gap-3 text-2xs text-[var(--color-text-muted)] tabular">
                    <span
                      className={`text-2xs px-1.5 py-0.5 rounded ${
                        r.visibility === "public" ? "bg-success/15 text-success" : "bg-[var(--color-surface-2)]"
                      }`}
                    >
                      {r.visibility}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => setManageNs(r.namespace)}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md shrink-0"
                  title={`Invite people to ${r.namespace}`}
                >
                  <UserPlus size={13} /> Invite
                </button>
                <button
                  onClick={() => setSettingsNs(r.namespace)}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-sm rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)] shrink-0"
                  title={`Namespace settings for ${r.namespace}`}
                >
                  <SettingsIcon size={13} />
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* SECTION 2: public namespaces in the org to join. */}
      <div className="flex items-baseline justify-between mb-3 gap-3">
        <h2 className="text-md font-semibold tracking-tight inline-flex items-center gap-2">
          <Globe size={14} /> Public namespaces in <span className="font-mono">{identity.org}</span>
        </h2>
        <div className="flex items-center gap-3 text-xs">
          <span className="text-[var(--color-text-muted)] tabular">
            {rows ? `${filtered.length} ${filtered.length === 1 ? "namespace" : "namespaces"}` : ""}
          </span>
          <button
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1 px-2.5 py-1 font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md whitespace-nowrap"
          >
            <Plus size={12} /> Create namespace
          </button>
        </div>
      </div>

      <input
        aria-label="Search namespaces"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search public namespaces, or type an exact private namespace name to request access…"
        className="w-full mb-4 px-3 py-2 text-base rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
      />

      {rows === null && (
        <div className="space-y-2">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {rows !== null && filtered.length === 0 && !query && (
        <EmptyState
          icon={Globe}
          title="No public namespaces yet"
          description="Nobody in your organisation has opened a shared space yet. Until someone does, your own private namespace is the only one you can use."
          details={
            <>
              An admin creates a shared space with <code className="font-mono text-xs">namespace.register</code> and{" "}
              <code className="font-mono text-xs">visibility=public</code>; it then appears here for everyone in the org
              to join.
            </>
          }
        />
      )}

      {rows !== null && filtered.length === 0 && query && (
        <RequestAccessCard
          org={identity.org}
          typedName={query.trim()}
          existingRole={myRole[query.trim()]}
          onSuccess={() => {
            setQuery("");
            load();
          }}
        />
      )}

      {rows !== null && filtered.length > 0 && (
        <div className="space-y-2">
          {filtered.map((r) => {
            const isMember = mine.has(r.namespace);
            return (
              <div
                key={r.namespace}
                className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-start gap-3"
              >
                <Globe size={16} className="text-[var(--color-text-muted)] shrink-0 mt-1" />
                <div className="min-w-0 flex-1">
                  <div className="text-base font-mono text-[var(--color-text)] flex items-center gap-2">
                    {r.namespace}
                    {isSystemKind(r.kind) && (
                      <span className="font-sans text-2xs font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-subtle)]">
                        {r.kind}
                      </span>
                    )}
                  </div>
                  {r.description && (
                    <div className="text-xs text-[var(--color-text-muted)] mt-0.5">{r.description}</div>
                  )}
                  <div className="mt-1.5 flex items-center gap-3 text-2xs text-[var(--color-text-muted)] tabular">
                    <span className="inline-flex items-center gap-1">
                      <Users size={11} /> {r.member_count} member{r.member_count === 1 ? "" : "s"}
                    </span>
                    {r.owner_email &&
                      (isServiceSubject(r.owner_email) ? (
                        <span>shared by {displaySubject(r.owner_email)}</span>
                      ) : (
                        <span>owner {r.owner_email}</span>
                      ))}
                    {r.created_at && <span>created {relativeTime(r.created_at)}</span>}
                  </div>
                </div>
                {isMember && pending.has(r.namespace) ? (
                  <button
                    onClick={async () => {
                      if (
                        !(await confirmAction({
                          title: `Withdraw your request on ${r.namespace}?`,
                          message: "You'll keep any current access; the request just leaves the admin queue.",
                          confirmLabel: "Withdraw",
                        }))
                      )
                        return;
                      try {
                        await cancelNamespaceRequest(identity.org, r.namespace);
                        toast(`Withdrew request on ${r.namespace}`, "ok");
                        load();
                      } catch (e) {
                        toast(`Cancel failed: ${humanizeError(e)}`, "error");
                      }
                    }}
                    title="Click to withdraw the pending request"
                    className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-warn bg-warn/15 rounded-md whitespace-nowrap hover:bg-warn/15"
                  >
                    <ClockIcon size={12} /> Pending
                  </button>
                ) : isMember ? (
                  (() => {
                    const role = myRole[r.namespace];
                    // Already admin: nothing higher to request.
                    if (role === "admin") {
                      return (
                        <span className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] rounded-md">
                          <Check size={12} /> Joined · admin
                        </span>
                      );
                    }
                    // Viewer / writer / jwt: offer to request a higher role.
                    return (
                      <button
                        onClick={() => setJoinDialog({ ns: r.namespace, row: r })}
                        title={`You're a ${role} — request writer or admin`}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] hover:bg-[var(--color-surface)] hover:text-[var(--color-text)] rounded-md whitespace-nowrap"
                      >
                        <Check size={12} /> Joined · {role} — request upgrade
                      </button>
                    );
                  })()
                ) : (
                  <button
                    onClick={() => setJoinDialog({ ns: r.namespace, row: r })}
                    disabled={joining === r.namespace}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50 shadow-sm whitespace-nowrap"
                  >
                    {joining === r.namespace ? "Joining…" : "Join"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      <JoinDialog
        target={joinDialog}
        busy={joining}
        requiresApproval={joinDialog ? !!requireMap[joinDialog.ns] : false}
        currentRole={joinDialog ? myRole[joinDialog.ns] : undefined}
        onCancel={() => setJoinDialog(null)}
        onConfirm={(role) => joinDialog && onJoin(joinDialog.ns, role)}
      />

      <CreateNamespaceDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => {
          setCreateOpen(false);
          load();
        }}
      />

      {manageNs && (
        <MembersDialog
          open={true}
          onOpenChange={(v) => {
            if (!v) {
              setManageNs(null);
              load();
            }
          }}
          org={identity.org}
          namespace={manageNs}
          kind={rows?.find((r) => r.namespace === manageNs)?.kind}
        />
      )}
      {settingsNs && (
        <NamespaceSettingsDialog
          open={true}
          onOpenChange={(v) => {
            if (!v) {
              setSettingsNs(null);
              load();
            }
          }}
          org={identity.org}
          namespace={settingsNs}
        />
      )}
    </PageContainer>
  );
}

function JoinDialog({
  target,
  busy,
  requiresApproval,
  currentRole,
  onCancel,
  onConfirm,
}: {
  target: { ns: string; row: BrowseEntry } | null;
  busy: string | null;
  requiresApproval: boolean;
  /** Caller's current role on `target.ns`. When set, the dialog
   *  switches to "Request elevation" mode: default-selects the
   *  next tier up and re-titles. Undefined for first-time joins. */
  currentRole?: "viewer" | "writer" | "admin" | "jwt";
  onCancel: () => void;
  onConfirm: (role: JoinRole) => void;
}) {
  const isUpgrade = currentRole === "viewer" || currentRole === "writer" || currentRole === "jwt";
  const defaultRole: JoinRole =
    currentRole === "viewer" || currentRole === "jwt" ? "writer" : currentRole === "writer" ? "admin" : "writer";
  const [role, setRole] = useState<JoinRole>(defaultRole);
  // Reset to the right default whenever a new namespace dialog opens.
  useEffect(() => {
    if (target) setRole(defaultRole);
  }, [target?.ns, defaultRole]);
  // Role ranking — mirrors the server's _ROLE_RANK. It is used to (a) hide options at or
  // below the caller's current role in upgrade mode (no point showing "request viewer" to
  // someone already viewer), and (b) decide whether a given option is an actual elevation
  // for the willPend logic below.
  const roleRank: Record<string, number> = { jwt: 0, viewer: 1, writer: 2, admin: 3 };
  const visibleRoles = isUpgrade
    ? JOIN_ROLES.filter((opt) => roleRank[opt.value] > roleRank[currentRole ?? "viewer"])
    : JOIN_ROLES;
  if (!target) return null;
  const willPend = (r: JoinRole) => {
    // Elevation requests need admin approval — but only when the
    // requested role is actually higher than the caller's current
    // role. Requesting a role you already have is a no-op (server
    // returns joined:true immediately). Earlier this branch said
    // "if (isUpgrade) return true" which over-flagged the dialog
    // with "needs approval" badges on roles the user already had.
    if (isUpgrade && currentRole && roleRank[r] > roleRank[currentRole]) return true;
    const pending = JOIN_ROLES.find((x) => x.value === r)?.pending;
    return pending === "always" || (pending === "if-required" && requiresApproval);
  };
  return (
    <Dialog.Root
      open={!!target}
      onOpenChange={(v) => {
        if (!v) onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(520px,92vw)] rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-lg font-semibold tracking-tight">
              {isUpgrade ? "Request elevation on " : "Join "}
              <span className="font-mono">{target.ns}</span>
              {isUpgrade && currentRole && (
                <span className="ml-2 text-xs font-normal text-[var(--color-text-muted)]">
                  (currently {currentRole})
                </span>
              )}
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <XIcon size={16} />
            </Dialog.Close>
          </div>
          <div className="p-6 space-y-2">
            <div className="text-xs text-[var(--color-text-muted)] mb-2">Pick the role you want</div>
            {visibleRoles.map((opt) => {
              const Icon = opt.icon;
              const selected = role === opt.value;
              const pend = willPend(opt.value);
              return (
                <button
                  key={opt.value}
                  onClick={() => setRole(opt.value)}
                  className={`w-full text-left flex items-start gap-3 px-3 py-3 rounded-lg border transition-colors ${
                    selected
                      ? "border-[var(--color-accent)] bg-[var(--color-accent)]/5"
                      : "border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"
                  }`}
                >
                  <span
                    className={`w-4 h-4 mt-0.5 rounded-full border-2 shrink-0 ${selected ? "border-[var(--color-accent)] bg-[var(--color-accent)]" : "border-[var(--color-border-strong)]"}`}
                  >
                    {selected && (
                      <span className="block w-1.5 h-1.5 m-auto mt-[3px] bg-[var(--color-on-accent)] rounded-full" />
                    )}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <Icon size={13} className="text-[var(--color-text-muted)]" />
                      <span className="text-base font-medium">{opt.label}</span>
                      {pend && (
                        <span className="text-2xs px-1.5 py-0.5 rounded bg-warn/15 text-warn inline-flex items-center gap-1">
                          <ClockIcon size={9} /> needs approval
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-[var(--color-text-muted)] mt-0.5">{opt.blurb}</div>
                  </div>
                </button>
              );
            })}
            {willPend(role) && role !== "viewer" && (
              <div className="text-xs text-warn bg-warn/15 rounded-md px-3 py-2 mt-3">
                You'll get <strong>viewer</strong> access immediately so you can read. Your <strong>{role}</strong>{" "}
                request waits in the admin queue — you'll get a notification once it's approved.
              </div>
            )}
            {willPend(role) && role === "viewer" && (
              <div className="text-xs text-warn bg-warn/15 rounded-md px-3 py-2 mt-3">
                This namespace requires admin approval for every join, including read-only. Your request will sit in the
                admin queue until reviewed.
              </div>
            )}
          </div>
          <div className="px-6 py-3.5 border-t border-[var(--color-border)] bg-[var(--color-surface-2)] rounded-b-2xl flex items-center justify-end gap-2">
            <button
              onClick={onCancel}
              className="px-3 py-1.5 text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            >
              Cancel
            </button>
            <button
              onClick={() => onConfirm(role)}
              disabled={busy === target.ns}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
            >
              {busy === target.ns ? "Submitting…" : willPend(role) ? `Request ${role}` : `Join as ${role}`}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// Inline card that appears when the search yields no public matches:
// the caller already typed an exact name into the list search, so we
// reuse that input rather than asking them to type it again.
function RequestAccessCard({
  org,
  typedName,
  existingRole,
  onSuccess,
}: {
  org: string;
  typedName: string;
  existingRole: "viewer" | "writer" | "admin" | "jwt" | undefined;
  onSuccess: () => void;
}) {
  const [role, setRole] = useState<JoinRole>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Reset when the typed name changes so a fresh attempt clears stale
  // errors from a previous submit.
  useEffect(() => {
    setError(null);
  }, [typedName]);

  const roleRank: Record<string, number> = { jwt: 0, viewer: 1, writer: 2, admin: 3 };
  const alreadyHasIt = existingRole !== undefined && roleRank[existingRole] >= roleRank[role];

  const submit = async () => {
    if (!typedName || alreadyHasIt || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await joinNamespace(org, typedName, { role });
      if (r.status === "pending") {
        const admins =
          (r.pending_admins || []).slice(0, 3).join(", ") + ((r.pending_admins?.length || 0) > 3 ? ", …" : "");
        toast(`Requested ${role} on ${typedName}. Pending admin approval${admins ? ` (${admins})` : ""}.`, "ok");
      } else {
        toast(`Joined ${typedName} as ${r.role}.`, "ok");
      }
      onSuccess();
    } catch (e) {
      const raw = e instanceof Error ? e.message : String(e);
      // post() throws "STATUS: <body>" — detect 404 to swap the
      // raw JSON detail for a copy that tells the user what to do.
      if (/^404[:\s]/.test(raw) || /RESOURCE_NOT_FOUND|does not exist/i.test(raw)) {
        setError(
          `No namespace named "${typedName}" in ${org}. Check the spelling, or ask whoever shared it with you to confirm the exact name.`,
        );
      } else {
        setError(humanizeError(e));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel padding="none" className="p-5">
      <div className="flex items-start gap-3">
        <Globe size={18} className="text-[var(--color-text-muted)] mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-base font-medium">
            No public namespace matching <span className="font-mono">{typedName}</span>.
          </div>
          <div className="text-xs text-[var(--color-text-muted)] mt-1">
            If <span className="font-mono">{typedName}</span> is a private namespace someone told you about, request
            access — an admin of that namespace will see and approve.
          </div>
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            <label className="text-xs text-[var(--color-text-muted)]">
              Role
              <select
                value={role}
                onChange={(e) => {
                  const v = oneOf(
                    JOIN_ROLES.map((o) => o.value),
                    e.target.value,
                  );
                  if (v !== null) setRole(v);
                }}
                className="ml-2 px-2.5 py-1.5 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
              >
                {JOIN_ROLES.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              onClick={submit}
              disabled={busy || alreadyHasIt}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50"
            >
              {busy ? "Submitting…" : `Request access`}
            </button>
          </div>
          {alreadyHasIt && (
            <div className="text-xs text-warn bg-warn/15 rounded-md px-3 py-2 mt-3">
              You're already <strong>{existingRole}</strong> on <span className="font-mono">{typedName}</span> — that's
              at least {role}. Pick a higher role above to request an upgrade.
            </div>
          )}
          {error && <div className="text-xs text-[var(--color-danger)] mt-3">{error}</div>}
        </div>
      </div>
    </Panel>
  );
}
