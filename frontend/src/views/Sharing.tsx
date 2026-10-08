import { useState } from "react";
import { toolActivityRows } from "../lib/flowRows";
import { useAbortableEffect } from "../lib/hooks";
import { Share2, Globe, Lock, Users, Code } from "lucide-react";
import { getMySharingSummary, type SharingSummary, type CodeRepoSummary } from "../api";
import { sharedCodeRepos, sharedMine } from "../lib/sharedReads";
import { useStore, useIdentity } from "../lib/state";
import { canCallCodeRepos } from "../lib/canCall";
import { Skeleton } from "../components/Skeleton";
import { EmptyState } from "../components/EmptyState";
import { displayAgent, humanizeError, SENTINEL_AGENTS } from "../lib/format";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";

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
    <>
      <h1 className="text-[30px] font-semibold tracking-tight mb-1">Flow</h1>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
    </>
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
        <p className="text-[14px] text-[var(--color-text-muted)] mb-6">Couldn't load activity: {error}</p>
      </PageContainer>
    );
  }

  if (!data) {
    return (
      <PageContainer>
        {heading}
        <p className="text-[14px] text-[var(--color-text-muted)] mb-6">Loading activity…</p>
        <div className="space-y-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
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
        <p className="text-[14px] text-[var(--color-text-muted)] mb-6">
          Where your memories flow — which tools wrote, which read, and which namespaces you share with.
        </p>
        <EmptyState
          icon={Share2}
          title="No activity yet"
          description="Once your agents start writing memories — or you join a public namespace from Spaces (namespace picker → Browse and join) — the cross-tool flow will appear here."
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      {heading}
      <p className="text-[14px] text-[var(--color-text-muted)] mb-6">
        Where your memories flow — which tools wrote, which read, and which namespaces you share with.
      </p>

      <section className="mb-8">
        <h2 className="text-[12px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-3">
          Last {window_days} days
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatCard label="Writes" value={totals.writes} />
          <StatCard label="Recalls" value={totals.recalls} />
          <StatCard label="Cross-namespace recalls" value={totals.cross_namespace_recalls} accent />
          <StatCard label="Active namespaces" value={totals.namespaces} />
        </div>
        <p className="mt-2 text-[11px] text-[var(--color-text-muted)] leading-relaxed">
          Totals span the {totals.namespaces} namespace{totals.namespaces === 1 ? "" : "s"} you hold a grant on. A write
          is any operation that puts knowledge into a namespace — a memory, a captured observation, a seeded
          conversation, a typed record. A recall is a <code className="font-mono">memory.search</code> or a code read. A
          cross-namespace recall is a search that fanned out across every namespace you can read (
          <code className="font-mono">namespace_scope=all_mine</code>) rather than the one it was addressed to.
        </p>
      </section>

      {/* WHAT YOU'RE SHARING — namespace membership with member count
          and your 7-day activity. Rows ordered by recent activity
          (heat) so high-traffic spaces float to the top. */}
      <section className="mb-8">
        <h2 className="text-[12px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-3">
          What you're sharing
        </h2>
        {namespaces.length === 0 ? (
          <div className="text-[13px] text-[var(--color-text-muted)] py-3">No namespace memberships yet.</div>
        ) : (
          <div className="space-y-2">
            {[...namespaces]
              .sort((a, b) => b.writes_7d + b.recalls_7d - (a.writes_7d + a.recalls_7d))
              .map((n) => (
                <div
                  key={n.namespace}
                  className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-center gap-3"
                >
                  {n.visibility === "public" ? (
                    <Globe size={14} className="text-[var(--color-text-muted)] shrink-0" />
                  ) : (
                    <Lock size={14} className="text-[var(--color-text-muted)] shrink-0" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[13px] text-[var(--color-text)] truncate">{n.namespace}</span>
                      <RoleBadge role={n.role} />
                      <span className="text-[11px] text-[var(--color-text-muted)]">{n.visibility}</span>
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--color-text-muted)] tabular flex items-center gap-3">
                      <span className="inline-flex items-center gap-1">
                        <Users size={10} />
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
          <h2 className="text-[12px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)] mb-3">
            Repositories you read
          </h2>
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
                <div
                  key={grp.repo}
                  className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-start gap-3"
                >
                  <Code size={14} className="text-[var(--color-text-muted)] shrink-0 mt-0.5" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[13px] font-medium text-[var(--color-text)] truncate">{grp.repo}</span>
                      {grp.rule && (
                        <span
                          className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded ${grp.rule === "open" ? "bg-[var(--color-ok)]/15 text-[var(--color-ok)]" : "bg-[var(--color-warn)]/15 text-[var(--color-warn)]"}`}
                        >
                          {grp.rule === "open" ? "open" : grp.rule === "closed" ? "closed" : "restricted"}
                        </span>
                      )}
                      <span className="text-[11px] text-[var(--color-text-muted)]">read-only</span>
                    </div>
                    <div className="mt-1 flex items-center gap-1.5 flex-wrap">
                      {grp.rows.map((n) => (
                        <span
                          key={n.namespace}
                          className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)]"
                          title={n.namespace}
                        >
                          {repoInfo.get(n.namespace)?.branch ?? n.namespace}
                        </span>
                      ))}
                    </div>
                    <div className="mt-1 text-[11px] text-[var(--color-text-muted)] tabular">
                      {grp.rows.length} branch{grp.rows.length === 1 ? "" : "es"} you read · {readers.toLocaleString()}{" "}
                      reader{readers === 1 ? "" : "s"} incl. agents · {reads.toLocaleString()} read
                      {reads === 1 ? "" : "s"} in {window_days} days
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setView("code")}
                    className="text-[12px] text-[var(--color-accent)] hover:underline shrink-0"
                  >
                    Open in Repositories
                  </button>
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
              <h2 className="text-[12px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">
                Tool activity
                {scope.kind === "namespace" && (
                  <span className="ml-2 normal-case tracking-normal font-mono text-[11px]">in {scope.namespace}</span>
                )}
                {withNamespace && (
                  <span className="ml-2 normal-case tracking-normal font-normal text-[11px]">
                    across {scope.namespaces} namespace{scope.namespaces === 1 ? "" : "s"} you can read
                  </span>
                )}
              </h2>
              {scope.kind === "namespace" && scope.others > 0 && (
                <button
                  type="button"
                  onClick={() => setShowAllNamespaces(true)}
                  className="text-[12px] text-[var(--color-accent)] hover:underline"
                >
                  Show {scope.others} other namespace{scope.others === 1 ? "" : "s"}
                </button>
              )}
              {scope.kind === "all" && identity.namespace && (
                <button
                  type="button"
                  onClick={() => setShowAllNamespaces(false)}
                  className="text-[12px] text-[var(--color-accent)] hover:underline"
                >
                  Only {identity.namespace}
                </button>
              )}
            </div>
            {scope.kind === "outside" && (
              <p className="text-[12px] text-[var(--color-text-muted)] mb-3 leading-relaxed">
                <span className="font-mono">{scope.namespace}</span> is not among the namespaces you hold a grant on, so
                this page has no activity for it. Showing every namespace the summary covers instead.
              </p>
            )}
            {rows.length === 0 ? (
              <div className="text-[13px] text-[var(--color-text-muted)] py-3">
                {scope.kind === "namespace"
                  ? `No tool activity in ${scope.namespace} in the last ${window_days} days.`
                  : "No agent activity yet."}
              </div>
            ) : (
              <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-x-auto">
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] border-b border-[var(--color-border)]">
                      <th className="px-3.5 py-2 font-semibold">Agent</th>
                      {withNamespace && <th className="px-3.5 py-2 font-semibold">Namespace</th>}
                      <th className="px-3.5 py-2 font-semibold text-right">Writes</th>
                      <th className="px-3.5 py-2 font-semibold text-right">Recalls</th>
                      <th
                        className="px-3.5 py-2 font-semibold text-right"
                        title="Searches that fanned out across every namespace you can read (namespace_scope=all_mine)"
                      >
                        Fan-out
                      </th>
                      <th className="px-3.5 py-2 font-semibold">Pattern</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((a) => {
                      const label = displayAgent(a.agent);
                      const isSentinel = SENTINEL_AGENTS.has(a.agent);
                      const ns = "namespace" in a ? a.namespace : "";
                      return (
                        <tr
                          key={`${a.agent}::${ns}`}
                          className="border-t border-[var(--color-border)] first:border-t-0"
                        >
                          <td
                            className={`px-3.5 py-2.5 truncate ${isSentinel ? "italic text-[var(--color-text-muted)]" : "font-mono text-[var(--color-text)]"}`}
                          >
                            {label}
                          </td>
                          {withNamespace && (
                            <td className="px-3.5 py-2.5 font-mono text-[12px] text-[var(--color-text-muted)] truncate">
                              {ns}
                            </td>
                          )}
                          <td className="px-3.5 py-2.5 text-right tabular">{a.writes.toLocaleString()}</td>
                          <td className="px-3.5 py-2.5 text-right tabular">{a.recalls.toLocaleString()}</td>
                          <td className="px-3.5 py-2.5 text-right tabular">
                            {a.cross_namespace_recalls.toLocaleString()}
                          </td>
                          <td className="px-3.5 py-2.5 text-[11px] text-[var(--color-text-muted)]">
                            {isSentinel ? "—" : agentPattern(a)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
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

function StatCard({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <div
      className={`p-4 rounded-xl border ${
        accent
          ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]"
          : "border-[var(--color-border)] bg-[var(--color-surface)]"
      }`}
    >
      <div className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)]">{label}</div>
      <div
        className={`mt-1 text-[24px] font-semibold tabular ${accent ? "text-[var(--color-accent)]" : "text-[var(--color-text)]"}`}
      >
        {value.toLocaleString()}
      </div>
    </div>
  );
}

function RoleBadge({ role }: { role: string }) {
  const tone =
    role === "admin"
      ? "bg-[var(--color-accent-soft)] text-[var(--color-accent)]"
      : role === "writer"
        ? "bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300"
        : "bg-[var(--color-surface-2)] text-[var(--color-text-muted)]";
  return <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${tone}`}>{role}</span>;
}
