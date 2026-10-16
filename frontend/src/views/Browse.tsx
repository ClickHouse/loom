import { useEffect, useState } from "react";
import { Alert } from "@clickhouse/click-ui/Alert";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
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
import { Panel, Tip } from "../components/ui";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { IconButton } from "../components/IconButton";
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
  pending: "always" | "if-required" | "never";
}> = [
  {
    value: "viewer",
    label: "Viewer",
    blurb: "Read memories. Cannot write or manage. Auto-active for most namespaces.",
    pending: "if-required",
  },
  {
    value: "writer",
    label: "Writer",
    blurb: "Read and contribute memories. Requires approval from an existing admin.",
    pending: "always",
  },
  {
    value: "admin",
    label: "Admin",
    blurb: "Manage members, settings, deletion. Requires approval from an existing admin.",
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
        // user never visits /memories before the admin approves,
        // the next dashboard bootstrap diff still fires the "approved by …"
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
        <h1 className="text-2xl font-semibold tracking-tight inline-flex items-center gap-2">
          <Icon name="globe" size="lg" aria-hidden="true" /> Spaces
        </h1>
      </div>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <p className="text-sm text-text-muted mb-6">
        Connect people and namespaces. Browse public namespaces to join, or invite members into namespaces you manage.
      </p>
      {repoCount > 0 && (
        <div className="mb-6 -mt-3 text-xs text-text-muted">
          Looking for a code index or a tracker source? Both are joined from{" "}
          <button type="button" onClick={() => setView("code")} className="text-link hover:underline">
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
            <h2 className="text-lg font-semibold tracking-tight inline-flex items-center gap-2">
              <Icon name="star" size="sm" color="var(--color-warn)" aria-hidden="true" /> Namespaces you manage
            </h2>
            <span className="text-xs text-text-muted tabular">
              {adminNs.length} {adminNs.length === 1 ? "namespace" : "namespaces"}
            </span>
          </div>
          <div className="space-y-2">
            {adminNs.map((r) => (
              <div
                key={r.namespace}
                className="p-3.5 rounded-xl border border-border bg-surface flex items-center gap-3"
              >
                <Icon name="star" size="sm" color="var(--color-warn)" aria-hidden="true" className="shrink-0" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-mono text-text">{r.namespace}</div>
                  {r.description && <div className="text-xs text-text-muted mt-0.5 truncate">{r.description}</div>}
                  <div className="mt-1.5 flex items-center gap-3 text-2xs text-text-muted tabular">
                    <Badge text={r.visibility} state={r.visibility === "public" ? "success" : "neutral"} size="sm" />
                  </div>
                </div>
                <Tip content={`Invite people to ${r.namespace}`}>
                  <Button
                    type="secondary"
                    htmlType="button"
                    iconLeft="users"
                    onClick={() => setManageNs(r.namespace)}
                    className="shrink-0"
                  >
                    Invite
                  </Button>
                </Tip>
                <IconButton
                  icon="gear"
                  label={`Namespace settings for ${r.namespace}`}
                  onClick={() => setSettingsNs(r.namespace)}
                  className="shrink-0"
                />
              </div>
            ))}
          </div>
        </section>
      )}

      {/* SECTION 2: public namespaces in the org to join. */}
      <div className="flex items-baseline justify-between mb-3 gap-3">
        <h2 className="text-lg font-semibold tracking-tight inline-flex items-center gap-2">
          <Icon name="globe" size="sm" aria-hidden="true" /> Public namespaces in{" "}
          <span className="font-mono">{identity.org}</span>
        </h2>
        <div className="flex items-center gap-3 text-xs">
          <span className="text-text-muted tabular">
            {rows ? `${filtered.length} ${filtered.length === 1 ? "namespace" : "namespaces"}` : ""}
          </span>
          <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setCreateOpen(true)}>
            Create namespace
          </Button>
        </div>
      </div>

      <div className="mb-4">
        <SearchField
          aria-label="Search namespaces"
          value={query}
          onChange={(value) => setQuery(value)}
          placeholder="Search public namespaces, or type an exact private namespace name to request access…"
        />
      </div>

      {rows === null && (
        <div className="space-y-2">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {rows !== null && filtered.length === 0 && !query && (
        <EmptyState
          icon="globe"
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
                className="p-3.5 rounded-xl border border-border bg-surface flex items-start gap-3"
              >
                <Icon name="globe" size="sm" aria-hidden="true" className="shrink-0 mt-1" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-mono text-text flex items-center gap-2">
                    {r.namespace}
                    {isSystemKind(r.kind) && <Badge text={r.kind} state="neutral" size="sm" />}
                  </div>
                  {r.description && <div className="text-xs text-text-muted mt-0.5">{r.description}</div>}
                  <div className="mt-1.5 flex items-center gap-3 text-2xs text-text-muted tabular">
                    <span className="inline-flex items-center gap-1">
                      <Icon name="users" size="xs" aria-hidden="true" /> {r.member_count} member
                      {r.member_count === 1 ? "" : "s"}
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
                  <Tip content="Withdraw the pending request">
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="clock"
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
                    >
                      Pending
                    </Button>
                  </Tip>
                ) : isMember ? (
                  (() => {
                    const role = myRole[r.namespace];
                    // Already admin: nothing higher to request.
                    if (role === "admin") {
                      return <Badge text="Joined · admin" state="neutral" />;
                    }
                    // Viewer / writer / jwt: offer to request a higher role.
                    return (
                      <Tip content={`You're a ${role} — request writer or admin`}>
                        <Button
                          type="secondary"
                          htmlType="button"
                          iconLeft="check"
                          onClick={() => setJoinDialog({ ns: r.namespace, row: r })}
                        >
                          Joined · {role} — request upgrade
                        </Button>
                      </Tip>
                    );
                  })()
                ) : (
                  <Button
                    type="secondary"
                    htmlType="button"
                    loading={joining === r.namespace}
                    onClick={() => setJoinDialog({ ns: r.namespace, row: r })}
                  >
                    Join
                  </Button>
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
    <Modal
      open={!!target}
      onOpenChange={(v) => {
        if (!v) onCancel();
      }}
      title={`${isUpgrade ? "Request elevation on" : "Join"} ${target.ns}`}
      description={
        isUpgrade && currentRole
          ? `You are currently ${currentRole}. Pick the role you want.`
          : "Pick the role you want."
      }
      className="max-w-[520px] w-full"
    >
      <ChoiceGroup
        label="Role"
        value={role}
        onChange={setRole}
        options={visibleRoles.map((opt) => ({
          value: opt.value,
          label: opt.label,
          hint: willPend(opt.value) ? `${opt.blurb} Needs approval.` : opt.blurb,
        }))}
      />
      {willPend(role) && role !== "viewer" && (
        <Alert
          className="mt-3"
          state="warning"
          size="small"
          text={
            <>
              You'll get <strong>viewer</strong> access immediately so you can read. Your <strong>{role}</strong>{" "}
              request waits in the admin queue — you'll get a notification once it's approved.
            </>
          }
        />
      )}
      {willPend(role) && role === "viewer" && (
        <Alert
          className="mt-3"
          state="warning"
          size="small"
          text="This namespace requires admin approval for every join, including read-only. Your request will sit in the admin queue until reviewed."
        />
      )}
      <ModalFooter>
        <Button type="secondary" htmlType="button" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="primary" htmlType="button" loading={busy === target.ns} onClick={() => onConfirm(role)}>
          {willPend(role) ? `Request ${role}` : `Join as ${role}`}
        </Button>
      </ModalFooter>
    </Modal>
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
        <Icon name="globe" size="md" aria-hidden="true" className="mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium">
            No public namespace matching <span className="font-mono">{typedName}</span>.
          </div>
          <div className="text-xs text-text-muted mt-1">
            If <span className="font-mono">{typedName}</span> is a private namespace someone told you about, request
            access — an admin of that namespace will see and approve.
          </div>
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            <Select
              label="Role"
              orientation="horizontal"
              value={role}
              onSelect={(value) => {
                const v = oneOf(
                  JOIN_ROLES.map((o) => o.value),
                  value,
                );
                if (v !== null) setRole(v);
              }}
              className="w-56"
            >
              {JOIN_ROLES.map((o) => (
                <Select.Item key={o.value} value={o.value}>
                  {o.label}
                </Select.Item>
              ))}
            </Select>
            <Button type="primary" htmlType="button" loading={busy} disabled={alreadyHasIt} onClick={submit}>
              Request access
            </Button>
          </div>
          {alreadyHasIt && (
            <Alert
              className="mt-3"
              state="warning"
              size="small"
              text={
                <>
                  You're already <strong>{existingRole}</strong> on <span className="font-mono">{typedName}</span> —
                  that's at least {role}. Pick a higher role above to request an upgrade.
                </>
              }
            />
          )}
          {error && <div className="text-xs text-danger mt-3">{error}</div>}
        </div>
      </div>
    </Panel>
  );
}
