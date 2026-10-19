import { lazy, Suspense, useEffect, useId, useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { Title } from "@clickhouse/click-ui/Title";
import { IconButton } from "./IconButton";
import { listPendingApprovals, type GrantRole } from "../api";
import { sharedCodeRepos } from "../lib/sharedReads";
import { useVisiblePoll } from "../lib/hooks";
import { useAfterPaint } from "../lib/afterPaint";
import { useStore } from "../lib/state";
import { canCallCodeRepos } from "../lib/canCall";
// 10.5 kB that opens on a click loads on the click.
const MembersDialog = lazy(() => import("../flows/MembersDialog").then((m) => ({ default: m.MembersDialog })));
import { relativeTime, displaySubject, isServiceSubject } from "../lib/format";
import { PopoverTrigger } from "./ui/Menu";

interface AdminPending {
  namespace: string;
  kind?: string;
  subject: string;
  requested_role: GrantRole;
  current_role: GrantRole;
  pending_since: string | null;
}

export function PendingApprovalsBell() {
  const headingId = useId();
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const [items, setItems] = useState<AdminPending[]>([]);
  const [open, setOpen] = useState(false);
  const [manageNs, setManageNs] = useState<string | null>(null);
  const [manageKind, setManageKind] = useState<string | undefined>(undefined);
  const [manageLabel, setManageLabel] = useState<string | null>(null);
  const [managePendingElsewhere, setManagePendingElsewhere] = useState<
    Record<string, { namespace: string; label: string }[]>
  >({});

  // One org-level call answers "what can I approve", instead of namespace.list_mine and
  // then grants.list once per administered namespace on every poll in every tab. The
  // server already excludes the caller's own requests (they live in
  // PendingRequestBanner).
  const refresh = async () => {
    if (!identity) return;
    try {
      const r = await listPendingApprovals(identity.org, identity.namespace);
      const collected: AdminPending[] = (r.rows || []).map((g) => ({
        namespace: g.namespace,
        kind: g.kind,
        subject: g.subject,
        requested_role: g.requested_role,
        current_role: g.current_role,
        pending_since: g.pending_since,
      }));
      setItems(collected);
    } catch {
      /* fail-quiet */
    }
  };

  // Branches of one repository are one entry per person. Only a caller with standing on a
  // code namespace (or an admin claim) asks for the repositories; anyone else keeps the
  // namespace identifier as the label and sends nothing.
  const myNamespaces = useStore((s) => s.myNamespaces);
  const reposReadable = canCallCodeRepos(myNamespaces, whoami);
  const [repoInfo, setRepoInfo] = useState<Record<string, { repo: string; branch: string }>>({});
  // The repositories are asked for only when there is a code-namespace request to name,
  // not on every page load for a popover that is usually empty and closed.
  const hasCodeItems = items.some((it) => it.kind === "code");
  useEffect(() => {
    if (!identity?.org || !reposReadable || !hasCodeItems) return;
    sharedCodeRepos(identity.org)
      .then((rows) =>
        setRepoInfo(Object.fromEntries(rows.map((r) => [r.namespace, { repo: r.repo, branch: r.branch }]))),
      )
      .catch(() => {});
  }, [identity?.org, reposReadable, hasCodeItems]);
  const grouped = useMemo(() => {
    const out: (AdminPending & { branches?: string[] })[] = [];
    const seen = new Map<string, AdminPending & { branches?: string[] }>();
    for (const it of items) {
      if (it.kind !== "code") {
        out.push(it);
        continue;
      }
      const repo = repoInfo[it.namespace]?.repo ?? it.namespace;
      const key = `${it.subject}::${repo}::${it.requested_role}`;
      const prev = seen.get(key);
      if (prev) {
        (prev.branches ??= [repoInfo[prev.namespace]?.branch ?? prev.namespace]).push(
          repoInfo[it.namespace]?.branch ?? it.namespace,
        );
        continue;
      }
      const entry = { ...it };
      seen.set(key, entry);
      out.push(entry);
    }
    return out;
  }, [items, repoInfo]);
  /** "owner/name @ branch" for a code namespace the caller may see the
   *  repositories of; the namespace identifier otherwise. */
  const repoLabel = (ns: string): string => {
    const r = repoInfo[ns];
    return r ? `${r.repo} @ ${r.branch}` : ns;
  };
  /** For the dialog on `ns`, every other branch of the same repository where each
   *  requester is also waiting — one Approve or Reject there covers them all. */
  const pendingElsewhereFor = (ns: string): Record<string, { namespace: string; label: string }[]> => {
    const repo = repoInfo[ns]?.repo;
    if (!repo) return {};
    const out: Record<string, { namespace: string; label: string }[]> = {};
    for (const it of items) {
      if (it.kind !== "code" || it.namespace === ns || repoInfo[it.namespace]?.repo !== repo) continue;
      (out[it.subject] ??= []).push({ namespace: it.namespace, label: repoLabel(it.namespace) });
    }
    return out;
  };
  // The read is org-scoped: a namespace switch changes nothing here. Every
  // two minutes while the tab is visible; a hidden tab catches up on return.
  // The first read leaves after the page's first paint, on idle — an approvals count is
  // not what the person is waiting for.
  useAfterPaint(
    () => {
      void refresh();
    },
    [identity?.org, whoami?.subject],
    !!identity,
  );
  useVisiblePoll(refresh, 120_000, !!identity);

  if (!identity) return null;

  const tip =
    items.length > 0 ? `${items.length} pending approval${items.length === 1 ? "" : "s"}` : "No pending approvals";
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <span className="relative inline-flex">
        <PopoverTrigger>
          <IconButton
            icon="bell"
            label={items.length > 0 ? `Pending approvals, ${items.length}` : "Pending approvals"}
            tooltip={tip}
          />
        </PopoverTrigger>
        {items.length > 0 && <CountBadge count={items.length} />}
      </span>
      <Popover.Content
        align="end"
        aria-labelledby={headingId}
        className="w-[min(440px,92vw)] max-h-[480px] overflow-y-auto"
      >
        <Title type="h2" size="sm" id={headingId} className="pb-2 border-b border-border">
          Pending approvals · {items.length}
        </Title>
        {items.length === 0 && (
          <div className="py-6 text-sm text-text-muted text-center">
            No one's waiting for your approval.
            <div className="text-xs text-text-muted mt-1.5">
              Join requests on namespaces you admin show up here. To invite someone proactively, head to Spaces →
              Namespaces you manage.
            </div>
            <Button
              type="secondary"
              htmlType="button"
              className="mt-3"
              onClick={() => {
                setOpen(false);
                useStore.getState().setView("browse");
              }}
            >
              Open Spaces
            </Button>
          </div>
        )}
        {grouped.map((it) => (
          <button
            type="button"
            key={`${it.namespace}::${it.subject}`}
            onClick={() => {
              setOpen(false);
              setManageKind(it.kind);
              setManageLabel(it.kind === "code" ? repoLabel(it.namespace) : null);
              setManagePendingElsewhere(it.kind === "code" ? pendingElsewhereFor(it.namespace) : {});
              setManageNs(it.namespace);
            }}
            className="text-left -mx-2 w-[calc(100%+1rem)] px-2 py-2.5 rounded-sm hover:bg-surface-2 flex items-start gap-3 transition-colors"
          >
            <RoleIcon role={it.requested_role} />
            <div className="flex-1 min-w-0">
              {/* The target is what the approver has to read, so it gets
                    its own line and wraps; nothing here is ever truncated. */}
              <div className="text-sm text-text break-words">
                <span className={isServiceSubject(it.subject) ? "font-medium" : "font-mono break-all"}>
                  {displaySubject(it.subject)}
                </span>{" "}
                {it.kind === "code" ? (
                  <>
                    asks to <span className="font-medium">{it.requested_role === "admin" ? "maintain" : "read"}</span>
                  </>
                ) : (
                  <>
                    wants <span className="font-medium">{it.requested_role}</span> on
                  </>
                )}
              </div>
              <div className="text-sm font-medium text-text break-words">
                {it.kind === "code" ? (
                  <>
                    {repoInfo[it.namespace]?.repo ?? it.namespace}
                    {it.branches && it.branches.length > 1 ? (
                      <span className="text-text-muted font-normal">
                        {" "}
                        · {it.branches.length} branches ({it.branches.join(", ")}) — one decision
                      </span>
                    ) : (
                      <span className="text-text-muted font-normal">
                        {" "}
                        @ {repoInfo[it.namespace]?.branch ?? it.namespace}
                      </span>
                    )}
                  </>
                ) : (
                  <span className="font-mono break-all">{it.namespace}</span>
                )}
              </div>
              {isServiceSubject(it.subject) && (
                <div className="text-xs font-mono text-text-muted break-all">{it.subject}</div>
              )}
              {it.kind === "code" && repoInfo[it.namespace] && (
                <div className="text-xs font-mono text-text-muted break-all">{it.namespace}</div>
              )}
              <div className="text-xs text-text-muted mt-0.5">
                {it.current_role !== it.requested_role && <>currently {it.current_role} · </>}
                {it.pending_since && <>requested {relativeTime(it.pending_since)}</>}
              </div>
            </div>
            <span className="text-xs text-text-muted mt-1 shrink-0 inline-flex items-center gap-1">
              Review <Icon name="arrow-right" size="xs" aria-hidden="true" />
            </span>
          </button>
        ))}
      </Popover.Content>
      {manageNs && (
        <Suspense fallback={null}>
          <MembersDialog
            open={true}
            onOpenChange={(v) => {
              if (!v) {
                setManageNs(null);
                refresh();
              }
            }}
            org={identity.org}
            namespace={manageNs}
            kind={manageKind}
            label={manageLabel ?? undefined}
            pendingElsewhere={managePendingElsewhere}
          />
        </Suspense>
      )}
    </Popover>
  );
}

function RoleIcon({ role }: { role: GrantRole }) {
  if (role === "admin")
    return <Icon name="star" size="xs" color="var(--global-color-text-muted)" aria-label="Admin" className="mt-1" />;
  if (role === "writer") return <Icon name="user" size="xs" aria-label="Writer" className="mt-1" />;
  return <Icon name="eye" size="xs" color="var(--color-text-muted)" aria-label="Viewer" className="mt-1" />;
}

/** The count over a bell: Click UI's small solid badge, read out by the bell's tooltip. A
 *  count is not a warning: neutral, unless what it counts has failed or is overdue. */
export function CountBadge({ count, state = "neutral" }: { count: number; state?: "neutral" | "danger" }) {
  return (
    <Badge
      text={count > 99 ? "99+" : String(count)}
      type="solid"
      size="sm"
      state={state}
      aria-hidden="true"
      className="absolute -top-2 -right-2 pointer-events-none"
    />
  );
}
