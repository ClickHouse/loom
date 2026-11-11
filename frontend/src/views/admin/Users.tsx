import { Fragment, useMemo, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import { useStore, useIdentity } from "../../lib/state";
import { ListPager, usePageSize } from "../../components/ui/ListPager";
import { hasOrgAdminClaim } from "../../lib/role";
import { adminListUsers, claimRelease, type AdminUserRow, type AdminUsersResult } from "../../api";
import { EmptyState } from "../../components/EmptyState";
import { parseChTime } from "../../lib/format";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { PHONE_QUERY, useMedia } from "../../lib/useMedia";
import { useAbortableEffect } from "../../lib/hooks";
import { toastError } from "../../components/Toast";
import { confirmAction } from "../../components/ConfirmDialog";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";
import { TableSkeleton } from "../../components/Skeleton";
import { Alert, PageHeader, TipTarget } from "../../components/ui";
import { isCollectionName } from "../../lib/namespace";
import {
  collectionPlace,
  isOthersPersonal,
  knownCollections,
  collectionTitleOf,
  type CollectionsRead,
  useSharedCollectionsRead,
  withTwinNames,
} from "../../lib/collectionPlaces";

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
  const phone = useMedia(PHONE_QUERY);
  const [data, setData] = useState<AdminUsersResult | null>(null);
  // Whether those accounts were read with service accounts (null before the first read):
  // under the other setting they are not the list's to page.
  const [dataWithServiceAccounts, setDataWithServiceAccounts] = useState<boolean | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [includeServiceAccounts, setIncludeServiceAccounts] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = usePageSize("users");
  // A refresh keeps the list on screen, marked busy, until it answers.
  const [refreshing, setRefreshing] = useState(false);

  // Refresh and Retry read again through the effect, so a newer read drops an older one.
  const [reads, setReads] = useState(0);
  const load = (signal: AbortSignal, live: () => boolean) => {
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
        if (!live()) return;
        setData(d);
        setDataWithServiceAccounts(includeServiceAccounts);
      })
      .catch((e) => {
        if (live()) setError(e);
      })
      .finally(() => {
        if (live()) setRefreshing(false);
      });
  };
  useAbortableEffect(load, [identity?.org, includeServiceAccounts, reads]);

  const rows = useMemo(() => filtered(data, filter), [data, filter]);

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

  // The accounts on screen were read with the service-accounts setting now ticked; until a
  // toggle's read answers, its counts and rows are not drawn.
  const dataCurrent = dataWithServiceAccounts === includeServiceAccounts;
  const pageRows = rows.slice(page * pageSize, (page + 1) * pageSize);
  // One read holds at most 1,000 accounts: past that the list says it holds the first ones.
  const cut = data !== null && data.total > data.users.length;
  const count =
    !error && data && dataCurrent && rows.length > 0
      ? `${(page * pageSize + 1).toLocaleString()}–${(page * pageSize + pageRows.length).toLocaleString()} of ${rows.length.toLocaleString()}${cut ? ` · the first ${data.users.length.toLocaleString()} of ${data.total.toLocaleString()} accounts` : ""}`
      : undefined;
  const pick = (value: string) => {
    setFilter(value as Filter);
    setPage(0);
  };
  const options = data
    ? [
        // Each counts the accounts loaded, as its filter lists them (the toolbar says when
        // they are the first 1,000 of more).
        { value: "all", label: "All", count: data.users.length },
        {
          value: "pending",
          label: "Pending elevation",
          count: filtered(data, "pending").length,
          icon: "clock" as const,
        },
        { value: "active7d", label: "Active last 7d", count: filtered(data, "active7d").length },
      ]
    : [];
  const serviceAccounts = (
    <Checkbox
      label="Include service accounts"
      checked={includeServiceAccounts}
      onCheckedChange={(x) => {
        setIncludeServiceAccounts(x === true);
        setPage(0);
      }}
    />
  );

  return (
    <PageContainer>
      <div className="mb-6">
        <PageHeader
          title="Users"
          actions={
            <BusyButton
              type="secondary"
              htmlType="button"
              iconLeft="refresh"
              onClick={() => setReads((n) => n + 1)}
              busy={refreshing}
            >
              Refresh
            </BusyButton>
          }
        >
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <p className="text-sm text-text-muted max-w-2xl">
          Every subject in <span className="font-mono">{identity.org}</span> with namespace grants + last-activity. Use
          this to answer "who has access to X" and "when was Y last on."
        </p>
      </div>

      {/* Mounted before the first read answers, so its count is announced when it lands; it
          stays through a failed read, as its checkbox is how the reader got there. */}
      <ListToolbar label="Filters and count" search={false} count={count}>
        {/* Three filters with their counts are wider than a phone: there they are a select. */}
        {data &&
          (phone ? (
            <Select
              value={filter}
              onSelect={pick}
              triggerProps={{ "aria-label": "Filter users" }}
              className="min-w-0 flex-1"
            >
              {options.map((o) => (
                <Select.Item key={o.value} value={o.value}>
                  {dataCurrent ? `${o.label} ${o.count.toLocaleString()}` : o.label}
                </Select.Item>
              ))}
            </Select>
          ) : (
            <>
              <ButtonGroup
                aria-label="Filter users"
                selected={filter}
                onClick={pick}
                options={options.map((o) => ({
                  value: o.value,
                  label: <FilterLabel label={o.label} count={dataCurrent ? o.count : null} icon={o.icon} />,
                }))}
              />
              {serviceAccounts}
            </>
          ))}
      </ListToolbar>
      {data && phone && <div className="mb-3">{serviceAccounts}</div>}

      {!!error && <ListState kind="error" things="users" error={error} onRetry={() => setReads((n) => n + 1)} />}
      {!error && (data === null || !dataCurrent) && (
        <Table
          aria-label="Users"
          aria-busy
          size="sm"
          headers={USER_HEADERS}
          rows={[]}
          noDataMessage={<TableSkeleton label="Loading users" />}
        />
      )}
      {!error && data !== null && dataCurrent && data.users.length === 0 && (
        <ListState
          kind="empty"
          icon="users"
          title="No accounts yet"
          description="When users sign up in the dashboard or get granted on a namespace, they'll appear here."
        />
      )}
      {!error && data !== null && dataCurrent && data.users.length > 0 && (
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

          <UserListTable rows={pageRows} filter={filter} />
          <ListPager
            label="Users pages"
            page={page}
            pageSize={pageSize}
            shown={pageRows.length}
            total={rows.length}
            busy={refreshing}
            onPage={setPage}
            onPageSize={(n) => {
              setPageSize(n);
              setPage(0);
            }}
            topId="users-list"
          />

          <div className="mt-3 text-xs text-text-muted">
            To promote someone to platform admin, add their login handle, or the email they sign in with over SSO, to{" "}
            <code className="code-inline text-xs">LOOM_PLATFORM_ADMINS</code> in{" "}
            <code className="code-inline text-xs">.env</code> and have them re-login.
          </div>
        </div>
      )}

      <ClaimReleaseSection />
    </PageContainer>
  );
}

const SEVEN_DAYS_MS = 7 * 24 * 3600 * 1000;

/** The accounts the filter keeps (admin.users has no offset, so the pages are cut here). */
function filtered(data: AdminUsersResult | null, filter: Filter): AdminUserRow[] {
  if (!data) return [];
  if (filter === "pending") return data.users.filter((u) => u.grants.some((g) => g.pending_role));
  if (filter === "active7d") {
    return data.users.filter((u) => {
      const active = parseChTime(u.last_active);
      return !!active && Date.now() - active.getTime() < SEVEN_DAYS_MS;
    });
  }
  return data.users;
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
      toastError("Release failed", e);
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
function FilterLabel({ label, count, icon }: { label: string; count: number | null; icon?: IconName }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {icon && <Icon name={icon} size="xs" aria-hidden="true" />}
      {label}
      {count !== null && <span className="tabular">{count.toLocaleString()}</span>}
    </span>
  );
}

const USER_HEADERS = [{ label: "Subject" }, { label: "Grants" }, { label: "Auth" }, { label: "Last active" }];

// A new page scrolls to the table and gives it focus.
function UserListTable({ rows, filter }: { rows: AdminUserRow[]; filter: Filter }) {
  // A grant on a collection is tagged as one, by title where the viewer can read it.
  const org = useStore((s) => s.identity?.org);
  const collectionsRead = useSharedCollectionsRead(
    rows.some((u) => u.grants.some((g) => isCollectionName(g.namespace))) ? org : null,
  );
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
            label: <GrantBadges grants={u.grants} read={collectionsRead} />,
            overflowMode: "wrap",
          },
          { label: <span className="text-xs text-text-muted">{u.has_password_auth ? "password" : "token / SA"}</span> },
          {
            label: (
              <span className="text-xs text-text-muted">
                {u.last_active ? <ListTime time={u.last_active} quiet /> : "never"}
              </span>
            ),
          },
        ],
      }))}
    />
  );
}

/** One person's grants as badges; a collection by its title, with its place and address in
 *  its tip. */
function GrantBadges({ grants, read }: { grants: AdminUserRow["grants"]; read: CollectionsRead | null }) {
  const collections = read?.rows ?? null;
  // Past `collection.list`'s first page, a title the viewer's own list_mine row carries.
  const mine = useStore((s) => s.myNamespaces);
  // Two of one person's collections that look alike differ by name, as everywhere else.
  const title = withTwinNames(
    (ns) => collectionTitleOf(ns, collections, mine),
    [...grants.map((g) => g.namespace), ...knownCollections(collections, mine)],
  );
  return (
    <div className="flex flex-wrap gap-1.5">
      {grants.length === 0 && <span className="text-xs text-text-muted">—</span>}
      {grants.map((g) => {
        const key = `${g.namespace}-${g.role}`;
        const collection = isCollectionName(g.namespace);
        const badge = (
          <Badge
            size="sm"
            state={g.pending_role ? "warning" : g.is_owner ? "info" : "neutral"}
            text={
              <span className={collection ? undefined : "font-mono"}>
                {g.pending_role && <span className="sr-only">Pending elevation: </span>}
                {collection
                  ? isOthersPersonal(g.namespace, read)
                    ? "personal collection"
                    : `collection ${title(g.namespace)}`
                  : g.namespace}{" "}
                · {g.pending_role ? `${g.role} → ${g.pending_role}` : `${g.role}${g.is_owner ? " · owner" : ""}`}
              </span>
            }
          />
        );
        if (!collection) return <Fragment key={key}>{badge}</Fragment>;
        // Titles repeat: a collection's place and address are in its tip.
        const place = collectionPlace(g.namespace, collections);
        return (
          <TipTarget key={key} tip={place ? `${place} · ${g.namespace}` : g.namespace}>
            {badge}
          </TipTarget>
        );
      })}
    </div>
  );
}
