import { Suspense, lazy, useEffect, useRef, useState, useMemo } from "react";
import { Bell, Crown, UserRound, Eye } from "lucide-react";
import { listPendingApprovals, type GrantRole } from "../api";
import { sharedCodeRepos } from "../lib/sharedReads";
import { useVisiblePoll } from "../lib/hooks";
import { useAfterPaint } from "../lib/afterPaint";
import { useStore } from "../lib/state";
import { canCallCodeRepos } from "../lib/canCall";
// 10.5 kB that opens on a click loads on the click.
const MembersDialog = lazy(() => import("../flows/MembersDialog").then((m) => ({ default: m.MembersDialog })));
import { relativeTime, displaySubject, isServiceSubject } from "../lib/format";

interface AdminPending {
  namespace: string;
  kind?: string;
  subject: string;
  requested_role: GrantRole;
  current_role: GrantRole;
  pending_since: string | null;
}

export function PendingApprovalsBell() {
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
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

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
  const rowTitle = (it: AdminPending & { branches?: string[] }): string =>
    it.kind === "code"
      ? `${it.subject} asks to ${it.requested_role === "admin" ? "maintain" : "read"} ${repoInfo[it.namespace]?.repo ?? it.namespace}${it.branches && it.branches.length > 1 ? ` (${it.branches.join(", ")})` : ` @ ${repoInfo[it.namespace]?.branch ?? it.namespace}`} — ${it.namespace}`
      : `${it.subject} wants ${it.requested_role} on ${it.namespace}`;

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

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target instanceof Node ? e.target : null;
      if (triggerRef.current?.contains(t)) return;
      if (dropdownRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  if (!identity) return null;

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        onClick={() => setOpen((v) => !v)}
        className="h-8 w-8 inline-flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors relative"
        title={
          items.length > 0 ? `${items.length} pending approval${items.length === 1 ? "" : "s"}` : "No pending approvals"
        }
        aria-label="Pending approvals"
      >
        <Bell size={15} />
        {items.length > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-[16px] px-1 inline-flex items-center justify-center text-2xs font-semibold bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-full">
            {items.length > 99 ? "99+" : items.length}
          </span>
        )}
      </button>
      {open && (
        <div
          ref={dropdownRef}
          className="absolute right-0 top-full mt-1 z-50 w-[min(440px,92vw)] max-h-[480px] overflow-y-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] shadow-xl py-1"
        >
          <div className="px-4 py-2.5 border-b border-[var(--color-border)] text-2xs uppercase tracking-wider text-[var(--color-text-subtle)] font-semibold">
            Pending approvals · {items.length}
          </div>
          {items.length === 0 && (
            <div className="px-4 py-6 text-sm text-[var(--color-text-muted)] text-center">
              No one's waiting for your approval.
              <div className="text-2xs text-[var(--color-text-subtle)] mt-1.5">
                Join requests on namespaces you admin show up here. To invite someone proactively, head to Spaces →
                Namespaces you manage.
              </div>
              <button
                onClick={() => {
                  setOpen(false);
                  useStore.getState().setView("browse");
                }}
                className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
              >
                Open Spaces →
              </button>
            </div>
          )}
          {grouped.map((it) => (
            <button
              key={`${it.namespace}::${it.subject}`}
              onClick={() => {
                setOpen(false);
                setManageKind(it.kind);
                setManageLabel(it.kind === "code" ? repoLabel(it.namespace) : null);
                setManagePendingElsewhere(it.kind === "code" ? pendingElsewhereFor(it.namespace) : {});
                setManageNs(it.namespace);
              }}
              className="w-full text-left px-4 py-2.5 hover:bg-[var(--color-surface-2)] flex items-start gap-3 transition-colors"
              title={rowTitle(it)}
            >
              <RoleIcon role={it.requested_role} />
              <div className="flex-1 min-w-0">
                {/* The target is what the approver has to read, so it gets
                    its own line and wraps; nothing here is ever truncated. */}
                <div className="text-sm text-[var(--color-text)] break-words">
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
                <div className="text-sm font-medium text-[var(--color-text)] break-words">
                  {it.kind === "code" ? (
                    <>
                      {repoInfo[it.namespace]?.repo ?? it.namespace}
                      {it.branches && it.branches.length > 1 ? (
                        <span className="text-[var(--color-text-subtle)] font-normal">
                          {" "}
                          · {it.branches.length} branches ({it.branches.join(", ")}) — one decision
                        </span>
                      ) : (
                        <span className="text-[var(--color-text-subtle)] font-normal">
                          {" "}
                          @ {repoInfo[it.namespace]?.branch ?? it.namespace}
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="font-mono break-all">{it.namespace}</span>
                  )}
                </div>
                {it.kind === "code" && repoInfo[it.namespace] && (
                  <div className="text-2xs font-mono text-[var(--color-text-subtle)] break-all">{it.namespace}</div>
                )}
                <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">
                  {it.current_role !== it.requested_role && <>currently {it.current_role} · </>}
                  {it.pending_since && <>requested {relativeTime(it.pending_since)}</>}
                </div>
              </div>
              <span className="text-2xs text-[var(--color-text-subtle)] mt-1 shrink-0">Review →</span>
            </button>
          ))}
        </div>
      )}
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
    </div>
  );
}

function RoleIcon({ role }: { role: GrantRole }) {
  if (role === "admin") return <Crown size={13} className="text-warn shrink-0 mt-0.5" aria-label="Admin" />;
  if (role === "writer")
    return <UserRound size={13} className="text-[var(--color-text-muted)] shrink-0 mt-0.5" aria-label="Writer" />;
  return <Eye size={13} className="text-[var(--color-text-subtle)] shrink-0 mt-0.5" aria-label="Viewer" />;
}
