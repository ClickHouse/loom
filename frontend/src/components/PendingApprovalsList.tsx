import { lazy, Suspense, useId, useMemo, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { Title } from "@clickhouse/click-ui/Title";
import type { GrantRole } from "../api";
import { useStore } from "../lib/state";
import { relativeTime, displaySubject, isServiceSubject } from "../lib/format";
// 10.5 kB that opens on a click loads on the click.
const MembersDialog = lazy(() => import("../flows/MembersDialog").then((m) => ({ default: m.MembersDialog })));

export interface AdminPending {
  namespace: string;
  kind?: string;
  subject: string;
  requested_role: GrantRole;
  current_role: GrantRole;
  pending_since: string | null;
}

/** The pending-approvals popover's list, and the members dialog a row opens. */
export function PendingApprovalsList({
  items,
  repoInfo,
  refresh,
  close,
}: {
  items: AdminPending[];
  repoInfo: Record<string, { repo: string; branch: string }>;
  refresh: () => void;
  close: () => void;
}) {
  const headingId = useId();
  const identity = useStore((s) => s.identity);
  const [manageNs, setManageNs] = useState<string | null>(null);
  const [manageKind, setManageKind] = useState<string | undefined>(undefined);
  const [manageLabel, setManageLabel] = useState<string | null>(null);
  const [managePendingElsewhere, setManagePendingElsewhere] = useState<
    Record<string, { namespace: string; label: string }[]>
  >({});
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
  if (!identity) return null;
  return (
    <>
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
                close();
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
              close();
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
    </>
  );
}

function RoleIcon({ role }: { role: GrantRole }) {
  if (role === "admin")
    return <Icon name="star" size="xs" color="var(--global-color-text-muted)" aria-label="Admin" className="mt-1" />;
  if (role === "writer") return <Icon name="user" size="xs" aria-label="Writer" className="mt-1" />;
  return <Icon name="eye" size="xs" color="var(--color-text-muted)" aria-label="Viewer" className="mt-1" />;
}
