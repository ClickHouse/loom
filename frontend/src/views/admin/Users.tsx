import { useMemo, useState } from "react";
import { Users as UsersIcon, RefreshCw, Shield, Crown, Clock } from "lucide-react";
import { useStore, useIdentity } from "../../lib/state";
import { hasOrgAdminClaim } from "../../lib/role";
import { adminListUsers, claimRelease, type AdminUserRow, type AdminUsersResult } from "../../api";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { relativeTime, humanizeError } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";

type Filter = "all" | "pending" | "active7d";

/**
 * Users — platform-admin-only view. Aggregates every subject in the
 * org with the set of namespaces they hold + role on each + last
 * activity. Service accounts hidden by default; toggle to surface.
 *
 * Gated server-side via Role.ORG_ADMIN on the admin.users op. The
 * Sidebar gates the visibility client-side as a UX hint, but the
 * actual security boundary is the op-level role check.
 */
export function Users() {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const [data, setData] = useState<AdminUsersResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [includeServiceAccounts, setIncludeServiceAccounts] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    if (!identity) return;
    setError(null);
    adminListUsers(
      {
        org: identity.org,
        include_service_accounts: includeServiceAccounts,
        limit: 200,
      },
      { signal },
    )
      .then((d) => {
        if (live()) setData(d);
      })
      .catch((e) => {
        if (live()) setError(String(e?.message || e));
      });
  };
  useAbortableEffect(load, [identity?.org, includeServiceAccounts]);

  if (!identity) return null;

  // admin.users requires ORG_ADMIN.
  if (!hasOrgAdminClaim(whoami)) {
    return (
      <PageContainer>
        <EmptyState
          icon={Shield}
          title="Operators only"
          description="The Users view shows every account in the org. It's gated to platform / org admins — the operators running this Loom. Your token doesn't carry that role."
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <div className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
          <UsersIcon size={20} /> Users
        </h1>
        <div className="mt-1">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </div>
        <p className="text-sm text-[var(--color-text-muted)] mt-1 max-w-2xl">
          Every subject in <span className="font-mono">{identity.org}</span> with namespace grants + last-activity. Use
          this to answer "who has access to X" and "when was Y last on."
        </p>
      </div>

      {data && (
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <FilterChip
            label="All"
            count={data.users.length}
            active={filter === "all"}
            onClick={() => setFilter("all")}
          />
          <FilterChip
            label="Pending elevation"
            count={data.total_pending_elevations}
            active={filter === "pending"}
            onClick={() => setFilter("pending")}
            tone={data.total_pending_elevations > 0 ? "warn" : "muted"}
            icon={<Clock size={11} />}
          />
          <FilterChip
            label="Active last 7d"
            count={data.total_active_last_7d}
            active={filter === "active7d"}
            onClick={() => setFilter("active7d")}
          />
          <div className="flex-1" />
          <label className="inline-flex items-center gap-2 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={includeServiceAccounts}
              onChange={(e) => setIncludeServiceAccounts(e.target.checked)}
            />
            Include service accounts
          </label>
          <button
            onClick={() => load()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-2)]"
          >
            <RefreshCw size={12} /> Refresh
          </button>
        </div>
      )}

      {error && <EmptyState icon={UsersIcon} title="Couldn't load" description={error} />}
      {!error && data === null && (
        <div className="space-y-2">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </div>
      )}
      {!error && data !== null && data.users.length === 0 && (
        <EmptyState
          icon={UsersIcon}
          title="No accounts yet"
          description="When users sign up in the dashboard or get granted on a namespace, they'll appear here."
        />
      )}
      {!error && data !== null && data.users.length > 0 && (
        <div className="space-y-2">
          {data.platform_admins_configured.length > 0 && (
            <div className="mb-3 text-xs text-[var(--color-text-muted)]">
              <span className="font-medium text-[var(--color-text)]">
                Configured platform admins (LOOM_PLATFORM_ADMINS):
              </span>{" "}
              {data.platform_admins_configured.map((e, i) => (
                <span key={e} className="font-mono">
                  {i > 0 && ", "}
                  {e}
                </span>
              ))}
            </div>
          )}

          <UserListTable data={data} filter={filter} />

          <div className="mt-3 text-2xs text-[var(--color-text-muted)]">
            {data.total} account{data.total === 1 ? "" : "s"} shown. To promote someone to platform admin, add their
            email to{" "}
            <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 py-0.5 rounded">
              LOOM_PLATFORM_ADMINS
            </code>{" "}
            in <code className="font-mono text-2xs bg-[var(--color-surface-2)] px-1 py-0.5 rounded">.env</code> and have
            them re-login.
          </div>
        </div>
      )}

      <ClaimReleaseSection />
    </PageContainer>
  );
}

/**
 * Operator escape hatch: release a namespace whose owner lost their
 * password (Loom has no self-serve reset). Grants are revoked and the
 * namespace deactivated; its memories stay put and the name stays
 * reserved, so no fresh signup can take over the old owner's data.
 * Lives here on the Users page
 * because the audience overlaps (platform admins managing accounts).
 */
function ClaimReleaseSection() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const [ns, setNs] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  // claim_release requires ORG_ADMIN.
  if (!hasOrgAdminClaim(whoami)) return null;

  const run = async () => {
    if (!ns.trim()) return;
    if (
      !(await confirmAction({
        title: `Release ${ns.trim()}?`,
        message:
          "Every grant is revoked and the namespace deactivated. Memories stay intact and the name stays reserved: nobody can claim it again.",
        confirmLabel: "Release",
        danger: true,
      }))
    )
      return;
    setBusy(true);
    try {
      const r = await claimRelease(identity.org, ns.trim(), reason.trim());
      setResult(
        `Released ${r.namespace} (was owned by ${r.previous_owner || "unknown"}, revoked ${r.grants_revoked} grants).`,
      );
      setNs("");
      setReason("");
    } catch (e) {
      toast(`Release failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mt-8">
      <h2 className="text-md font-semibold mb-3">Release a namespace claim</h2>
      <div className="rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-5">
        <div className="text-sm text-[var(--color-text-muted)] mb-4">
          Release a namespace whose owner lost their password: its grants are revoked and it is deactivated. Existing
          memories under it are preserved, and the name stays reserved.
        </div>
        <input
          aria-label="Namespace to release"
          value={ns}
          onChange={(e) => setNs(e.target.value)}
          placeholder="namespace to release (e.g. alice)"
          className="w-full px-3 py-2 text-base font-mono rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)] mb-2"
        />
        <input
          aria-label="Reason for releasing the claim"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (audit trail)"
          className="w-full px-3 py-2 text-base rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)] mb-3"
        />
        <button
          onClick={run}
          disabled={busy || !ns.trim()}
          className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium border border-[var(--color-danger)]/60 text-[var(--color-danger)] hover:bg-[var(--color-surface-2)] rounded-md disabled:opacity-50"
        >
          {busy ? "Releasing…" : "Release claim"}
        </button>
        {result && (
          <div className="mt-3 px-3 py-2 text-xs text-[var(--color-success)] bg-[var(--color-surface-2)] rounded-md">
            {result}
          </div>
        )}
      </div>
    </section>
  );
}

function FilterChip({
  label,
  count,
  active,
  onClick,
  tone = "default",
  icon,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  tone?: "default" | "warn" | "muted";
  icon?: React.ReactNode;
}) {
  const toneCls = active
    ? "bg-[var(--color-accent)] text-[var(--color-on-accent)] border-[var(--color-accent)]"
    : tone === "warn" && count > 0
      ? "bg-[var(--color-warn)]/10 text-[var(--color-warn)] border-[var(--color-warn)]/40 hover:bg-[var(--color-warn)]/15"
      : "bg-[var(--color-surface)] text-[var(--color-text-muted)] border-[var(--color-border)] hover:bg-[var(--color-surface-2)]";
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md border transition-colors ${toneCls}`}
    >
      {icon}
      <span>{label}</span>
      <span className={`tabular ${active ? "opacity-90" : "opacity-70"}`}>{count}</span>
    </button>
  );
}

function UserListTable({ data, filter }: { data: AdminUsersResult; filter: Filter }) {
  const sevenDaysMs = 7 * 24 * 3600 * 1000;
  const rows = useMemo(() => {
    if (filter === "pending") {
      return data.users.filter((u) => u.grants.some((g) => g.pending_role));
    }
    if (filter === "active7d") {
      return data.users.filter((u) => u.last_active && Date.now() - new Date(u.last_active).getTime() < sevenDaysMs);
    }
    return data.users;
  }, [data, filter, sevenDaysMs]);

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-6 text-sm text-[var(--color-text-muted)] text-center">
        {filter === "pending"
          ? "No pending elevation requests."
          : filter === "active7d"
            ? "No users active in the last 7 days."
            : "No users to show."}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-[var(--color-surface-2)]">
          <tr className="text-left text-2xs uppercase tracking-wider text-[var(--color-text-muted)]">
            <th className="px-4 py-2.5">Subject</th>
            <th className="px-4 py-2.5">Namespaces</th>
            <th className="px-4 py-2.5">Auth</th>
            <th className="px-4 py-2.5 text-right">Last active</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((u: AdminUserRow) => (
            <tr key={u.subject} className="border-t border-[var(--color-border)] align-top">
              <td className="px-4 py-3">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="font-mono text-sm">{u.subject}</span>
                  {u.is_platform_admin && (
                    <span
                      title="Platform admin"
                      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-2xs font-semibold bg-[var(--color-accent-soft)] text-[var(--color-accent)] border border-[var(--color-accent)]/30"
                    >
                      <Crown size={10} /> ADMIN
                    </span>
                  )}
                  {u.grants.some((g) => g.pending_role) && (
                    <span
                      title="Has a pending elevation request"
                      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-2xs font-semibold bg-[var(--color-warn)]/10 text-[var(--color-warn)] border border-[var(--color-warn)]/30"
                    >
                      <Clock size={10} /> PENDING
                    </span>
                  )}
                </div>
                {u.contact_email && (
                  <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">{u.contact_email}</div>
                )}
              </td>
              <td className="px-4 py-3">
                <div className="flex flex-wrap gap-1.5">
                  {u.grants.length === 0 && <span className="text-2xs text-[var(--color-text-muted)]">—</span>}
                  {u.grants.map((g) => (
                    <span
                      key={`${g.namespace}-${g.role}`}
                      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-2xs font-mono ${
                        g.pending_role
                          ? "bg-[var(--color-warn)]/10 text-[var(--color-text)] border border-[var(--color-warn)]/40"
                          : g.is_owner
                            ? "bg-[var(--color-surface-2)] text-[var(--color-text)] border border-[var(--color-accent)]/40"
                            : "bg-[var(--color-surface-2)] text-[var(--color-text-muted)] border border-[var(--color-border)]"
                      }`}
                      title={
                        g.pending_role
                          ? `Pending elevation: ${g.role} → ${g.pending_role}`
                          : `${g.is_owner ? "Owner" : "Grant"}: ${g.role}`
                      }
                    >
                      {g.namespace}
                      <span className="opacity-60">·</span>
                      <span className="text-2xs">
                        {g.pending_role ? `${g.role} → ${g.pending_role}` : `${g.role}${g.is_owner ? "·owner" : ""}`}
                      </span>
                    </span>
                  ))}
                </div>
              </td>
              <td className="px-4 py-3 text-xs text-[var(--color-text-muted)]">
                {u.has_password_auth ? "password" : "token / SA"}
              </td>
              <td className="px-4 py-3 text-xs tabular text-right text-[var(--color-text-muted)] whitespace-nowrap">
                {u.last_active ? relativeTime(u.last_active) : "never"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
