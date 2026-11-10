import { useState } from "react";
import { toolActivityRows } from "../lib/flowRows";
import { useAbortableEffect } from "../lib/hooks";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Table } from "@clickhouse/click-ui/Table";
import { Title } from "@clickhouse/click-ui/Title";
import { getMySharingSummary, type SharingSummary, type CodeRepoSummary } from "../api";
import { sharedCodeRepos, sharedMine } from "../lib/sharedReads";
import { useStore, useIdentity } from "../lib/state";
import { canCallCodeRepos } from "../lib/canCall";
import { Skeleton } from "../components/Skeleton";
import { EmptyState } from "../components/EmptyState";
import { displayAgent, SENTINEL_AGENTS } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { LoadingSkeleton, PageHeader, Stat, TipTarget } from "../components/ui";

export function Sharing() {
  // The summary itself spans every namespace the caller holds a grant on
  // ("what you're sharing" needs all of them); the Tool activity table is
  // filtered to the namespace this page shows. Re-fetch when the namespace switcher fires
  // so the filter follows it.
  const identity = useIdentity();
  const [showAllNamespaces, setShowAllNamespaces] = useState(false);
  // One heading for the four render branches below: the H1 and the scope
  // it reads from.
  const heading = (
    <PageHeader title="Flow">
      <ScopeBadge kind="namespace" value={identity.namespace} />
    </PageHeader>
  );
  const [data, setData] = useState<SharingSummary | null>(null);
  const [repoNames, setRepoNames] = useState<Set<string>>(new Set());
  // repo @ branch + join rule per repository namespace (a repository is named by what it
  // indexes and how you join, never by "public"/"private", which mean something else on a
  // memory namespace).
  const [repoInfo, setRepoInfo] = useState<Map<string, CodeRepoSummary>>(new Map());
  const setView = useStore((s) => s.setView);
  const whoami = useStore((s) => s.whoami);
  useAbortableEffect(
    (_signal, live) => {
      // `code.repos` follows the list_mine read and runs only when a code-kind row (or an
      // admin claim) says the caller can read repositories; a failed list_mine asks for
      // nothing.
      sharedMine(identity.org)
        .then((rows) => {
          if (!live()) return;
          setRepoNames(new Set(rows.filter((r) => r.kind === "code").map((r) => r.namespace)));
          if (!canCallCodeRepos(rows, whoami)) {
            setRepoInfo(new Map());
            return;
          }
          sharedCodeRepos(identity.org)
            .then((repos) => {
              if (live()) setRepoInfo(new Map(repos.map((r) => [r.namespace, r])));
            })
            .catch(() => {
              if (live()) setRepoInfo(new Map());
            });
        })
        .catch(() => {
          if (live()) {
            setRepoNames(new Set());
            setRepoInfo(new Map());
          }
        });
    },
    [identity.org],
  );
  const [error, setError] = useState<string | null>(null);

  useAbortableEffect(
    (signal, live) => {
      setData(null);
      setError(null);
      getMySharingSummary({ signal })
        .then((d) => {
          if (live()) setData(d);
        })
        .catch((e) => {
          if (live()) setError(humanizeError(e));
        });
    },
    [identity.org, identity.namespace],
  );

  if (error) {
    return (
      <PageContainer>
        {heading}
        <p className="text-sm text-text-muted mb-6">Couldn't load activity: {error}</p>
      </PageContainer>
    );
  }

  if (!data) {
    return (
      <PageContainer>
        {heading}
        <p className="text-sm text-text-muted mb-6">
          Where your memories flow — which tools wrote, which read, and which namespaces you share with.
        </p>
        {/* Shaped like the page: the four totals, then the two lists. */}
        <LoadingSkeleton label="Loading activity">
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
            </div>
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        </LoadingSkeleton>
      </PageContainer>
    );
  }

  const { totals, window_days } = data;
  // A code index is a repository you read, not a namespace you share into — it gets its
  // own block and its own verbs (in the Repositories section), never a row among memory
  // namespaces.
  const namespaces = data.namespaces.filter((n) => !repoNames.has(n.namespace));
  const repos = data.namespaces.filter((n) => repoNames.has(n.namespace));
  const empty = totals.namespaces === 0 && totals.agents === 0;

  if (empty) {
    return (
      <PageContainer>
        {heading}
        <p className="text-sm text-text-muted mb-6">
          Where your memories flow — which tools wrote, which read, and which namespaces you share with.
        </p>
        <EmptyState
          icon="share-network"
          title="No activity yet"
          description="Once your agents start writing memories — or you join a public namespace from Spaces (namespace picker → Browse and join) — the cross-tool flow will appear here."
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      {heading}
      <p className="text-sm text-text-muted mb-6">
        Where your memories flow — which tools wrote, which read, and which namespaces you share with.
      </p>

      <section className="mb-8">
        <Title type="h2" size="sm" className="mb-3">
          Last {window_days} days
        </Title>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat label="Writes" value={totals.writes.toLocaleString()} />
          <Stat label="Recalls" value={totals.recalls.toLocaleString()} />
          <Stat label="Cross-namespace recalls" value={totals.cross_namespace_recalls.toLocaleString()} />
          <Stat label="Active namespaces" value={totals.namespaces.toLocaleString()} />
        </div>
        <p className="mt-2 text-xs text-text-muted leading-relaxed">
          Totals span the {totals.namespaces} namespace{totals.namespaces === 1 ? "" : "s"} you hold a grant on. A write
          is any operation that puts knowledge into a namespace — a memory, a captured observation, a seeded
          conversation, a typed record. A recall is a <code className="code-inline">memory.search</code> or a code read.
          A cross-namespace recall is a search that fanned out across your own namespaces (
          <code className="code-inline">namespace_scope=all_mine</code>), not only the one it was addressed to.
        </p>
      </section>

      {/* WHAT YOU'RE SHARING — namespace membership with member count
          and your 7-day activity. Rows ordered by recent activity
          (heat) so high-traffic spaces float to the top. */}
      <section className="mb-8">
        <Title type="h2" size="sm" className="mb-3">
          What you're sharing
        </Title>
        {namespaces.length === 0 ? (
          <div className="text-sm text-text-muted py-3">No namespace memberships yet.</div>
        ) : (
          <div className="space-y-2">
            {[...namespaces]
              .sort((a, b) => b.writes_7d + b.recalls_7d - (a.writes_7d + a.recalls_7d))
              .map((n) => (
                <div
                  key={n.namespace}
                  className="p-3.5 rounded-xl border border-border bg-surface flex items-center gap-3"
                >
                  <Icon
                    name={n.visibility === "public" ? "globe" : "lock"}
                    size="sm"
                    aria-hidden="true"
                    className="shrink-0"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm text-text truncate">{n.namespace}</span>
                      <RoleBadge role={n.role} />
                      <span className="text-xs text-text-muted">{n.visibility}</span>
                    </div>
                    <div className="mt-1 text-xs text-text-muted tabular flex items-center gap-3">
                      <span className="inline-flex items-center gap-1">
                        <Icon name="users" size="xs" aria-hidden="true" />
                        {n.member_count} member{n.member_count === 1 ? "" : "s"}
                      </span>
                      <span>·</span>
                      <span>
                        {n.writes_7d.toLocaleString()} write{n.writes_7d === 1 ? "" : "s"}
                      </span>
                      <span>
                        {n.recalls_7d.toLocaleString()} recall{n.recalls_7d === 1 ? "" : "s"}
                      </span>
                    </div>
                  </div>
                </div>
              ))}
          </div>
        )}
      </section>

      {repos.length > 0 && (
        <section className="mb-8">
          <Title type="h2" size="sm" className="mb-3">
            Repositories you read
          </Title>
          {/* One row per repository; the branches you read beneath it. */}
          <div className="space-y-2">
            {Array.from(
              repos
                .reduce((by, n) => {
                  const info = repoInfo.get(n.namespace);
                  const key = info?.repo ?? n.namespace;
                  const grp = by.get(key) ?? { repo: key, rule: info?.join_policy, rows: [] as typeof repos };
                  grp.rows.push(n);
                  if (!grp.rule && info?.join_policy) grp.rule = info.join_policy;
                  by.set(key, grp);
                  return by;
                }, new Map<string, { repo: string; rule?: string; rows: typeof repos }>())
                .values(),
            ).map((grp) => {
              const reads = grp.rows.reduce((a, n) => a + n.recalls_7d, 0);
              const readers = Math.max(...grp.rows.map((n) => n.member_count));
              return (
                <div key={grp.repo} className="p-3.5 rounded-xl border border-border bg-surface flex items-start gap-3">
                  <Icon name="code" size="sm" aria-hidden="true" className="shrink-0 mt-0.5" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-text truncate">{grp.repo}</span>
                      {grp.rule && (
                        <Badge
                          size="sm"
                          state={grp.rule === "open" ? "success" : "warning"}
                          text={grp.rule === "open" ? "open" : grp.rule === "closed" ? "closed" : "restricted"}
                        />
                      )}
                      <span className="text-xs text-text-muted">read-only</span>
                    </div>
                    <div className="mt-1 flex items-center gap-1.5 flex-wrap">
                      {grp.rows.map((n) => {
                        const branch = repoInfo.get(n.namespace)?.branch;
                        const badge = (
                          <Badge
                            size="sm"
                            state="neutral"
                            text={<span className="font-mono">{branch ?? n.namespace}</span>}
                          />
                        );
                        return branch ? (
                          <TipTarget key={n.namespace} tip={n.namespace}>
                            {badge}
                          </TipTarget>
                        ) : (
                          <span key={n.namespace}>{badge}</span>
                        );
                      })}
                    </div>
                    <div className="mt-1 text-xs text-text-muted tabular">
                      {grp.rows.length} branch{grp.rows.length === 1 ? "" : "es"} you read · {readers.toLocaleString()}{" "}
                      reader{readers === 1 ? "" : "s"} incl. agents · {reads.toLocaleString()} read
                      {reads === 1 ? "" : "s"} in {window_days} days
                    </div>
                  </div>
                  <Button type="empty" htmlType="button" className="shrink-0" onClick={() => setView("code")}>
                    Open in Repositories
                  </Button>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* TOOL ACTIVITY — per-agent writes vs recalls inside the namespace
          this page shows, with a switch to every namespace
          the summary covers. The asymmetry IS the story: an agent that
          recalls far more than it writes is a consumer; writes-heavy agents
          are producers. */}
      {(() => {
        const scope = toolActivityRows(data, identity.namespace, showAllNamespaces);
        const withNamespace = scope.kind === "all" || scope.kind === "outside";
        const rows = scope.rows;
        return (
          <section className="mb-8">
            <div className="flex items-baseline justify-between gap-3 mb-3 flex-wrap">
              <Title type="h2" size="sm">
                Tool activity
                {scope.kind === "namespace" && (
                  <span className="ml-2 font-mono font-normal text-xs">in {scope.namespace}</span>
                )}
                {withNamespace && (
                  <span className="ml-2 font-normal text-xs">
                    across {scope.namespaces} namespace{scope.namespaces === 1 ? "" : "s"} you can read
                  </span>
                )}
              </Title>
              {scope.kind === "namespace" && scope.others > 0 && (
                <Button type="empty" htmlType="button" onClick={() => setShowAllNamespaces(true)}>
                  Show {scope.others} other namespace{scope.others === 1 ? "" : "s"}
                </Button>
              )}
              {scope.kind === "all" && identity.namespace && (
                <Button type="empty" htmlType="button" onClick={() => setShowAllNamespaces(false)}>
                  Only {identity.namespace}
                </Button>
              )}
            </div>
            {scope.kind === "outside" && (
              <p className="text-xs text-text-muted mb-3 leading-relaxed">
                <span className="font-mono">{scope.namespace}</span> is not among the namespaces you hold a grant on, so
                this page has no activity for it. Showing every namespace the summary covers instead.
              </p>
            )}
            <Table
              size="sm"
              noDataMessage={
                scope.kind === "namespace"
                  ? `No tool activity in ${scope.namespace} in the last ${window_days} days.`
                  : "No agent activity yet."
              }
              headers={[
                { label: "Agent" },
                ...(withNamespace ? [{ label: "Namespace" }] : []),
                { label: "Writes" },
                { label: "Recalls" },
                {
                  label: "Fan-out",
                  title: "Searches that fanned out across your own namespaces (namespace_scope=all_mine)",
                },
                { label: "Pattern" },
              ]}
              rows={rows.map((a) => {
                const isSentinel = SENTINEL_AGENTS.has(a.agent);
                const ns = "namespace" in a ? a.namespace : "";
                return {
                  id: `${a.agent}::${ns}`,
                  items: [
                    {
                      label: (
                        <span className={isSentinel ? "italic text-text-muted" : "font-mono"}>
                          {displayAgent(a.agent)}
                        </span>
                      ),
                    },
                    ...(withNamespace
                      ? [{ label: <span className="font-mono text-xs text-text-muted">{ns}</span> }]
                      : []),
                    { label: <span className="tabular">{a.writes.toLocaleString()}</span> },
                    { label: <span className="tabular">{a.recalls.toLocaleString()}</span> },
                    { label: <span className="tabular">{a.cross_namespace_recalls.toLocaleString()}</span> },
                    { label: <span className="text-xs text-text-muted">{isSentinel ? "—" : agentPattern(a)}</span> },
                  ],
                };
              })}
            />
          </section>
        );
      })()}
    </PageContainer>
  );
}

/**
 * Bucket an agent into producer / consumer / mixed by the
 * writes:recalls ratio. Heuristic — the goal is a one-glance label,
 * not a calibrated metric.
 */
function agentPattern(a: { writes: number; recalls: number }): string {
  if (a.writes === 0 && a.recalls === 0) return "—";
  if (a.writes === 0) return "consumer";
  if (a.recalls === 0) return "producer";
  const ratio = a.recalls / Math.max(1, a.writes);
  if (ratio >= 3) return "consumer (reads >> writes)";
  if (ratio <= 0.33) return "producer (writes >> reads)";
  return "mixed";
}

const ROLE_STATE: Record<string, BadgeState> = { admin: "info", writer: "success" };

function RoleBadge({ role }: { role: string }) {
  return <Badge size="sm" state={ROLE_STATE[role] ?? "neutral"} text={role} />;
}
