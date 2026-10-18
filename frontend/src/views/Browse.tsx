import { useEffect, useState, type ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import {
  browseNamespaces,
  joinNamespace,
  cancelNamespaceRequest,
  leaveNamespace,
  type BrowseEntry,
  type MyNamespaceEntry,
} from "../api";
import { sharedMine } from "../lib/sharedReads";
import { pendingKey } from "../lib/pendingKey";
import { MembersDialog } from "../flows/MembersDialog";
import { NamespaceSettingsDialog } from "../flows/NamespaceSettingsDialog";
import { CreateNamespaceDialog } from "../flows/CreateNamespaceDialog";
import { useStore, useIdentity } from "../lib/state";
import { RowSkeleton } from "../components/Skeleton";
import { Alert, ListRow, LoadingSkeleton, PageHeader, Panel, SectionHeading, Tip } from "../components/ui";
import { ListState } from "../components/ui/ListState";
import { ListToolbar } from "../components/ui/ListToolbar";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { useRowNav } from "../lib/useRowNav";
import { useFold } from "../lib/folds";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { humanizeError, isSystemKind, sentenceCase } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { oneOf } from "../lib/oneOf";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";

const isCodeKind = (kind: string | undefined) => kind === "code";
const isTrackerKind = (kind: string | undefined) => kind === "qa" || kind === "tracker";

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
  const phone = useMedia(PHONE_QUERY);
  const [rows, setRows] = useState<BrowseEntry[] | null>(null);
  // The browse read failed: its error, in place of the public list; Retry reads again.
  const [error, setError] = useState<unknown>(null);
  const [reads, setReads] = useState(0);
  // A reload after a join or a create keeps the list on screen and marks it busy.
  const [refreshing, setRefreshing] = useState(false);
  const [mine, setMine] = useState<Set<string>>(new Set());
  // Per-namespace current role so the row can offer "Request upgrade"
  // when the caller is already a member but at a role below admin.
  const [myRole, setMyRole] = useState<Record<string, "viewer" | "writer" | "admin" | "jwt">>({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  // Namespaces I can invite to (admin, active), memory ones and code / tracker ones apart.
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
  const whoami = useStore((s) => s.whoami);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    setRefreshing(true);
    let failed: unknown = null;
    Promise.all([
      browseNamespaces(identity.org, { limit: 200, signal }).catch((e: unknown) => {
        failed = e;
        return [] as BrowseEntry[];
      }),
      // The shared membership read, not a second list_mine
      sharedMine(identity.org).catch(() => [] as MyNamespaceEntry[]),
    ]).then(([browseRows, mineRows]) => {
      if (!live()) return;
      setRefreshing(false);
      setError(failed);
      // Code namespaces are surfaced in the Code section, not as joinable memory.
      setRows(failed === null ? browseRows.filter((r) => r.kind !== "code") : null);
      setRepoCount(browseRows.filter((r) => r.kind === "code").length);
      setMine(new Set(mineRows.map((r) => r.namespace)));
      setMyRole(Object.fromEntries(mineRows.map((r) => [r.namespace, r.role])));
      setPending(new Set(mineRows.filter((r) => r.pending_role || r.status === "pending").map((r) => r.namespace)));
      // Excludes pending-admin requests — until approved I can't act.
      setAdminNs(mineRows.filter((r) => r.role === "admin" && r.status === "active"));
      const map: Record<string, boolean> = {};
      for (const b of browseRows) map[b.namespace] = !!b.requires_approval;
      setRequireMap(map);
    });
  };

  // Another org's list is not this one's: start from the skeleton.
  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      setError(null);
      load(signal, live);
    },
    [identity.org, reads],
  );

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

  // A code index is kind=code and a tracker record kind=qa or tracker, as the server says.
  const managedMemory = adminNs.filter((r) => !isCodeKind(r.kind) && !isTrackerKind(r.kind));
  const managedCode = adminNs.filter((r) => isCodeKind(r.kind));
  const managedTracker = adminNs.filter((r) => isTrackerKind(r.kind));
  const repoHint = repoCount > 0 && (
    <div className="text-xs text-text-muted">
      Looking for a code index or a tracker source? Both are joined from{" "}
      <Link component="button" type="button" size="sm" onClick={() => setView("code")}>
        Repositories
      </Link>{" "}
      — {repoCount} repositor{repoCount === 1 ? "y" : "ies"} available, each with its issues & PRs when a record covers
      it, read-only.
    </div>
  );
  const managedProps = {
    subject: whoami?.subject ?? identity.org,
    onInvite: setManageNs,
    onSettings: setSettingsNs,
    onRepositories: () => setView("code"),
  };

  const matches = (namespace: string, description: string | undefined) =>
    !query ||
    namespace.toLowerCase().includes(query.toLowerCase()) ||
    (description || "").toLowerCase().includes(query.toLowerCase());
  const filtered = (rows || []).filter((r) => matches(r.namespace, r.description));
  const managedShown = managedMemory.filter((r) => matches(r.namespace, r.description));
  const memberCount = (ns: string) => rows?.find((r) => r.namespace === ns)?.member_count;
  const dialogOpen = !!(joinDialog || manageNs || settingsNs || createOpen);
  // The rows of both sections are one tab stop; the arrows move through them.
  const nav = useRowNav({ enabled: !dialogOpen && filtered.length + managedShown.length > 0 });

  const withdraw = async (ns: string) => {
    if (
      !(await confirmAction({
        title: `Withdraw your request on ${ns}?`,
        message: "You'll keep any current access; the request just leaves the admin queue.",
        confirmLabel: "Withdraw",
      }))
    )
      return;
    try {
      await cancelNamespaceRequest(identity.org, ns);
      toast(`Withdrew request on ${ns}`, "ok");
      load();
    } catch (e) {
      toast(`Cancel failed: ${humanizeError(e)}`, "error");
    }
  };
  const leave = async (ns: string) => {
    if (
      !(await confirmAction({
        title: `Leave ${ns}?`,
        message: "You stop reading its memories, and new tokens no longer include it. An admin can add you again.",
        confirmLabel: "Leave",
        danger: true,
      }))
    )
      return;
    try {
      await leaveNamespace(identity.org, ns);
      toast(`Left ${ns}`, "ok");
      load();
    } catch (e) {
      toast(`Leave failed: ${humanizeError(e)}`, "error");
    }
  };
  /** A row's More menu: Settings where you manage it, Copy name, Leave where you belong. */
  const menuFor = (ns: string, admin: boolean, member: boolean): RowMenuItem[] => [
    ...(admin ? [{ label: "Settings", icon: "gear" as const, onSelect: () => setSettingsNs(ns) }] : []),
    {
      label: "Copy name",
      icon: "copy",
      onSelect: () =>
        void navigator.clipboard
          ?.writeText(ns)
          .then(() => toast(`Copied ${ns}`, "ok"))
          .catch(() => toast("Couldn't copy the name", "error")),
    },
    ...(member ? [{ label: "Leave", icon: "cross" as const, danger: true, onSelect: () => void leave(ns) }] : []),
  ];
  const invite = (ns: string) => (
    <Tip content={`Invite people to ${ns}`}>
      <Button
        type="secondary"
        htmlType="button"
        iconLeft="users"
        aria-label={`Invite people to ${ns}`}
        onClick={() => setManageNs(ns)}
      >
        Invite
      </Button>
    </Tip>
  );

  return (
    <PageContainer>
      <PageHeader
        title="Spaces"
        actions={
          <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setCreateOpen(true)}>
            Create namespace
          </Button>
        }
      >
        {/* The phone's top bar names the namespace; the title row keeps its action beside it. */}
        <span className="hidden sm:contents">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </span>
      </PageHeader>
      <p className="text-sm text-text-muted mb-3">
        Join public namespaces in your org, or invite people into the ones you manage.
      </p>
      {/* The Repositories hint: under the description, or inside the folded code section when there is one. */}
      {managedCode.length === 0 && repoHint && <div className="mb-3">{repoHint}</div>}

      <ListToolbar
        label="Namespaces"
        count={rows ? `${filtered.length} ${filtered.length === 1 ? "namespace" : "namespaces"}` : undefined}
      >
        <div className="min-w-0 flex-1">
          <SearchField
            aria-label="Search namespaces"
            value={query}
            onChange={(value) => setQuery(value)}
            // A phone's field holds the short form only.
            placeholder={
              phone
                ? "Search namespaces"
                : "Search namespaces, or type an exact private namespace name to request access…"
            }
          />
        </div>
      </ListToolbar>

      <div ref={nav}>
        {/* SECTION 1: namespaces you admin — proactive invitation surface.
            Hidden when the caller doesn't admin anything. */}
        {managedShown.length > 0 && (
          <section className="mb-6">
            <SectionHeading level={2} label="Namespaces you manage" count={managedShown.length} />
            <div className="mt-2 space-y-2">
              {managedShown.map((r) => (
                <SpaceRow
                  key={r.namespace}
                  namespace={r.namespace}
                  visibility={r.visibility}
                  description={r.description}
                  members={memberCount(r.namespace)}
                  onOpen={() => setSettingsNs(r.namespace)}
                  action={invite(r.namespace)}
                  menu={menuFor(r.namespace, true, true)}
                />
              ))}
            </div>
          </section>
        )}

        {/* SECTION 2: public namespaces in the org to join. */}
        <section aria-labelledby="spaces-public-heading">
          <SectionHeading
            level={2}
            id="spaces-public-heading"
            label="Public namespaces"
            caption={`in ${identity.org}`}
            className="mb-2"
          />

          {error !== null && (
            <ListState kind="error" things="namespaces" error={error} onRetry={() => setReads((n) => n + 1)} />
          )}

          {rows === null && error === null && (
            <LoadingSkeleton label="Loading namespaces">
              <div className="space-y-2">
                <RowSkeleton />
                <RowSkeleton />
                <RowSkeleton />
              </div>
            </LoadingSkeleton>
          )}

          {rows !== null && filtered.length === 0 && !query && (
            <ListState
              kind="empty"
              icon="globe"
              title="No public namespaces yet"
              description="Nobody in your org has opened a shared space yet. Until someone does, your own private namespace is the only one you can use."
              details={
                <>
                  An admin creates a shared space with <code className="code-inline text-xs">namespace.register</code>{" "}
                  and <code className="code-inline text-xs">visibility=public</code>; it then appears here for everyone
                  in the org to join.
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
            <div className="space-y-2" data-testid="browse-list" aria-busy={refreshing || undefined}>
              {filtered.map((r) => {
                const isMember = mine.has(r.namespace);
                const role = myRole[r.namespace];
                const isPending = isMember && pending.has(r.namespace);
                const isAdmin = isMember && role === "admin";
                const open = () => {
                  if (isPending) void withdraw(r.namespace);
                  else if (isAdmin) setSettingsNs(r.namespace);
                  else setJoinDialog({ ns: r.namespace, row: r });
                };
                return (
                  <SpaceRow
                    key={r.namespace}
                    namespace={r.namespace}
                    visibility={r.visibility ?? "public"}
                    kind={isSystemKind(r.kind) ? r.kind : undefined}
                    description={r.description}
                    members={r.member_count}
                    onOpen={open}
                    action={
                      isPending ? (
                        <Tip content="Your request is waiting for an admin">
                          <Button type="secondary" htmlType="button" iconLeft="clock" onClick={open}>
                            Withdraw request
                          </Button>
                        </Tip>
                      ) : isAdmin ? (
                        invite(r.namespace)
                      ) : isMember ? (
                        <Tip content={`You're a ${role} — request writer or admin`}>
                          <Button type="secondary" htmlType="button" onClick={open}>
                            Request upgrade
                          </Button>
                        </Tip>
                      ) : (
                        <Button type="secondary" htmlType="button" loading={joining === r.namespace} onClick={open}>
                          Join
                        </Button>
                      )
                    }
                    menu={menuFor(r.namespace, isAdmin, isMember && !isPending)}
                  />
                );
              })}
            </div>
          )}
        </section>
      </div>

      <ManagedKindSection kind="code" rows={managedCode} hint={repoHint} {...managedProps} />
      <ManagedKindSection kind="tracker" rows={managedTracker} {...managedProps} />

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
          kind={(rows?.find((r) => r.namespace === manageNs) ?? adminNs.find((r) => r.namespace === manageNs))?.kind}
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

/**
 * One space: line 1 the namespace in mono and its visibility, line 2 its
 * description; at the right its member count, the one inline action and the More menu.
 * The name is the row's open button (`data-row-open`), stretched over the row.
 */
function SpaceRow({
  namespace,
  visibility,
  kind,
  description,
  members,
  onOpen,
  action,
  menu,
}: {
  namespace: string;
  visibility: string;
  kind?: string;
  description?: string;
  members?: number;
  onOpen: () => void;
  action: ReactNode;
  menu: RowMenuItem[];
}) {
  return (
    // Back to this page focuses the space that had the focus.
    <ListRow data-focus-key={`space:${namespace}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-60">
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              data-row-open=""
              onClick={onOpen}
              className="list-row-open min-w-0 break-all font-mono text-sm text-text"
            >
              <code>{namespace}</code>
            </button>
            {/* Visibility is not a status: neutral either way. */}
            <Badge text={sentenceCase(visibility)} state="neutral" size="sm" />
            {kind && <Badge text={sentenceCase(kind)} state="neutral" size="sm" />}
          </div>
          {description && <p className="mt-0.5 truncate text-xs text-text-muted">{description}</p>}
        </div>
        <div className="list-row-control ml-auto flex shrink-0 items-center gap-2">
          {members !== undefined && (
            <span className="whitespace-nowrap text-xs text-text-muted tabular">
              {members} member{members === 1 ? "" : "s"}
            </span>
          )}
          {action}
          <RowMenu name={namespace} items={menu} />
        </div>
      </div>
    </ListRow>
  );
}

const MANAGED = {
  code: { label: "Code indexes you manage", icon: "code", link: "Manage indexing in Repositories" },
  tracker: { label: "Tracker records you manage", icon: "flag", link: "Manage tracker sources in Repositories" },
} as const;

/**
 * The code indexes (or tracker records) someone manages: a section at the bottom of the
 * page, folded until opened (remembered per person in this browser), drawn only when it
 * has rows. Each row is the repository first and the namespace in mono under it.
 */
function ManagedKindSection({
  kind,
  rows,
  hint,
  subject,
  onInvite,
  onSettings,
  onRepositories,
}: {
  kind: keyof typeof MANAGED;
  rows: MyNamespaceEntry[];
  hint?: ReactNode;
  subject: string;
  onInvite: (ns: string) => void;
  onSettings: (ns: string) => void;
  onRepositories: () => void;
}) {
  const meta = MANAGED[kind];
  const [open, toggle] = useFold(`spaces-${kind}-${subject}`);
  const bodyId = `managed-${kind}-body`;
  if (rows.length === 0) return null;
  return (
    <section className="mt-8">
      <SectionHeading
        level={2}
        label={meta.label}
        count={rows.length}
        fold={{ open, onToggle: toggle, controls: bodyId }}
      />
      <div id={bodyId} hidden={!open} className="mt-3 space-y-2">
        {open && (
          <>
            {hint}
            {rows.map((r) => (
              <ListRow
                key={r.namespace}
                data-focus-key={`space:${r.namespace}`}
                leading={<Icon name={meta.icon} size="sm" aria-hidden="true" />}
              >
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-text truncate">{r.description || r.namespace}</div>
                    {r.description && (
                      <code className="block text-xs font-mono text-text-muted truncate">{r.namespace}</code>
                    )}
                  </div>
                  <Badge text={sentenceCase(r.visibility)} state="neutral" size="sm" />
                  <Tip content={`Invite people to ${r.namespace}`}>
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="users"
                      aria-label={`Invite people to ${r.namespace}`}
                      onClick={() => onInvite(r.namespace)}
                      className="shrink-0"
                    >
                      Invite
                    </Button>
                  </Tip>
                  <RowMenu
                    name={r.namespace}
                    items={[{ label: "Settings", icon: "gear", onSelect: () => onSettings(r.namespace) }]}
                  />
                </div>
              </ListRow>
            ))}
            <Link component="button" type="button" size="sm" onClick={onRepositories}>
              {meta.link}
            </Link>
          </>
        )}
      </div>
    </section>
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
