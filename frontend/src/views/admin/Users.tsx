import { useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import { useStore, useIdentity } from "../../lib/state";
import { ListPager, usePageSize } from "../../components/ui/ListPager";
import { hasOrgAdminClaim } from "../../lib/role";
import { adminListUsers, claimRelease, type AdminUserRow, type AdminUsersResult } from "../../api";
import { EmptyState } from "../../components/EmptyState";
import { relativeTime, humanizeError } from "../../lib/format";
import { useAbortableEffect } from "../../lib/hooks";
import { toast } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";
import { Alert, PageHeader } from "../../components/ui";

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
  // A refresh keeps the list on screen, marked busy, until it answers.
  const [refreshing, setRefreshing] = useState(false);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    if (!identity) return;
    setError(null);
    setRefreshing(true);
    adminListUsers(
      {
        org: identity.org,
        include_service_accounts: includeServiceAccounts,
        // admin.users has no offset: one read of up to 1,000 accounts, paged here.
        limit: 1000,
      },
      { signal },
    )
      .then((d) => {
        if (live()) setData(d);
      })
      .catch((e) => {
        if (live()) setError(String(e?.message || e));
      })
      .finally(() => {
        if (live()) setRefreshing(false);
      });
  };
  useAbortableEffect(load, [identity?.org, includeServiceAccounts]);

  if (!identity) return null;

  // admin.users requires ORG_ADMIN.
  if (!hasOrgAdminClaim(whoami)) {
    return (
      <PageContainer>
        <EmptyState
          icon="secure"
          title="Operators only"
          description="The Users view shows every account in the org. It's gated to platform / org admins — the operators running this Loom. Your token doesn't carry that role."
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <div className="mb-6">
        <PageHeader title="Users">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <p className="text-sm text-text-muted max-w-2xl">
          Every subject in <span className="font-mono">{identity.org}</span> with namespace grants + last-activity. Use
          this to answer "who has access to X" and "when was Y last on."
        </p>
      </div>

      {data && (
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <ButtonGroup
            aria-label="Filter users"
            selected={filter}
            onClick={(value) => setFilter(value as Filter)}
            options={[
              { value: "all", label: <FilterLabel label="All" count={data.users.length} /> },
              {
                value: "pending",
                label: <FilterLabel label="Pending elevation" count={data.total_pending_elevations} icon="clock" />,
              },
              { value: "active7d", label: <FilterLabel label="Active last 7d" count={data.total_active_last_7d} /> },
            ]}
          />
          <div className="flex-1" />
          <Checkbox
            label="Include service accounts"
            checked={includeServiceAccounts}
            onCheckedChange={(x) => setIncludeServiceAccounts(x === true)}
          />
          <Button type="secondary" htmlType="button" iconLeft="refresh" onClick={() => load()} loading={refreshing}>
            Refresh
          </Button>
        </div>
      )}

      {error && <EmptyState icon="users" title="Couldn't load" description={error} />}
      {!error && data === null && (
        <Table aria-label="Users" aria-busy loading size="sm" headers={USER_HEADERS} rows={[]} />
      )}
      {!error && data !== null && data.users.length === 0 && (
        <EmptyState
          icon="users"
          title="No accounts yet"
          description="When users sign up in the dashboard or get granted on a namespace, they'll appear here."
        />
      )}
      {!error && data !== null && data.users.length > 0 && (
        <div className="space-y-2" aria-busy={refreshing || undefined}>
          {data.platform_admins_configured.length > 0 && (
            <div className="mb-3 text-xs text-text-muted">
              <span className="font-medium text-text">Configured platform admins (LOOM_PLATFORM_ADMINS):</span>{" "}
              {data.platform_admins_configured.map((e, i) => (
                <span key={e} className="font-mono">
                  {i > 0 && ", "}
                  {e}
                </span>
              ))}
            </div>
          )}

          {/* Keyed by the filter, so another filter starts from its first page. */}
          <UserList key={filter} data={data} filter={filter} />

          <div className="mt-3 text-xs text-text-muted">
            {data.total} account{data.total === 1 ? "" : "s"} shown. To promote someone to platform admin, add their
            email to <code className="font-mono text-xs bg-surface-2 px-1 py-0.5 rounded">LOOM_PLATFORM_ADMINS</code> in{" "}
            <code className="font-mono text-xs bg-surface-2 px-1 py-0.5 rounded">.env</code> and have them re-login.
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
      <h2 className="mb-3">Release a namespace claim</h2>
      <div className="rounded-2xl border border-border bg-surface p-5">
        <div className="text-sm text-text-muted mb-4">
          Release a namespace whose owner lost their password: its grants are revoked and it is deactivated. Existing
          memories under it are preserved, and the name stays reserved.
        </div>
        <div className="space-y-2 mb-3">
          <TextField
            aria-label="Namespace to release"
            value={ns}
            onChange={(value) => setNs(value)}
            placeholder="namespace to release (e.g. alice)"
          />
          <TextField
            aria-label="Reason for releasing the claim"
            value={reason}
            onChange={(value) => setReason(value)}
            placeholder="Reason (audit trail)"
          />
        </div>
        <Button type="danger" htmlType="button" onClick={run} loading={busy} disabled={busy || !ns.trim()}>
          Release claim
        </Button>
        {result && <Alert className="mt-3" state="success" size="small" text={result} />}
      </div>
    </section>
  );
}

// ButtonGroup draws an option's `icon` only in icon-only mode, so the label carries it.
function FilterLabel({ label, count, icon }: { label: string; count: number; icon?: IconName }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {icon && <Icon name={icon} size="xs" aria-hidden="true" />}
      {label}
      <span className="tabular">{count}</span>
    </span>
  );
}

const USER_HEADERS = [{ label: "Subject" }, { label: "Namespaces" }, { label: "Auth" }, { label: "Last active" }];

/** The accounts the filter keeps, 50 to a page (admin.users has no offset, so the pages
 *  are cut here). */
function UserList({ data, filter }: { data: AdminUsersResult; filter: Filter }) {
  const sevenDaysMs = 7 * 24 * 3600 * 1000;
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = usePageSize("users");
  const rows = useMemo(() => {
    if (filter === "pending") {
      return data.users.filter((u) => u.grants.some((g) => g.pending_role));
    }
    if (filter === "active7d") {
      return data.users.filter((u) => u.last_active && Date.now() - new Date(u.last_active).getTime() < sevenDaysMs);
    }
    return data.users;
  }, [data, filter, sevenDaysMs]);
  const pageRows = rows.slice(page * pageSize, (page + 1) * pageSize);

  return (
    <>
      <UserListTable rows={pageRows} filter={filter} />
      <ListPager
        label="Users pages"
        page={page}
        pageSize={pageSize}
        shown={pageRows.length}
        total={rows.length}
        onPage={setPage}
        onPageSize={(n) => {
          setPageSize(n);
          setPage(0);
        }}
        topId="users-list"
      />
    </>
  );
}

// A new page scrolls to the table and gives it focus.
function UserListTable({ rows, filter }: { rows: AdminUserRow[]; filter: Filter }) {
  return (
    <Table
      id="users-list"
      tabIndex={-1}
      aria-label="Users"
      size="sm"
      noDataMessage={
        filter === "pending"
          ? "No pending elevation requests."
          : filter === "active7d"
            ? "No users active in the last 7 days."
            : "No users to show."
      }
      headers={USER_HEADERS}
      rows={rows.map((u: AdminUserRow) => ({
        id: u.subject,
        items: [
          {
            label: (
              <div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="font-mono text-sm">{u.subject}</span>
                  {u.is_platform_admin && <Badge text="Platform admin" state="info" size="sm" />}
                  {u.grants.some((g) => g.pending_role) && (
                    <Badge
                      text={
                        <>
                          Pending<span className="sr-only"> elevation request</span>
                        </>
                      }
                      state="warning"
                      size="sm"
                    />
                  )}
                </div>
                {u.contact_email && <div className="text-xs text-text-muted mt-0.5">{u.contact_email}</div>}
              </div>
            ),
            overflowMode: "wrap",
          },
          {
            label: (
              <div className="flex flex-wrap gap-1.5">
                {u.grants.length === 0 && <span className="text-xs text-text-muted">—</span>}
                {u.grants.map((g) => (
                  <Badge
                    key={`${g.namespace}-${g.role}`}
                    size="sm"
                    state={g.pending_role ? "warning" : g.is_owner ? "info" : "neutral"}
                    text={
                      <span className="font-mono">
                        {g.pending_role && <span className="sr-only">Pending elevation: </span>}
                        {g.namespace} ·{" "}
                        {g.pending_role ? `${g.role} → ${g.pending_role}` : `${g.role}${g.is_owner ? " · owner" : ""}`}
                      </span>
                    }
                  />
                ))}
              </div>
            ),
            overflowMode: "wrap",
          },
          { label: <span className="text-xs text-text-muted">{u.has_password_auth ? "password" : "token / SA"}</span> },
          {
            label: (
              <span className="text-xs tabular text-text-muted whitespace-nowrap">
                {u.last_active ? relativeTime(u.last_active) : "never"}
              </span>
            ),
          },
        ],
      }))}
    />
  );
}
