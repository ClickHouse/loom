/**
 * Code — the indexed-repo plane.
 *
 * Not memory: a code namespace is one repo@branch, read-only, machine-built,
 * pinned to a commit, with a health pipeline behind it. So it gets a section
 * of its own instead of a slot in the namespace picker. Repos list → one repo
 * with four tabs: Health (watermarks, lag, workers, CI breadcrumb), Explore
 * (search/symbol/callers — the same ops agents call), PR packs (precomputed
 * per-PR context), Access (who can read it, how tokens get the claim).
 */
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Code,
  GitBranch,
  GitCommitHorizontal,
  Activity,
  Search,
  Package,
  ShieldCheck,
  RefreshCw,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  Plus,
  Trash2,
  Cpu,
  Network,
  Database,
  GitMerge,
  Braces,
  Globe,
  Lock,
  Settings,
  AlertTriangle,
  History,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { PageContainer } from "../layout/PageContainer";
import { UserPlus, Clock, Check } from "lucide-react";
import { useStore, useIdentity } from "../lib/state";
import { holdsAdminGrant } from "../lib/role";
import { fmtInt, relativeTime, humanizeError, refreshDetail } from "../lib/format";
import { useAbortableEffect, useVisiblePoll } from "../lib/hooks";
import { EmptyState } from "../components/EmptyState";
import { Skeleton, SkeletonCard } from "../components/Skeleton";
import { toast } from "../components/Toast";
import { stageFlips } from "../lib/codeStage";
import { confirmAction } from "../components/ConfirmDialog";
import {
  listCodeRepos,
  codeRefresh,
  codeSearch,
  codeSymbol,
  codeCallers,
  codeCallees,
  codeMap,
  listCodeContextPacks,
  codeContextPack,
  listGrants,
  createGrant,
  revokeGrant,
  getNamespace,
  codeCoverage,
  codeIndexErrors,
  codeRecentCommits,
  listCostRollup,
  joinNamespace,
  cancelNamespaceRequest,
  leaveNamespace,
  approveGrant,
  reregisterNamespace,
  addCodeRepo,
  addCodeBranch,
  removeCodeRepo,
  setCodeRepoAccess,
  setCodeRepoSource,
  type CodeRepoSummary,
  type CodeIndexStatus,
  type CodeUnit,
  type CodeEdge,
  type CodeContextPackRow,
  type GrantEntry,
  type GrantRole,
  type CodeId,
  type NamespaceInfo,
  type CodeCoverage,
  type CodeIndexErrorRow,
  type CodeRecentCommitRow,
  type CostRow,
  ApiError,
  remoteBranches,
  type CodeRepoRemoteBranches,
  type CodeWorkerStatus,
} from "../api";
import { invalidateShared, sharedHealth, sharedIndexStatus } from "../lib/sharedReads";
import { NamespaceSettingsDialog } from "../flows/NamespaceSettingsDialog";
import { joinTarget, withdrawTarget, leaveTarget, type JoinTarget } from "../lib/joinActions";
import { freshness, type Fresh } from "../lib/codeFreshness";
import { tierCards, type TierIcon } from "../lib/compilerTiers";
import { branchState, branchOrder, buildCards, type CodeFacet } from "../lib/repositoryCard";
import { RepositoryCard, BranchChip, BTN, BTN_TONE, type CodeActions, type RecordActionProps } from "./RepositoryCard";
import { useTrackerSources, useRecordActions, TRACKER_SOURCES_ABSENT } from "./TrackerSources";
import { TrackersPanel } from "./TrackersPanel";
import { Tip } from "../components/ui";
import { isRecord } from "../lib/guards";
import { oneOf } from "../lib/oneOf";

// The register dialog loads on first open.
const RegisterTrackerSourceDialog = lazy(() =>
  import("../flows/TrackerSourceDialogs").then((m) => ({ default: m.RegisterTrackerSourceDialog })),
);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const short = (sha: string) => (sha || "").slice(0, 12);

const TONE: Record<Fresh["tone"], string> = {
  ok: "text-[var(--color-success)] border-[var(--color-success)]/30 bg-[var(--color-success)]/10",
  warn: "text-[var(--color-warn)] border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10",
  bad: "text-[var(--color-danger)] border-[var(--color-danger)]/30 bg-[var(--color-danger)]/10",
  muted: "text-[var(--color-text-subtle)] border-[var(--color-border)] bg-[var(--color-surface-2)]",
};

function Pill({ tone, children, title }: { tone: Fresh["tone"]; children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 text-[11px] font-medium rounded-md border tabular ${TONE[tone]}`}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-current" />
      {children}
    </span>
  );
}

function Mono({ children, className = "", title }: { children: React.ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={`font-mono text-[12px] ${className}`}>
      {children}
    </span>
  );
}

function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="section-label">{label}</div>
      <div className="text-[22px] font-semibold tabular mt-1">{value}</div>
      {hint && <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">{hint}</div>}
    </div>
  );
}

function readRepoParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("repo");
}

function writeRepoParam(ns: string | null) {
  const url = new URL(window.location.href);
  if (ns) url.searchParams.set("repo", ns);
  else url.searchParams.delete("repo");
  window.history.replaceState(null, "", url.toString());
}

// ---------------------------------------------------------------------------
// Repos list
// ---------------------------------------------------------------------------

export function CodeView() {
  const identity = useIdentity();
  const [repos, setRepos] = useState<CodeRepoSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(readRepoParam);

  // The page announces a first index that became searchable or failed between two polls
  // (the approvals bell has no generic feed yet).
  const seen = useRef<CodeRepoSummary[] | null>(null);
  const load = useCallback(
    (signal?: AbortSignal, live: () => boolean = () => true) => {
      listCodeRepos(identity.org, { signal })
        .then((rows) => {
          if (!live()) return;
          for (const f of stageFlips(seen.current, rows)) {
            if (f.to === "ready") toast(`${f.repo} @ ${f.branch} is ready to search`, "ok");
            else toast(`${f.repo} @ ${f.branch}: first index failed${f.reason ? ` — ${f.reason}` : ""}`, "error");
          }
          seen.current = rows;
          setRepos(rows);
          setError(null);
        })
        .catch((e) => {
          if (live()) setError(humanizeError(e));
        });
    },
    [identity.org],
  );

  useAbortableEffect(
    (signal, live) => {
      setRepos(null);
      load(signal, live);
    },
    [load],
  );
  // A repository's stage changes over minutes, not seconds.
  useVisiblePoll(load, 120_000);

  const select = (ns: string | null) => {
    setSelected(ns);
    writeRepoParam(ns);
  };

  const current = useMemo(
    () => repos?.find((r) => r.namespace === selected && r.readable !== false) ?? null,
    [repos, selected],
  );

  const siblings = useMemo(
    () => (current ? (repos ?? []).filter((r) => r.repo === current.repo) : []),
    [repos, current],
  );

  if (selected && current) {
    return (
      <RepoDetail
        repo={current}
        siblings={siblings}
        onBack={() => select(null)}
        onSwitch={(ns) => select(ns)}
        onJoined={load}
      />
    );
  }

  return <RepositoriesHome repos={repos} error={error} onOpen={(ns) => select(ns)} onChanged={load} />;
}

// ---------------------------------------------------------------------------
// Repositories home. One card per repository; each branch is a chip that carries its own
// state. A person is joined to nothing by default: an open repository joins in one click,
// a restricted one is requested and a maintainer approves. Every surface here says
// read-only.
// ---------------------------------------------------------------------------

/** The two roles a repository has: viewer = reader, admin = maintainer. */
type RepoRole = "viewer" | "admin";
const ROLE_WORD: Record<RepoRole, string> = { viewer: "reader", admin: "maintainer" };
const REPO_ROLES: ReadonlyArray<{ value: RepoRole; label: string }> = [
  { value: "viewer", label: "reader (read-only)" },
  { value: "admin", label: "maintainer (approves readers, sets the rule)" },
];

/** What a join names for a branch: "repo @ branch". */
const branchTarget = (r: CodeRepoSummary): JoinTarget => ({
  org: r.org,
  namespace: r.namespace,
  label: `${r.repo} @ ${r.branch}`,
});

/** The repository group is the card's Code facet. */
type RepoGroup = CodeFacet;

function useJoinActions(onChanged: () => void) {
  const [busy, setBusy] = useState<string | null>(null);
  // Join, withdraw and leave — and their toasts — are the shared helpers in
  // lib/joinActions.ts, the same ones the record facet uses.
  const join = async (r: CodeRepoSummary, role: RepoRole = "viewer") => {
    setBusy(r.namespace);
    try {
      if (await joinTarget(branchTarget(r), role)) onChanged();
    } finally {
      setBusy(null);
    }
  };
  const cancel = async (r: CodeRepoSummary) => {
    setBusy(r.namespace);
    try {
      if (await withdrawTarget(branchTarget(r))) onChanged();
    } finally {
      setBusy(null);
    }
  };
  const leave = async (r: CodeRepoSummary) => {
    setBusy(r.namespace);
    try {
      if (await leaveTarget(branchTarget(r))) onChanged();
    } finally {
      setBusy(null);
    }
  };
  /** One pass over many branches: join (instant or request), leave, withdraw.
   *  Errors never stop the pass; the toast says what happened in numbers. */
  const applyPlan = async (plan: {
    join: CodeRepoSummary[];
    leave: CodeRepoSummary[];
    withdraw: CodeRepoSummary[];
  }) => {
    const total = plan.join.length + plan.leave.length + plan.withdraw.length;
    if (total === 0) return;
    setBusy("__all__");
    let joined = 0,
      requested = 0,
      left = 0,
      withdrawn = 0,
      failed = 0;
    for (const b of plan.join) {
      try {
        const res = await joinNamespace(b.org, b.namespace, { role: "viewer" });
        if (res.status === "pending") requested++;
        else joined++;
      } catch {
        failed++;
      }
    }
    for (const b of plan.leave) {
      try {
        await leaveNamespace(b.org, b.namespace);
        left++;
      } catch {
        failed++;
      }
    }
    for (const b of plan.withdraw) {
      try {
        await cancelNamespaceRequest(b.org, b.namespace);
        withdrawn++;
      } catch {
        failed++;
      }
    }
    const parts = [
      joined && `joined ${joined}`,
      requested && `requested ${requested}`,
      left && `left ${left}`,
      withdrawn && `withdrew ${withdrawn}`,
      failed && `${failed} failed`,
    ].filter((x): x is string => typeof x === "string");
    toast(parts.join(" · ") + (joined || requested ? " · read-only" : ""), failed ? "error" : "ok");
    setBusy(null);
    onChanged();
  };
  const joinAll = async (branches: CodeRepoSummary[]) => {
    const targets = branches.filter((b) => branchState(b) === "open" || branchState(b) === "approval");
    if (targets.length === 0) return;
    const asks = targets.filter((b) => branchState(b) === "approval").length;
    if (
      asks > 0 &&
      !(await confirmAction({
        title: `Join all ${targets.length} branches?`,
        message:
          asks === targets.length
            ? `This repository is restricted: ${asks} request${asks === 1 ? "" : "s"} go to its maintainers; you read nothing until they approve.`
            : `${targets.length - asks} join instantly; ${asks} go to a maintainer for approval.`,
        confirmLabel: asks === targets.length ? "Request all" : "Join all",
      }))
    )
      return;
    await applyPlan({ join: targets, leave: [], withdraw: [] });
  };
  const leaveAll = async (branches: CodeRepoSummary[]) => {
    const targets = branches.filter((b) => branchState(b) === "member");
    if (targets.length === 0) return;
    if (
      !(await confirmAction({
        title: `Leave all ${targets.length} branches of ${targets[0].repo}?`,
        message: `${targets.map((b) => b.branch).join(", ")}. You stop seeing these indexes and new tokens no longer include them. You can join again any time.`,
        confirmLabel: "Leave all",
        danger: true,
      }))
    )
      return;
    await applyPlan({ join: [], leave: targets, withdraw: [] });
  };
  return { busy, join, cancel, leave, joinAll, leaveAll, applyPlan };
}

/** How `/v1/health` reports a deployment without the repository-job sandbox, where adding
 *  a repository or branch from the page is refused (`SANDBOX_UNAVAILABLE`): "warn" while
 *  LOOM_CODE_REPOS_JSON entries still index, "error" when LOOM_CODE_SANDBOX_REQUIRED
 *  refuses them too, null when the sandbox runs or health has not said. */
export function sandboxProblem(health: Record<string, unknown> | null): "warn" | "error" | null {
  const problems = health?.problems;
  if (!Array.isArray(problems)) return null;
  const found = problems.find((p) => isRecord(p) && p.code === "repo_jobs_unsandboxed");
  return isRecord(found) && (found.severity === "warn" || found.severity === "error") ? found.severity : null;
}

const sandboxRefused = (err: unknown): boolean => err instanceof ApiError && err.code === "SANDBOX_UNAVAILABLE";

/** null until health reports the sandbox off, so a slow or failed read never hides the
 *  add buttons. `recheck` reads health again, for when the server refuses an add. */
function useSandboxProblem(): { problem: "warn" | "error" | null; recheck: () => void } {
  const [problem, setProblem] = useState<"warn" | "error" | null>(null);
  const [checks, setChecks] = useState(0);
  useAbortableEffect(
    async (_signal, live) => {
      const health = await sharedHealth();
      if (live()) setProblem(sandboxProblem(health));
    },
    [checks],
  );
  const recheck = useCallback(() => {
    invalidateShared("health");
    setChecks((n) => n + 1);
  }, []);
  return { problem, recheck };
}

function RepositoriesHome({
  repos,
  error,
  onOpen,
  onChanged,
}: {
  repos: CodeRepoSummary[] | null;
  error: string | null;
  onOpen: (ns: string) => void;
  onChanged: () => void;
}) {
  const actions = useJoinActions(onChanged);
  const [requesting, setRequesting] = useState<{ repo: CodeRepoSummary; role: RepoRole } | null>(null);
  const [adding, setAdding] = useState(false);
  const { problem: sandbox, recheck: recheckSandbox } = useSandboxProblem();
  const canAdd = sandbox === null;
  const [addingBranch, setAddingBranch] = useState<RepoGroup | null>(null);
  const [managing, setManaging] = useState<RepoGroup | null>(null);
  // The add-a-tracker dialog, from a card (that repository) or the page (none)
  const [registering, setRegistering] = useState<{ group: RepoGroup | null } | null>(null);
  const org = useStore((s) => s.identity)?.org ?? repos?.[0]?.org ?? "";
  const [query, setQuery] = useState("");
  // The record facet — the tracker namespaces whose `repos` name a repository — is read
  // once here and joined onto the cards by repository.
  const tracker = useTrackerSources(org);
  const record = useRecordActions(org, tracker.refresh);
  const cards = useMemo(() => buildCards(repos ?? [], tracker.sources), [repos, tracker.sources]);
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return cards;
    return cards.filter(
      (c) =>
        c.repo.toLowerCase().includes(q) ||
        (c.code?.branches ?? []).some(
          (b) => b.branch.toLowerCase().includes(q) || b.namespace.toLowerCase().includes(q),
        ) ||
        (c.record?.namespaces ?? []).some((n) => n.ns.namespace.toLowerCase().includes(q)),
    );
  }, [cards, query]);
  const totals = useMemo(() => {
    const all = repos ?? [];
    return {
      repos: cards.length,
      branches: all.length,
      joined: all.filter((r) => branchState(r) === "member").length,
      pending: all.filter((r) => branchState(r) === "pending").length,
    };
  }, [repos, cards]);
  const pendingRows = (repos ?? []).filter((r) => branchState(r) === "pending");
  const firstJoin =
    cards.find((c) => c.code && c.code.joinedCount === 0 && branchState(c.code.primary) === "open")?.code ?? null;
  const busy = actions.busy ?? record.busy;
  const loaded = repos !== null && !error;
  const empty = loaded && repos.length === 0 && cards.length === 0;

  const bindSource = async (g: RepoGroup, tracker_namespaces: string[]) => {
    try {
      const out = await setCodeRepoSource(g.primary.org, g.primary.namespace, tracker_namespaces);
      // the card's tracker lines say what each link means (and warn inline); the toast only confirms
      toast(
        out.tracker_namespaces.length
          ? `${g.primary.repo} reads ${out.tracker_namespaces.join(" + ")}`
          : `${g.primary.repo} unlinked`,
        "ok",
      );
      onChanged();
    } catch (e) {
      toast(`Couldn't link the tracker: ${humanizeError(e)}`, "error");
    }
  };
  /** The links a repository holds now, primary first. */
  const linksOf = (g: RepoGroup): string[] => g.primary.bound_trackers ?? [];

  const codeActions: CodeActions = {
    onOpen: (b) => onOpen(b.namespace),
    onJoin: (b) => void actions.join(b),
    onRequest: (b, role) => setRequesting({ repo: b, role: role ?? "viewer" }),
    onCancel: (b) => void actions.cancel(b),
    onLeave: (b) => void actions.leave(b),
    onJoinAll: (bs) => void actions.joinAll(bs),
    onLeaveAll: (bs) => void actions.leaveAll(bs),
    onManage: (g) => setManaging(g),
    onAddBranch: canAdd ? (g) => setAddingBranch(g) : undefined,
    // Every branch the person maintains takes the new access; the QA binding is set on
    // the primary branch's namespace (the record is per repository)
    onSetAccess: (g, classification, join_policy, opening) => {
      void (async () => {
        const mine = g.branches.filter((b) => b.my_role === "admin");
        let confirm = "";
        if (opening) {
          const typed =
            window.prompt(
              `Opening ${g.primary.repo} to every reader of the org. Type the namespace (${g.primary.namespace}) to confirm.`,
            ) ?? "";
          if (typed.trim() !== g.primary.namespace) {
            toast("Not opened — the namespace did not match", "error");
            return;
          }
          confirm = typed.trim();
        }
        let done = 0;
        for (const b of mine) {
          try {
            await setCodeRepoAccess(
              b.org,
              b.namespace,
              classification,
              join_policy,
              opening ? (b.namespace === confirm ? confirm : b.namespace) : "",
            );
            done += 1;
          } catch (e) {
            toast(`${b.namespace}: ${humanizeError(e)}`, "error");
          }
        }
        if (done)
          toast(
            `${g.primary.repo}: ${classification}, ${join_policy} — ${done} branch${done === 1 ? "" : "es"}; every process follows within a minute`,
            "ok",
          );
        onChanged();
      })();
    },
    onSetSource: (g, tracker_namespaces) => void bindSource(g, tracker_namespaces),
    trackerSources: tracker.sources,
    onRegisterSource: tracker.selfService ? (g) => setRegistering({ group: g }) : undefined,
  };
  const recordActions: RecordActionProps = {
    onOpen: (r) => record.open(r.ns),
    onJoin: (r, confirm) => void record.join(r.ns, confirm),
    onRequest: (r) => void record.request(r.ns),
    onCancel: (r) => void record.cancel(r.ns),
    onLeave: (r) => void record.leave(r.ns),
    org,
    onSourcesChanged: () => {
      void tracker.refresh();
      onChanged();
    },
  };

  return (
    <PageContainer>
      <div className="mb-5 flex items-start justify-between gap-4 flex-wrap">
        <div className="min-w-0">
          <h1 className="text-3xl font-semibold tracking-tight inline-flex items-center gap-2">
            <Code size={20} aria-hidden="true" /> Repositories
          </h1>
          <p className="text-sm text-text-muted mt-1 max-w-2xl">
            One card per repository: its <strong className="font-medium text-text">code</strong> — indexed branches you
            can search and ask about — and its <strong className="font-medium text-text">tracker</strong> — issues and
            PRs, read live. Everything here is read-only. An <strong className="font-medium text-text">open</strong> one
            joins in one click; a <strong className="font-medium text-text">restricted</strong> one is requested and a
            maintainer approves.
          </p>
        </div>
        {org && (
          <div className="flex flex-wrap items-center gap-2 mt-2">
            {tracker.selfService && (
              <Tip content="Connect a ClickHouse database of issues, PRs and incidents. You become its admin.">
                <button
                  type="button"
                  onClick={() => setRegistering({ group: null })}
                  className={`${BTN} ${BTN_TONE.default}`}
                >
                  <Plus size={13} aria-hidden="true" /> Add a tracker
                </button>
              </Tip>
            )}
            {canAdd && (
              <Tip content="Index a Git repository. You become its maintainer.">
                <button
                  type="button"
                  onClick={() => setAdding(true)}
                  className={`${BTN} ${BTN_TONE.primary} shadow-sm`}
                >
                  <Plus size={13} aria-hidden="true" /> Add repository
                </button>
              </Tip>
            )}
          </div>
        )}
      </div>

      {loaded && repos.length > 0 && (
        <div className="mb-5 flex items-center gap-3 flex-wrap text-xs text-text-muted tabular-nums">
          <span>
            <span className="font-semibold text-text">{totals.repos}</span> repositor{totals.repos === 1 ? "y" : "ies"}
          </span>
          <span>·</span>
          <span>
            <span className="font-semibold text-text">{totals.joined}</span> of {totals.branches} branches you read
          </span>
          {totals.pending > 0 && (
            <>
              <span>·</span>
              <span className="text-warn">
                {totals.pending} request{totals.pending === 1 ? "" : "s"} waiting
              </span>
            </>
          )}
          <span className="ml-auto" />
          <label className="relative min-w-0">
            <Search
              size={12}
              aria-hidden="true"
              className="absolute left-2 top-1/2 -translate-y-1/2 text-text-subtle"
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter repositories or branches"
              aria-label="Filter repositories"
              className="pl-7 pr-2 min-h-9 text-xs rounded-md border border-border bg-surface outline-none focus:border-accent w-64 max-w-full pointer-coarse:min-h-11"
            />
          </label>
        </div>
      )}

      {pendingRows.length > 0 && (
        <div className="mb-5 p-3.5 rounded-lg border border-warn/40 bg-warn/10 text-sm flex items-start gap-3">
          <Clock size={15} aria-hidden="true" className="text-warn shrink-0 mt-0.5" />
          <div className="min-w-0 flex-1">
            <div className="font-medium">Waiting for a maintainer</div>
            <div className="text-xs text-text-muted mt-0.5 break-words">
              {pendingRows.map((r) => `${r.repo} @ ${r.branch}`).join(" · ")}. You'll be able to read as soon as a
              maintainer approves; nothing else to do.
            </div>
          </div>
        </div>
      )}

      {repos === null && !error && (
        <div className="space-y-3">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}
      {error && <EmptyState icon={Code} title="Couldn't load repositories" description={error} />}

      {empty && (
        <EmptyState
          icon={Code}
          title="No repositories yet"
          description={
            canAdd
              ? "Add the first one: a Git remote and a branch. You become its maintainer and the indexer builds it in the background."
              : sandbox === "warn"
                ? "This server cannot add repositories from this page, because it runs without the repository-job sandbox. Ask the operator to list them in LOOM_CODE_REPOS_JSON."
                : "This server cannot index repositories until its repository-job sandbox runs. Ask the operator."
          }
          action={org && canAdd ? { label: "Add repository", onClick: () => setAdding(true) } : undefined}
        />
      )}

      {loaded && repos.length > 0 && totals.joined === 0 && firstJoin && (
        <div className="mb-5 p-4 rounded-lg border border-accent/40 bg-accent-soft flex items-center gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold break-words">
              Start with {firstJoin.repo} @ {firstJoin.primary.branch}
            </div>
            <div className="text-xs text-text-muted mt-0.5">
              Open repository — joins instantly, read-only. Agents you mint a token for can then search and ask about
              it.
            </div>
          </div>
          <button
            type="button"
            onClick={() => void actions.join(firstJoin.primary)}
            disabled={busy !== null}
            className={`${BTN} ${BTN_TONE.primary} shadow-sm`}
          >
            <UserPlus size={13} aria-hidden="true" /> Join · {firstJoin.primary.branch}
          </button>
        </div>
      )}

      {loaded && visible.length > 0 && (
        // Folded rows, so the list is a compact stack; a card grows only when opened.
        <div className="space-y-2">
          {visible.map((c) => (
            <RepositoryCard
              key={c.repo}
              card={c}
              busy={busy}
              code={codeActions}
              record={recordActions}
              adminOpsPresent={tracker.selfService}
            />
          ))}
        </div>
      )}
      {loaded && cards.length > 0 && visible.length === 0 && (
        <div className="text-sm text-text-muted py-6">No repository or branch matches “{query}”.</div>
      )}

      {/* An absent op is a stated result, never an error banner. */}
      {tracker.status.kind === "absent" && (
        <p className="mt-4 text-xs text-text-muted" role="status">
          {TRACKER_SOURCES_ABSENT}
        </p>
      )}
      {tracker.status.kind === "error" && (
        <p className="mt-4 text-xs text-text-muted" role="status">
          Couldn't load the trackers — {tracker.status.message}
        </p>
      )}
      {(tracker.status.kind === "ready" || tracker.status.kind === "loading") && loaded && (
        <TrackersPanel
          org={org}
          sources={tracker.sources}
          selfService={tracker.selfService}
          loading={tracker.status.kind === "loading"}
          onAdd={() => setRegistering({ group: null })}
          onChanged={() => {
            void tracker.refresh();
            onChanged();
          }}
        />
      )}

      <JoinDialog
        target={requesting}
        onOpenChange={(v) => {
          if (!v) setRequesting(null);
        }}
        onConfirm={async (r, role) => {
          setRequesting(null);
          await actions.join(r, role);
        }}
      />
      <AddRepositoryDialog
        open={adding}
        org={org}
        onOpenChange={setAdding}
        onAdded={onChanged}
        onSandboxRefused={recheckSandbox}
      />
      <BranchesDialog
        group={managing}
        onOpenChange={(v) => {
          if (!v) setManaging(null);
        }}
        onApply={async (plan) => {
          setManaging(null);
          await actions.applyPlan(plan);
        }}
      />
      <AddBranchDialog
        group={addingBranch}
        onOpenChange={(v) => {
          if (!v) setAddingBranch(null);
        }}
        onAdded={onChanged}
        onSandboxRefused={recheckSandbox}
      />
      {registering && (
        <Suspense fallback={null}>
          <RegisterTrackerSourceDialog
            open
            org={org}
            repo={registering.group?.repo}
            takenNames={tracker.sources.map((x) => x.name)}
            takenNamespaces={tracker.sources.flatMap((x) => x.namespaces.map((n) => n.namespace))}
            onOpenChange={(v) => {
              if (!v) setRegistering(null);
            }}
            onRegistered={(ns) => {
              const g = registering.group;
              // A tracker added from a card is linked beside the ones it has
              void (async () => {
                await tracker.refresh({ fresh: true });
                if (g) await bindSource(g, [...linksOf(g).filter((x) => x !== ns), ns]);
              })();
            }}
          />
        </Suspense>
      )}
    </PageContainer>
  );
}

function JoinDialog({
  target,
  onOpenChange,
  onConfirm,
}: {
  target: { repo: CodeRepoSummary; role: RepoRole } | null;
  onOpenChange: (v: boolean) => void;
  onConfirm: (r: CodeRepoSummary, role: RepoRole) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [role, setRole] = useState<RepoRole>("viewer");
  useEffect(() => {
    if (target) setRole(target.role);
  }, [target]);
  const repo = target?.repo ?? null;
  const restricted = repo ? branchState(repo) === "approval" : false;
  // What the click does, said before the click: an open repository admits a
  // reader at once; anything else — a restricted repository, or asking to
  // maintain — goes to a current maintainer first.
  const instant = !!repo && !restricted && role === "viewer";
  // The five verbs — "Request access" for a restricted branch, "Request maintainer
  // access" for the role a current maintainer approves.
  const verb = instant ? "Join" : role === "admin" ? "Request maintainer access" : "Request access";
  return (
    <Dialog.Root open={repo !== null} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40 z-40" />
        <Dialog.Content className="fixed z-50 top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(92vw,500px)] rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6 shadow-xl outline-none">
          {repo && (
            <>
              <Dialog.Title className="text-[17px] font-semibold tracking-tight flex items-center gap-2">
                {restricted ? (
                  <Lock size={16} className="text-[var(--color-warn)]" />
                ) : (
                  <UserPlus size={16} className="text-[var(--color-accent)]" />
                )}
                Join {repo.repo}{" "}
                <Mono className="text-[13px] px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
                  {repo.branch}
                </Mono>
              </Dialog.Title>
              <Dialog.Description className="text-[13px] text-[var(--color-text-muted)] mt-2 leading-relaxed">
                {restricted ? (
                  <>
                    This repository is <b>restricted</b>. Your request goes to its maintainers; you read nothing until
                    one approves.
                  </>
                ) : (
                  <>
                    This repository is <b>open</b>: readers join instantly. Asking to maintain goes to a current
                    maintainer first.
                  </>
                )}{" "}
                Everything on a repository is read-only.
              </Dialog.Description>

              <fieldset className="mt-4 space-y-2" aria-label="Role">
                <legend className="text-[11px] uppercase tracking-wide text-[var(--color-text-subtle)] mb-1">
                  Join as
                </legend>
                {(
                  [
                    ["viewer", "Reader", "Search, ask, grep and explore the index."],
                    [
                      "admin",
                      "Maintainer",
                      "Approve readers, name maintainers, set how people join. Always needs a current maintainer's approval.",
                    ],
                  ] as const
                ).map(([v, label, desc]) => (
                  <label
                    key={v}
                    className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer ${role === v ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]" : "border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"}`}
                  >
                    <input
                      type="radio"
                      name="repo-role"
                      value={v}
                      checked={role === v}
                      onChange={() => setRole(v)}
                      className="mt-1"
                    />
                    <span className="min-w-0">
                      <span className="block text-[13px] font-medium">{label}</span>
                      <span className="block text-[12px] text-[var(--color-text-muted)]">{desc}</span>
                    </span>
                  </label>
                ))}
              </fieldset>

              <ul className="mt-3 text-[12px] text-[var(--color-text-muted)] space-y-1 list-disc pl-5">
                <li>Nothing is shared about you beyond your sign-in identity.</li>
                <li>
                  One approval covers you as a person: every token minted for you, and the service accounts you create,
                  read it without naming it.
                </li>
              </ul>
              <div className="mt-5 flex items-center justify-end gap-2">
                <Dialog.Close asChild>
                  <button className="px-3 py-1.5 text-[13px] rounded-md text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]">
                    Not now
                  </button>
                </Dialog.Close>
                <button
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await onConfirm(repo, role);
                    } finally {
                      setBusy(false);
                    }
                  }}
                  disabled={busy}
                  className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[13px] font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50"
                >
                  {instant ? <UserPlus size={13} /> : <Clock size={13} />} {verb}
                </button>
              </div>
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---------------------------------------------------------------------------
// Choose branches: one checklist for a whole repository. Each row is a branch with where
// you stand; the checkbox means "I read this". Ticking a branch you do not read joins it
// (or requests it, said inline); unticking one you read leaves it; unticking a request
// withdraws it. The footer counts what Apply will do, so nothing happens by surprise.
// ---------------------------------------------------------------------------

function BranchesDialog({
  group,
  onOpenChange,
  onApply,
}: {
  group: RepoGroup | null;
  onOpenChange: (v: boolean) => void;
  onApply: (plan: { join: CodeRepoSummary[]; leave: CodeRepoSummary[]; withdraw: CodeRepoSummary[] }) => Promise<void>;
}) {
  const [want, setWant] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (group)
      setWant(
        new Set(
          group.branches
            .filter((b) => branchState(b) === "member" || branchState(b) === "pending")
            .map((b) => b.namespace),
        ),
      );
  }, [group]);
  if (!group) return <Dialog.Root open={false} onOpenChange={onOpenChange} />;
  const rows = group.branches;
  const plan = {
    join: rows.filter((b) => want.has(b.namespace) && (branchState(b) === "open" || branchState(b) === "approval")),
    leave: rows.filter((b) => !want.has(b.namespace) && branchState(b) === "member"),
    withdraw: rows.filter((b) => !want.has(b.namespace) && branchState(b) === "pending"),
  };
  const requests = plan.join.filter((b) => branchState(b) === "approval").length;
  const changes = plan.join.length + plan.leave.length + plan.withdraw.length;
  const selectable = rows.filter((b) => branchState(b) !== "closed");
  const setAll = (on: boolean) => setWant(on ? new Set(selectable.map((b) => b.namespace)) : new Set());
  const rowWord = (b: CodeRepoSummary) => {
    const st = branchState(b);
    const on = want.has(b.namespace);
    if (st === "member") return on ? "reading" : "will leave";
    if (st === "pending") return on ? "requested" : "will withdraw";
    if (st === "open") return on ? "will join" : "not joined";
    if (st === "approval") return on ? "will request" : "not joined · needs approval";
    return "closed";
  };
  return (
    <Dialog.Root open={true} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40 z-40" />
        <Dialog.Content className="fixed z-50 top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(92vw,520px)] max-h-[90vh] flex flex-col rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] shadow-xl outline-none">
          <div className="p-6 pb-3">
            <Dialog.Title className="text-[17px] font-semibold tracking-tight flex items-center gap-2">
              <GitBranch size={16} /> Branches of {group.repo}
            </Dialog.Title>
            <Dialog.Description className="text-[13px] text-[var(--color-text-muted)] mt-1.5">
              Tick the branches you want to read. Everything is read-only;{" "}
              {group.restricted
                ? "this repository is restricted, so new branches go to a maintainer for approval"
                : "open branches join instantly"}
              .
            </Dialog.Description>
            <div className="mt-3 flex items-center gap-3 text-[12px]">
              <button type="button" onClick={() => setAll(true)} className="text-[var(--color-accent)] hover:underline">
                All {selectable.length}
              </button>
              <button
                type="button"
                onClick={() => setAll(false)}
                className="text-[var(--color-text-muted)] hover:underline"
              >
                None
              </button>
              <span className="text-[var(--color-text-subtle)]">·</span>
              <span className="text-[var(--color-text-muted)] tabular">
                {want.size} of {selectable.length} selected
              </span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto px-6" role="group" aria-label="Branches">
            <div className="rounded-xl border border-[var(--color-border)] divide-y divide-[var(--color-border)]">
              {rows.map((b) => {
                const st = branchState(b);
                const on = want.has(b.namespace);
                const disabled = st === "closed";
                const changed =
                  (st === "member" && !on) || (st === "pending" && !on) || ((st === "open" || st === "approval") && on);
                return (
                  <label
                    key={b.namespace}
                    className={`flex items-center gap-3 px-3 py-2 text-[13px] ${disabled ? "opacity-50" : "cursor-pointer hover:bg-[var(--color-surface-2)]"}`}
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      disabled={disabled}
                      onChange={(e) =>
                        setWant((prev) => {
                          const n = new Set(prev);
                          if (e.target.checked) n.add(b.namespace);
                          else n.delete(b.namespace);
                          return n;
                        })
                      }
                      className="accent-[var(--color-accent)]"
                      aria-label={`${b.branch}: ${rowWord(b)}`}
                    />
                    <span className="font-mono flex-1 truncate">{b.branch}</span>
                    {st === "approval" && <Lock size={11} className="text-[var(--color-warn)]" />}
                    <span
                      className={`text-[11px] tabular ${changed ? "font-medium text-[var(--color-text)]" : "text-[var(--color-text-muted)]"}`}
                    >
                      {rowWord(b)}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
          <div className="p-6 pt-4 flex items-center justify-between gap-3 flex-wrap">
            <span className="text-[12px] text-[var(--color-text-muted)] tabular">
              {changes === 0
                ? "No changes"
                : [
                    plan.join.length - requests > 0 && `join ${plan.join.length - requests}`,
                    requests > 0 && `request ${requests}`,
                    plan.leave.length > 0 && `leave ${plan.leave.length}`,
                    plan.withdraw.length > 0 && `withdraw ${plan.withdraw.length}`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
            </span>
            <div className="flex items-center gap-2">
              <Dialog.Close asChild>
                <button
                  type="button"
                  className="px-3 py-1.5 text-[13px] rounded-md text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="button"
                disabled={busy || changes === 0}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await onApply(plan);
                  } finally {
                    setBusy(false);
                  }
                }}
                className={`inline-flex items-center gap-1.5 px-3.5 py-2 text-[13px] font-medium rounded-md disabled:opacity-50 ${plan.leave.length > 0 && plan.join.length === 0 ? "bg-[var(--color-danger)] text-white hover:opacity-90" : "bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)]"}`}
              >
                {busy ? <RefreshCw size={13} className="animate-spin" /> : <Check size={13} />} Apply
                {changes ? ` ${changes} change${changes === 1 ? "" : "s"}` : ""}
              </button>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
// ---------------------------------------------------------------------------
// Adding a repository or a branch. Four questions, one button; the worker does the rest
// and the card shows its stage.
// ---------------------------------------------------------------------------

const FIELD =
  "w-full px-3 py-2 text-[13px] rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]";
const LABEL = "block text-[12px] font-medium text-[var(--color-text-muted)] mb-1";

type Producer = "" | "go" | "typescript" | "clang" | "python";
const PRODUCERS: ReadonlyArray<{ value: Producer; label: string }> = [
  { value: "", label: "Detect from the tree" },
  { value: "go", label: "Go (scip-go)" },
  { value: "typescript", label: "TypeScript / JavaScript (scip-typescript)" },
  { value: "clang", label: "C / C++ (scip-clang)" },
  { value: "python", label: "Python (scip-python)" },
];

/** Two questions a person keeps apart: whether the Git host lets the indexer fetch the source
 *  (public: no token; private: a fine-grained token with read access to Contents), and
 *  who may READ the index in Loom (open / restricted). The token is asked for only after
 *  the remote refuses — the server probes it before writing anything
 *  (`REMOTE_REFUSED`) — or when the person says up front that the repository is private.
 *  Exported for its test. */
export function AddRepositoryDialog({
  open,
  org,
  onOpenChange,
  onAdded,
  onSandboxRefused,
}: {
  open: boolean;
  org: string;
  onOpenChange: (v: boolean) => void;
  onAdded: () => void;
  /** The server refused for want of the repository-job sandbox (`SANDBOX_UNAVAILABLE`). */
  onSandboxRefused?: () => void;
}) {
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [rule, setRule] = useState<"open" | "restricted">("open");
  const [tokenNeeded, setTokenNeeded] = useState<"no" | "asked" | "refused">("no");
  const [token, setToken] = useState("");
  // Branches are chosen from what the remote shows, not typed.
  const [remote, setRemote] = useState<CodeRepoRemoteBranches | null>(null);
  const [looking, setLooking] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [prefixes, setPrefixes] = useState("");
  const [exclude, setExclude] = useState("");
  // Indexing knobs: skip generated files, and the compiler-tier producer
  const [skipGenerated, setSkipGenerated] = useState(true);
  const [producer, setProducer] = useState<Producer>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reset = () => {
    setRepo("");
    setBranch("");
    setRule("open");
    setTokenNeeded("no");
    setToken("");
    setRemote(null);
    setLooking(false);
    setPicked(new Set());
    setSearch("");
    setAdvanced(false);
    setPrefixes("");
    setExclude("");
    setSkipGenerated(true);
    setProducer("");
    setError(null);
  };
  const cleanRepo = repo.trim();
  const repoValid =
    /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(cleanRepo) ||
    /^(https|ssh):\/\/[^\s/]+\/.+/.test(cleanRepo) ||
    /^[A-Za-z0-9_.-]+@[^\s:]+:.+/.test(cleanRepo);
  const showToken = tokenNeeded !== "no";
  const indexedSet = new Set((remote?.indexed ?? []).map((x) => x.branch));
  const chosen = remote ? [...picked].filter((b) => !indexedSet.has(b)) : [];
  const primary = remote
    ? chosen.includes(remote.default_branch)
      ? remote.default_branch
      : (chosen[0] ?? "")
    : branch.trim();
  const overCap = remote !== null && chosen.length > Math.max(0, remote.cap);
  const valid = repoValid && (!showToken || token.trim().length > 0) && (remote ? chosen.length > 0 && !overCap : true);
  /** Ask the remote which branches exist (the first 60 matching `q`; default and
   *  indexed always ride); a refusal becomes the token step. A search on a
   *  truncated list asks the server again with the query. */
  const lookup = async (q = "") => {
    if (!repoValid || looking) return;
    setLooking(true);
    setError(null);
    try {
      const r = await remoteBranches(org, cleanRepo, token.trim(), q);
      setRemote(r);
      if (!q) {
        const taken = new Set(r.indexed.map((x) => x.branch));
        setPicked(new Set(r.default_branch && !taken.has(r.default_branch) ? [r.default_branch] : []));
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === "REMOTE_REFUSED" && !token.trim()) setTokenNeeded("refused");
      else setError(humanizeError(err));
      if (sandboxRefused(err)) onSandboxRefused?.();
    } finally {
      setLooking(false);
    }
  };
  // The chips shown: what the server returned (bounded), filtered by the
  // search locally; a picked branch stays visible even when it falls outside
  // the current page, so a choice is never hidden from the person making it.
  const q = search.trim().toLowerCase();
  const shownBranches = (() => {
    if (!remote) return [] as { name: string; sha: string }[];
    const filtered = remote.branches.filter((b) => !q || b.name.toLowerCase().includes(q));
    const names = new Set(filtered.map((b) => b.name));
    const kept = [...picked]
      .filter((n) => !names.has(n))
      .map((n) => remote.branches.find((b) => b.name === n) ?? { name: n, sha: "" });
    return [...filtered, ...kept];
  })();
  const visibleSuggested = remote
    ? shownBranches.map((b) => b.name).filter((n) => remote.suggested.includes(n) && !picked.has(n))
    : [];
  const toggle = (b: string) =>
    setPicked((prev) => {
      const n = new Set(prev);
      if (n.has(b)) n.delete(b);
      else n.add(b);
      return n;
    });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const others = remote ? chosen.filter((b) => b !== primary) : [];
      const out = await addCodeRepo(org, {
        repo: cleanRepo,
        branch: primary,
        join_policy: rule === "open" ? "open" : "approval",
        classification: rule === "restricted" ? "restricted" : "standard",
        access_token: token.trim(),
        path_prefixes: prefixes
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean),
        exclude: exclude
          .split(",")
          .map((x) => x.trim())
          .filter(Boolean),
        exclude_generated: skipGenerated,
        scip_producer: producer,
        branches: others,
      });
      const n = 1 + (out.also?.length ?? 0);
      const skipped = out.skipped?.length
        ? `; ${out.skipped.length} branch${out.skipped.length === 1 ? "" : "es"} refused: ${out.skipped.map((x) => x.branch).join(", ")}`
        : "";
      toast(
        `Added ${out.repo} — ${n} branch${n === 1 ? "" : "es"} indexing within two minutes; you maintain them${skipped}`,
        "ok",
      );
      onOpenChange(false);
      reset();
      onAdded();
    } catch (err) {
      // The remote would not show the repository to an anonymous fetch: ask for
      // the token now, in place, instead of an error a person must decode.
      if (err instanceof ApiError && err.code === "REMOTE_REFUSED" && !token.trim()) {
        setTokenNeeded("refused");
      } else {
        setError(humanizeError(err));
      }
      if (sandboxRefused(err)) onSandboxRefused?.();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) reset();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40 z-40" />
        <Dialog.Content className="fixed z-50 top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(92vw,540px)] max-h-[90vh] overflow-y-auto rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6 shadow-xl outline-none">
          <Dialog.Title className="text-[17px] font-semibold tracking-tight flex items-center gap-2">
            <GitBranch size={16} /> Add a repository
          </Dialog.Title>
          <Dialog.Description className="text-[13px] text-[var(--color-text-muted)] mt-1.5">
            One branch of a Git repository becomes a read-only index in <Mono>{org}</Mono>. You become its maintainer.
            Two separate questions below: whether the Git host lets Loom fetch the source, and who may read the index.
          </Dialog.Description>
          <form onSubmit={submit} className="mt-4 space-y-5">
            <fieldset className="space-y-3">
              <legend className={LABEL}>Fetching from Git</legend>
              <div className="grid grid-cols-[1fr_auto] gap-3 items-end">
                <div>
                  <label className={LABEL} htmlFor="add-repo">
                    Repository
                  </label>
                  <input
                    id="add-repo"
                    value={repo}
                    onChange={(e) => {
                      setRepo(e.target.value);
                      setRemote(null);
                      setPicked(new Set());
                    }}
                    onBlur={() => {
                      if (repoValid && !remote) void lookup();
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void lookup();
                      }
                    }}
                    placeholder="Acme/widgets"
                    autoFocus
                    className={`${FIELD} font-mono`}
                  />
                  <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                    owner/name, or paste an HTTPS/SSH Git URL
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void lookup()}
                  disabled={!repoValid || looking}
                  className="inline-flex items-center gap-1.5 px-3 py-2 text-[13px] font-medium rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] hover:bg-[var(--color-surface-2)] disabled:opacity-50 whitespace-nowrap"
                >
                  {looking ? <RefreshCw size={13} className="animate-spin" /> : <GitBranch size={13} />}{" "}
                  {looking ? "Asking remote…" : remote ? "Refresh branches" : "Find branches"}
                </button>
              </div>
              {remote ? (
                <fieldset>
                  <legend className={LABEL}>Branches to index</legend>
                  {remote.warnings.map((warning) => (
                    <div key={warning} className="mb-2 text-[12px] text-[var(--color-warn)]">
                      {warning}
                    </div>
                  ))}
                  <div className="text-[11px] text-[var(--color-text-subtle)] mb-1.5">
                    Each branch is its own index sharing one mirror.{" "}
                    {remote.default_branch && (
                      <>
                        Default <Mono>{remote.default_branch}</Mono> is pre-selected;{" "}
                      </>
                    )}
                    {remote.suggested.length > 0 && <>release branches are marked, newest first. </>}
                    {remote.cap >= 0 && (
                      <>
                        You may add {remote.cap} more index{remote.cap === 1 ? "" : "es"} in <Mono>{org}</Mono>.
                      </>
                    )}
                  </div>
                  {(remote.total > remote.branches.length || remote.query) && (
                    <div className="flex items-center gap-2 mb-2">
                      <input
                        aria-label="Search branches"
                        value={search}
                        onChange={(e) => setSearch(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void lookup(search.trim());
                          }
                        }}
                        placeholder={`Search ${fmtInt(remote.total)} branches…`}
                        className={`${FIELD} font-mono flex-1`}
                      />
                      <span className="text-[11px] text-[var(--color-text-subtle)] whitespace-nowrap tabular">
                        showing {shownBranches.length} of {fmtInt(remote.total)}
                        {remote.truncated && q && !shownBranches.some((b) => b.name.toLowerCase().includes(q))
                          ? " — press Enter to search the remote's full list"
                          : ""}
                      </span>
                    </div>
                  )}
                  <ul className="flex flex-wrap gap-2 list-none p-0 m-0 max-h-56 overflow-y-auto" aria-label="branches">
                    {shownBranches.map((b) => {
                      const taken = indexedSet.has(b.name);
                      const on = picked.has(b.name);
                      const release = remote.suggested.includes(b.name);
                      return (
                        <li key={b.name}>
                          <label
                            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[12px] font-mono cursor-pointer select-none ${taken ? "opacity-60 cursor-default border-[var(--color-border)]" : on ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]" : "border-[var(--color-border)] hover:border-[var(--color-border-strong)]"}`}
                            title={
                              taken
                                ? `already indexed here as ${remote.indexed.find((x) => x.branch === b.name)?.namespace}`
                                : release
                                  ? "release branch"
                                  : b.name === remote.default_branch
                                    ? "default branch"
                                    : undefined
                            }
                          >
                            <input
                              type="checkbox"
                              checked={taken || on}
                              disabled={taken}
                              onChange={() => toggle(b.name)}
                              className="sr-only"
                            />
                            {taken ? <Check size={11} /> : on ? <Check size={11} /> : <Plus size={11} />} {b.name}
                            {taken && (
                              <span className="font-sans text-[10px] uppercase tracking-wide opacity-70">indexed</span>
                            )}
                            {!taken && release && (
                              <span className="font-sans text-[10px] uppercase tracking-wide opacity-70">release</span>
                            )}
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                  {visibleSuggested.length > 0 &&
                    (() => {
                      const room = Math.max(0, remote.cap - chosen.length);
                      const take = visibleSuggested.slice(0, room);
                      return take.length > 0 ? (
                        <button
                          type="button"
                          onClick={() => setPicked((prev) => new Set([...prev, ...take]))}
                          className="mt-2 text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline"
                        >
                          Select the {take.length} newest release branch{take.length === 1 ? "" : "es"} shown
                          {take.length < visibleSuggested.length
                            ? ` (${visibleSuggested.length - take.length} more would exceed your ${remote.cap})`
                            : ""}
                        </button>
                      ) : null;
                    })()}
                  {overCap && (
                    <div role="alert" className="mt-2 text-[12px] text-[var(--color-danger)]">
                      {chosen.length} chosen, but only {Math.max(0, remote.cap)} more index
                      {remote.cap === 1 ? "" : "es"} may be added in this org.
                    </div>
                  )}
                </fieldset>
              ) : (
                <div>
                  <label className={LABEL} htmlFor="add-branch">
                    Branch
                  </label>
                  <input
                    id="add-branch"
                    value={branch}
                    onChange={(e) => setBranch(e.target.value)}
                    className={`${FIELD} font-mono w-48`}
                  />
                  <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                    Or press Find branches to choose from what the remote shows.
                  </div>
                </div>
              )}
              {showToken ? (
                <div role={tokenNeeded === "refused" ? "alert" : undefined}>
                  <label className={LABEL} htmlFor="add-token">
                    Access token
                  </label>
                  {tokenNeeded === "refused" && (
                    <div className="text-[12px] text-[var(--color-text)] mb-1.5">
                      The remote will not show <Mono>{cleanRepo}</Mono> without a token.
                    </div>
                  )}
                  <input
                    id="add-token"
                    type="password"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder="host access token"
                    autoComplete="off"
                    autoFocus={tokenNeeded === "refused"}
                    className={`${FIELD} font-mono`}
                  />
                  <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                    A fine-grained token with read access to Contents on this repository is enough. Stored encrypted,
                    used only to fetch, never shown again.
                  </div>
                </div>
              ) : (
                <div className="text-[11px] text-[var(--color-text-subtle)]">
                  A public repository needs no token; Loom asks the remote first and only asks you for one if it
                  refuses.{" "}
                  <button
                    type="button"
                    onClick={() => setTokenNeeded("asked")}
                    className="underline hover:text-[var(--color-text)]"
                  >
                    It is private — add a token now
                  </button>
                </div>
              )}
            </fieldset>

            <fieldset>
              <legend className={LABEL}>Who can read the index in Loom</legend>
              <div className="grid sm:grid-cols-2 gap-2">
                {(
                  [
                    ["open", "Open", Globe, "Anyone in the org joins as a reader at once."],
                    [
                      "restricted",
                      "Restricted",
                      Lock,
                      "You approve each reader. Access is per person; the source's own visibility does not change this.",
                    ],
                  ] as const
                ).map(([v, label, Icon, desc]) => (
                  <label
                    key={v}
                    className={`flex items-start gap-2.5 p-3 rounded-lg border cursor-pointer ${rule === v ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]" : "border-[var(--color-border)] hover:bg-[var(--color-surface-2)]"}`}
                  >
                    <input
                      type="radio"
                      name="add-rule"
                      value={v}
                      checked={rule === v}
                      onChange={() => setRule(v)}
                      className="mt-1"
                    />
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5 text-[13px] font-medium">
                        <Icon size={12} /> {label}
                      </span>
                      <span className="block text-[12px] text-[var(--color-text-muted)]">{desc}</span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              className="text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] inline-flex items-center gap-1"
            >
              {advanced ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Advanced
            </button>
            {advanced && (
              <div className="grid sm:grid-cols-2 gap-3">
                <div>
                  <label className={LABEL} htmlFor="add-prefixes">
                    Only these paths
                  </label>
                  <input
                    id="add-prefixes"
                    value={prefixes}
                    onChange={(e) => setPrefixes(e.target.value)}
                    placeholder="src/, include/"
                    className={`${FIELD} font-mono`}
                  />
                  <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                    Comma-separated prefixes. Empty = the whole tree.
                  </div>
                </div>
                <div>
                  <label className={LABEL} htmlFor="add-exclude">
                    Skip these paths
                  </label>
                  <input
                    id="add-exclude"
                    value={exclude}
                    onChange={(e) => setExclude(e.target.value)}
                    placeholder="**/generated/**, third_party/**"
                    className={`${FIELD} font-mono`}
                  />
                  <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                    Globs, comma-separated. Vendored and generated code usually.
                  </div>
                </div>
                <div>
                  <label className="flex items-start gap-2 text-[13px] text-[var(--color-text)] cursor-pointer">
                    <input
                      id="add-skip-generated"
                      type="checkbox"
                      checked={skipGenerated}
                      onChange={(e) => setSkipGenerated(e.target.checked)}
                      className="mt-1"
                    />
                    <span>
                      Skip generated files
                      <span className="block text-[11px] text-[var(--color-text-subtle)]">
                        Files that say so in their header (
                        <span className="font-mono">Code generated … DO NOT EDIT</span>,{" "}
                        <span className="font-mono">@generated</span>) and the repository's own{" "}
                        <span className="font-mono">.gitattributes</span> rules. Their sources stay indexed.
                      </span>
                    </span>
                  </label>
                </div>
                <div>
                  <label className={LABEL} htmlFor="add-producer">
                    Compiler tier
                  </label>
                  <select
                    id="add-producer"
                    value={producer}
                    onChange={(e) => {
                      const v = oneOf(
                        PRODUCERS.map((o) => o.value),
                        e.target.value,
                      );
                      if (v !== null) setProducer(v);
                    }}
                    className={FIELD}
                  >
                    {PRODUCERS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                  <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                    Exact cross-file calls on top of the parser tier; detected from go.mod, tsconfig, CMakeLists or
                    pyproject when left to the tree.
                  </div>
                </div>
              </div>
            )}

            {error && (
              <div
                role="alert"
                className="p-3 rounded-lg border border-[var(--color-danger)]/40 bg-[var(--color-danger)]/10 text-[12px] text-[var(--color-text)]"
              >
                {error}
              </div>
            )}

            <div className="flex items-center justify-between gap-2 pt-1">
              <span className="text-[11px] text-[var(--color-text-subtle)]">
                The remote is asked first; nothing is written if it refuses.
              </span>
              <div className="flex items-center gap-2">
                <Dialog.Close asChild>
                  <button
                    type="button"
                    className="px-3 py-1.5 text-[13px] rounded-md text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]"
                  >
                    Cancel
                  </button>
                </Dialog.Close>
                <button
                  type="submit"
                  disabled={!valid || busy}
                  className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[13px] font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50"
                >
                  {busy ? <RefreshCw size={13} className="animate-spin" /> : <Plus size={13} />}{" "}
                  {busy
                    ? "Asking remote…"
                    : tokenNeeded === "refused"
                      ? "Add with token"
                      : remote && chosen.length > 1
                        ? `Add ${chosen.length} branches`
                        : "Add and start indexing"}
                </button>
              </div>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function AddBranchDialog({
  group,
  onOpenChange,
  onAdded,
  onSandboxRefused,
}: {
  group: RepoGroup | null;
  onOpenChange: (v: boolean) => void;
  onAdded: () => void;
  onSandboxRefused?: () => void;
}) {
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (group) {
      setBranch("");
      setError(null);
    }
  }, [group]);
  const src = group?.branches.find((b) => b.my_role === "admin") ?? group?.primary ?? null;
  const taken = new Set((group?.branches ?? []).map((b) => b.branch));
  const valid = !!src && branch.trim().length > 0 && !taken.has(branch.trim());
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!src || !valid) return;
    setBusy(true);
    setError(null);
    try {
      const out = await addCodeBranch(src.org, src.namespace, branch.trim());
      toast(`Added ${out.repo} @ ${out.branch} — it shares the mirror, indexing starts within two minutes`, "ok");
      onOpenChange(false);
      onAdded();
    } catch (err) {
      setError(humanizeError(err));
      if (sandboxRefused(err)) onSandboxRefused?.();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog.Root open={group !== null} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40 z-40" />
        <Dialog.Content className="fixed z-50 top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[min(92vw,440px)] rounded-2xl border border-[var(--color-border)] bg-[var(--color-surface)] p-6 shadow-xl outline-none">
          {group && src && (
            <form onSubmit={submit}>
              <Dialog.Title className="text-[17px] font-semibold tracking-tight flex items-center gap-2">
                <GitBranch size={16} /> Add a branch of {group.repo}
              </Dialog.Title>
              <Dialog.Description className="text-[13px] text-[var(--color-text-muted)] mt-1.5">
                Same repository, another branch, its own index. It inherits the join rule (
                <b>{group.restricted ? "restricted" : "open"}</b>), the paths and the token; you maintain it.
              </Dialog.Description>
              <div className="mt-4">
                <label className={LABEL} htmlFor="add-branch-name">
                  Branch
                </label>
                <input
                  id="add-branch-name"
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                  placeholder="26.9"
                  autoFocus
                  className={`${FIELD} font-mono`}
                />
                <div className="text-[11px] text-[var(--color-text-subtle)] mt-1">
                  Already indexed: {group.branches.map((b) => b.branch).join(", ")}
                </div>
              </div>
              {error && (
                <div className="mt-3 p-3 rounded-lg border border-[var(--color-danger)]/40 bg-[var(--color-danger)]/10 text-[12px]">
                  {error}
                </div>
              )}
              <div className="mt-5 flex items-center justify-end gap-2">
                <Dialog.Close asChild>
                  <button
                    type="button"
                    className="px-3 py-1.5 text-[13px] rounded-md text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]"
                  >
                    Cancel
                  </button>
                </Dialog.Close>
                <button
                  type="submit"
                  disabled={!valid || busy}
                  className="inline-flex items-center gap-1.5 px-3.5 py-2 text-[13px] font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50"
                >
                  {busy ? <RefreshCw size={13} className="animate-spin" /> : <Plus size={13} />}{" "}
                  {busy ? "Asking remote…" : "Add branch"}
                </button>
              </div>
            </form>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---------------------------------------------------------------------------
// Repo detail
// ---------------------------------------------------------------------------

function RepoDetail({
  repo,
  siblings,
  onBack,
  onSwitch,
  onJoined,
}: {
  repo: CodeRepoSummary;
  siblings: CodeRepoSummary[];
  onBack: () => void;
  onSwitch: (ns: string) => void;
  onJoined: () => void;
}) {
  const id: CodeId = { org: repo.org, namespace: repo.namespace };
  const f = freshness(repo);
  const actions = useJoinActions(onJoined);
  const [joinTarget, setJoinTarget] = useState<{ repo: CodeRepoSummary; role: RepoRole } | null>(null);
  const others = siblings.filter((s) => s.namespace !== repo.namespace).sort(branchOrder);
  return (
    <PageContainer>
      <button
        onClick={onBack}
        className="inline-flex items-center gap-1 text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] mb-3"
      >
        <ChevronLeft size={14} /> Repositories
      </button>
      <div className="mb-5">
        <h1 className="text-[30px] font-semibold tracking-tight inline-flex items-center gap-3 flex-wrap">
          <GitBranch size={22} /> {repo.repo}
          <Mono className="text-[14px] px-2 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
            {repo.branch}
          </Mono>
          <Pill tone={f.tone} title={f.detail}>
            {f.label}
          </Pill>
          <span className="text-[12px] font-normal text-[var(--color-text-muted)]">read-only</span>
        </h1>
        <p className="text-[13px] text-[var(--color-text-muted)] mt-1">
          namespace <Mono>{repo.namespace}</Mono> · answers are as of <Mono>{short(repo.git_head) || "—"}</Mono>
        </p>
        {others.length > 0 && (
          <div className="mt-3 flex items-center gap-2 flex-wrap" aria-label="Other branches of this repository">
            <span className="text-[11px] uppercase tracking-wide text-[var(--color-text-subtle)]">Branches</span>
            {others.map((b) => (
              <BranchChip
                key={b.namespace}
                b={b}
                busy={actions.busy === b.namespace}
                onOpen={() => onSwitch(b.namespace)}
                onJoin={() => setJoinTarget({ repo: b, role: "viewer" })}
                onRequest={() => setJoinTarget({ repo: b, role: "viewer" })}
                onCancel={() => void actions.cancel(b)}
              />
            ))}
          </div>
        )}
      </div>
      <JoinDialog
        target={joinTarget}
        onOpenChange={(v) => {
          if (!v) setJoinTarget(null);
        }}
        onConfirm={async (r, role) => {
          setJoinTarget(null);
          await actions.join(r, role);
        }}
      />

      <Tabs.Root defaultValue="health">
        <Tabs.List className="flex border-b border-[var(--color-border)] mb-5">
          <TabTrigger value="health" icon={Activity}>
            Health
          </TabTrigger>
          <TabTrigger value="explore" icon={Search}>
            Explore
          </TabTrigger>
          <TabTrigger value="packs" icon={Package}>
            PR packs
          </TabTrigger>
          <TabTrigger value="access" icon={ShieldCheck}>
            Access
          </TabTrigger>
        </Tabs.List>
        <Tabs.Content value="health" className="outline-none">
          <HealthTab id={id} />
        </Tabs.Content>
        <Tabs.Content value="explore" className="outline-none">
          <ExploreTab id={id} />
        </Tabs.Content>
        <Tabs.Content value="packs" className="outline-none">
          <PacksTab id={id} repo={repo.repo} />
        </Tabs.Content>
        <Tabs.Content value="access" className="outline-none">
          <AccessTab
            id={id}
            siblings={siblings}
            repo={repo}
            onRetired={() => {
              onJoined();
              onBack();
            }}
          />
        </Tabs.Content>
      </Tabs.Root>
    </PageContainer>
  );
}

function TabTrigger({ value, icon: Icon, children }: { value: string; icon: LucideIcon; children: React.ReactNode }) {
  return (
    <Tabs.Trigger
      value={value}
      className="inline-flex items-center gap-2 px-4 py-2.5 text-[14px] font-medium text-[var(--color-text-muted)] hover:text-[var(--color-text)] data-[state=active]:text-[var(--color-text)] data-[state=active]:border-b-2 data-[state=active]:border-[var(--color-accent)] -mb-px outline-none transition-colors"
    >
      <Icon size={14} /> {children}
    </Tabs.Trigger>
  );
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

// One icon per compiler-tier producer on the Health tab.
const TIER_ICONS: Record<TierIcon, LucideIcon> = {
  cpp: Network,
  typescript: Braces,
  go: Package,
  python: Braces,
  none: Network,
};

function lagText(lag: number, watermark: string): { text: string; tone: Fresh["tone"] } {
  if (!watermark) return { text: "unset", tone: "muted" };
  if (lag < 0) return { text: "lag unknown", tone: "muted" };
  if (lag === 0) return { text: "at tip", tone: "ok" };
  return { text: `${lag} behind`, tone: lag > 20 ? "bad" : "warn" };
}

// PR history's watermark is a merge time, not a commit, so no commit lag
// exists: its pill reads the worker's own cadence — current while a pass ran
// within two intervals, stale after that.
export function passFreshness(
  w: CodeWorkerStatus | undefined,
  now = Date.now(),
): { text: string; tone: Fresh["tone"] } {
  if (!w) return { text: "not on this replica", tone: "muted" };
  if (!w.enabled) return { text: "disabled", tone: "muted" };
  if (w.consecutive_failures > 0) return { text: "failing", tone: "bad" };
  if (!w.last_run_at) return { text: "pending", tone: "muted" };
  const age = (now - Date.parse(w.last_run_at)) / 1000;
  return age <= 2 * w.interval_seconds + w.last_duration_ms / 1000
    ? { text: "current", tone: "ok" }
    : { text: "stale", tone: "warn" };
}

// A commit watermark shows as a short sha, a time watermark to the minute.
export const watermarkText = (w: string) => (/^[0-9a-f]{7,40}$/.test(w) ? short(w) : w.slice(0, 16).replace("T", " "));

function StageCard({
  icon: Icon,
  title,
  watermark,
  lag,
  freshness,
  updatedAt,
  workers,
  note,
  tone,
}: {
  icon: LucideIcon;
  title: string;
  watermark: string;
  lag: number;
  freshness?: { text: string; tone: Fresh["tone"] };
  updatedAt?: string;
  workers: Array<[string, CodeIndexStatus["workers"][string] | undefined]>;
  note?: string;
  tone?: Fresh["tone"];
}) {
  const l = freshness ?? lagText(lag, watermark);
  return (
    <div className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="flex items-center justify-between gap-2">
        <div className="inline-flex items-center gap-2 text-[13px] font-medium">
          <Icon size={14} /> {title}
        </div>
        <Pill tone={l.tone}>{l.text}</Pill>
      </div>
      <div className="mt-2 text-[12px] text-[var(--color-text-muted)]">
        watermark <Mono className="text-[var(--color-text)]">{watermarkText(watermark) || "—"}</Mono>
        {updatedAt && <> · advanced {relativeTime(updatedAt.replace(" ", "T") + "Z")}</>}
      </div>
      {note && (
        <div
          className={`mt-1 text-[11px] ${tone === "warn" ? "text-[var(--color-warn)]" : "text-[var(--color-text-subtle)]"}`}
        >
          {note}
        </div>
      )}
      {workers.length > 0 && (
        <div className="mt-3 space-y-1">
          {workers.map(([name, w]) => (
            <div key={name} className="flex items-center justify-between text-[11px] tabular">
              <Mono className="text-[11px] text-[var(--color-text-muted)]">{name}</Mono>
              {!w ? (
                <span className="text-[var(--color-text-subtle)]">not on this replica</span>
              ) : !w.enabled ? (
                <span className="text-[var(--color-text-subtle)]">disabled on this replica</span>
              ) : w.consecutive_failures > 0 ? (
                <span className="text-[var(--color-danger)]" title={w.last_error}>
                  {w.consecutive_failures}× failing · {w.last_error.slice(0, 60)}
                </span>
              ) : (
                <span className="text-[var(--color-text-muted)]">
                  every {w.interval_seconds}s · last {w.last_run_at ? relativeTime(w.last_run_at) : "never"} (
                  {w.last_duration_ms}ms)
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function HealthTab({ id }: { id: CodeId }) {
  const [st, setSt] = useState<CodeIndexStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showState, setShowState] = useState(false);
  const [cov, setCov] = useState<CodeCoverage | null>(null);
  const [recent, setRecent] = useState<CodeRecentCommitRow[] | null>(null);
  const [errs, setErrs] = useState<{ rows: CodeIndexErrorRow[]; total: number } | null>(null);
  const [showErrs, setShowErrs] = useState(false);
  const [spend, setSpend] = useState<{ usd: number; ops: number } | null | undefined>(undefined);

  const load = useCallback(
    (signal?: AbortSignal, live: () => boolean = () => true) => {
      sharedIndexStatus(id)
        .then((s) => {
          if (live()) {
            setSt(s);
            setError(null);
          }
        })
        .catch((e) => {
          if (live()) setError(humanizeError(e));
        });
      codeCoverage(id, { signal })
        .then((c) => {
          if (live()) setCov(c);
        })
        .catch(() => {
          if (live()) setCov(null);
        });
      codeRecentCommits(id, 15, { signal })
        .then((r) => {
          if (live()) setRecent(r.rows);
        })
        .catch(() => {
          if (live()) setRecent([]);
        });
      codeIndexErrors(id, 100, { signal })
        .then((e) => {
          if (live()) setErrs(e);
        })
        .catch(() => {
          if (live()) setErrs(null);
        });
      listCostRollup(id, { days: 7, groupBy: ["operation"], signal })
        .then((r) => {
          if (!live()) return;
          const rows: CostRow[] = r.rows || [];
          setSpend({
            usd: rows.reduce((a, x) => a + (x.cost_usd || 0), 0),
            ops: rows.reduce((a, x) => a + (x.ops || 0), 0),
          });
        })
        .catch(() => {
          if (live()) setSpend(null);
        });
    },
    // `id` is a fresh object each parent render; its two fields are the identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [id.org, id.namespace],
  );

  useAbortableEffect((signal, live) => load(signal, live), [load]);
  useVisiblePoll(load, 60_000);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      const r = await codeRefresh(id);
      toast(r.already_running ? "An ingest pass is already running" : "Ingest pass triggered", "ok");
      setTimeout(load, 2500);
    } catch (e) {
      toast(`Refresh failed: ${humanizeError(e)}`, "error");
    } finally {
      setRefreshing(false);
    }
  };

  if (error) return <EmptyState icon={Activity} title="Couldn't load index status" description={error} />;
  if (!st)
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );

  const W = st.workers;
  const gitHeadAt = st.state.git_head?.updated_at;

  return (
    <div className="space-y-6">
      <div className="text-xs text-[var(--color-text-muted)]">
        {st.provider || "git"} · {st.profile || "generic"} profile
        {st.compiler_candidates?.length ? ` · compiler candidates: ${st.compiler_candidates.join(", ")}` : ""}
      </div>
      {!!st.quality_warnings?.length && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm space-y-1">
          {st.quality_warnings.map((warning) => (
            <div key={warning}>{warning}</div>
          ))}
        </div>
      )}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="Files" value={fmtInt(st.counts.code_files)} hint={st.path_prefixes.join(" ")} />
        <Stat label="Symbols" value={fmtInt(st.counts.code_symbols)} hint={`${fmtInt(st.counts.code_edges)} edges`} />
        <Stat
          label="Chunks"
          value={fmtInt(st.counts.code_chunks)}
          hint={`${fmtInt(st.counts.code_summaries)} described`}
        />
        <Stat
          label="PR packs"
          value={fmtInt(st.counts.code_context_packs)}
          hint={`${fmtInt(st.counts.code_pr_history)} PRs in history`}
        />
        <Stat
          label="Spend · 7d"
          value={spend === undefined ? "…" : spend === null ? "—" : `$${spend.usd.toFixed(2)}`}
          hint={
            spend ? `${fmtInt(spend.ops)} metered ops (embeddings, describes, code.ask)` : "cost rollup unavailable"
          }
        />
      </div>

      <div>
        <div className="section-label mb-2">
          Pipeline · clone tip <Mono>{short(st.clone_head) || "unreadable"}</Mono>
        </div>
        <div className="grid md:grid-cols-2 2xl:grid-cols-4 gap-3">
          <StageCard
            icon={Cpu}
            title="Fast tier (tree-sitter + embeddings)"
            watermark={st.git_head}
            lag={st.fast_tier_lag}
            updatedAt={gitHeadAt}
            workers={[
              ["code_ingest", W.code_ingest],
              ["code_embed_recovery", W.code_embed_recovery],
              ["code_summary", W.code_summary],
            ]}
            note="Per-commit re-index of touched files; builds a context pack per merged PR."
          />
          {/* One card per compiler tier the repository runs
              (its own producer first), read from the status row — a TypeScript
              repository shows scip-typescript, not a C++ card by name. */}
          {tierCards(st).map((c) => (
            <StageCard
              key={c.key}
              icon={TIER_ICONS[c.icon]}
              title={c.title}
              watermark={c.watermark}
              lag={c.lag}
              updatedAt={c.updatedAt}
              workers={c.workers.map((name) => [name, W[name]] as [string, CodeWorkerStatus | undefined])}
              note={c.note}
            />
          ))}
          <StageCard
            icon={GitMerge}
            title={st.provider === "github" ? "PR history (GitHub)" : "PR history (GitHub only)"}
            watermark={st.pr_cursor}
            lag={-1}
            freshness={passFreshness(W.code_pr_history)}
            updatedAt={st.state.pr_cursor?.updated_at}
            workers={[
              ["code_pr_history", W.code_pr_history],
              ["code_graph_stats", W.code_graph_stats],
            ]}
            note={
              cov
                ? cov.pr_history_enabled
                  ? `PRs ${cov.pr_history_from.slice(0, 10)} → ${cov.pr_history_to.slice(0, 10)} · last fetch ${cov.pr_history_last_fetch_at ? relativeTime(cov.pr_history_last_fetch_at.replace(" ", "T") + "Z") : "pending"}${cov.pr_history_auth === "anonymous" ? " · anonymous GitHub API (60 req/h; set LOOM_CODE_GITHUB_TOKEN for headroom)" : ""}`
                  : `PRs ${cov.pr_history_from.slice(0, 10)} → ${cov.pr_history_to.slice(0, 10)}, then stopped: worker disabled — set LOOM_CODE_PR_HISTORY_ENABLED=true (a token is optional for public repos). New packs show no PR titles until then.`
                : st.state.pr_backfill_date
                  ? `backfilled to ${st.state.pr_backfill_date.value}`
                  : undefined
            }
            tone={cov && !cov.pr_history_enabled ? "warn" : undefined}
          />
        </div>
      </div>

      <div>
        <div className="section-label mb-2">Coverage by language · when each part was last written</div>
        {!cov ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-[var(--color-border)]">
            <table className="w-full text-[12px] tabular">
              <thead className="text-left text-[var(--color-text-muted)] bg-[var(--color-surface-2)]">
                <tr>
                  <th className="px-3 py-2">language</th>
                  <th className="px-3 py-2">files</th>
                  <th className="px-3 py-2">last file indexed</th>
                  <th className="px-3 py-2">chunks · embedded · described</th>
                  <th className="px-3 py-2">syntax symbols (tree-sitter)</th>
                  <th className="px-3 py-2">compiler symbols (scip)</th>
                </tr>
              </thead>
              <tbody>
                {cov.languages
                  .filter((l) => l.files > 0 || l.chunks > 0 || l.symbols_scip > 0)
                  .map((l) => {
                    const embPct = l.chunks ? Math.round((l.chunks_embedded / l.chunks) * 1000) / 10 : 0;
                    const ago = (t: string) => (t ? relativeTime(t.replace(" ", "T") + "Z") : "—");
                    return (
                      <tr key={l.lang || "(other)"} className="border-t border-[var(--color-border)]">
                        <td className="px-3 py-1.5 font-mono">
                          {l.lang || <span className="text-[var(--color-text-subtle)]">(outside indexed paths)</span>}
                        </td>
                        <td className="px-3 py-1.5">
                          {fmtInt(l.files)}
                          {l.files_errored > 0 && (
                            <span className="ml-1 text-[var(--color-warn)]" title="files with index_error">
                              · {l.files_errored} err
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-1.5 text-[var(--color-text-muted)]" title={l.files_last_at}>
                          {ago(l.files_last_at)}
                        </td>
                        <td className="px-3 py-1.5">
                          {fmtInt(l.chunks)} ·{" "}
                          <span className={embPct < 99.5 && l.chunks > 0 ? "text-[var(--color-warn)]" : ""}>
                            {embPct}%
                          </span>{" "}
                          · {fmtInt(l.chunks_described)}
                        </td>
                        <td className="px-3 py-1.5">
                          {l.symbols_treesitter ? (
                            <>
                              {fmtInt(l.symbols_treesitter)}{" "}
                              <span className="text-[var(--color-text-subtle)]" title={l.symbols_treesitter_last_at}>
                                · {ago(l.symbols_treesitter_last_at)}
                              </span>
                            </>
                          ) : (
                            <span className="text-[var(--color-text-subtle)]">—</span>
                          )}
                        </td>
                        <td className="px-3 py-1.5">
                          {l.symbols_scip ? (
                            <>
                              {fmtInt(l.symbols_scip)}{" "}
                              <span className="text-[var(--color-text-subtle)]" title={l.symbols_scip_last_at}>
                                · {ago(l.symbols_scip_last_at)}
                              </span>
                            </>
                          ) : (
                            <span className="text-[var(--color-text-subtle)]">not compiler-indexed</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
            <div className="px-3 py-2 text-[11px] text-[var(--color-text-subtle)] border-t border-[var(--color-border)]">
              Symbols agents can trust for exact callers/callees are the compiler (scip) column; languages with only
              tree-sitter symbols answer search and definitions but their call graph is heuristic.
              {cov.tiers.map((t) => (
                <span key={t.tier} className="ml-3">
                  {t.tier} edges {fmtInt(t.edges)} ·{" "}
                  {t.edges_last_at ? relativeTime(t.edges_last_at.replace(" ", "T") + "Z") : "—"}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        <div className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
          <div className="inline-flex items-center gap-2 text-[13px] font-medium mb-2">
            <History size={14} /> Recently ingested commits
          </div>
          {recent === null ? (
            <Skeleton className="h-20 w-full" />
          ) : recent.length === 0 ? (
            <div className="text-[12px] text-[var(--color-text-subtle)]">No commits recorded yet.</div>
          ) : (
            <div className="space-y-1.5">
              {recent.map((c) => (
                <div key={c.commit} className="flex items-start gap-2 text-[12px]">
                  <Mono className="text-[var(--color-text-muted)] shrink-0">{short(c.commit).slice(0, 8)}</Mono>
                  <span className="truncate flex-1" title={c.subject}>
                    {c.subject || <span className="text-[var(--color-text-subtle)]">(subject unavailable)</span>}
                  </span>
                  {c.pr_number > 0 && (
                    <span className="text-[11px] text-[var(--color-text-subtle)] tabular shrink-0">#{c.pr_number}</span>
                  )}
                  <span
                    className="text-[11px] text-[var(--color-text-subtle)] tabular shrink-0"
                    title={`${c.files} files · committed ${c.committed_at}`}
                  >
                    {c.files}f · {c.ingested_at ? relativeTime(c.ingested_at.replace(" ", "T") + "Z") : "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
          <button
            onClick={() => setShowErrs((v) => !v)}
            className="inline-flex items-center gap-2 text-[13px] font-medium w-full text-left"
          >
            <AlertTriangle size={14} className={errs && errs.total > 0 ? "text-[var(--color-warn)]" : ""} />
            Files with index errors{" "}
            {errs ? <span className="tabular text-[var(--color-text-muted)]">({fmtInt(errs.total)})</span> : null}
            <span className="ml-auto text-[var(--color-text-subtle)]">
              {showErrs ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          </button>
          {errs === null && <Skeleton className="h-6 w-full mt-2" />}
          {errs && errs.total === 0 && (
            <div className="mt-2 text-[12px] text-[var(--color-text-subtle)]">
              Every file under the indexed prefixes parsed and chunked.
            </div>
          )}
          {errs && errs.total > 0 && !showErrs && (
            <div className="mt-2 text-[12px] text-[var(--color-text-muted)]">
              Search still works for these files' neighbours; the files themselves have no chunks or symbols.
            </div>
          )}
          {errs && showErrs && errs.rows.length > 0 && (
            <div className="mt-2 max-h-64 overflow-auto space-y-1">
              {errs.rows.map((r) => (
                <div key={r.path} className="text-[11px] flex gap-2">
                  <Mono className="text-[11px] truncate flex-1" title={r.path}>
                    {r.path}
                  </Mono>
                  <span className="text-[var(--color-warn)] truncate max-w-[45%]" title={r.error}>
                    {r.error}
                  </span>
                </div>
              ))}
              {errs.total > errs.rows.length && (
                <div className="text-[11px] text-[var(--color-text-subtle)]">+{errs.total - errs.rows.length} more</div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] flex items-start justify-between gap-4">
        <div>
          <div className="inline-flex items-center gap-2 text-[13px] font-medium">
            <GitCommitHorizontal size={14} /> CI merge hook
          </div>
          <div className="mt-1 text-[12px] text-[var(--color-text-muted)]">
            {st.last_refresh_at ? (
              <>
                Last <Mono>code.refresh</Mono> received {relativeTime(st.last_refresh_at)}
                {refreshDetail(st.last_refresh_detail) && (
                  <>
                    {" "}
                    · <Mono>{refreshDetail(st.last_refresh_detail)}</Mono>
                  </>
                )}
                . The poller keeps the index fresh between requests.
              </>
            ) : (
              <span className="text-[var(--color-warn)]">
                Never received. The poller keeps the index fresh on its own; the hook only shortens latency after a
                merge.
              </span>
            )}
          </div>
        </div>
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50 shadow-sm whitespace-nowrap"
        >
          <RefreshCw size={13} className={refreshing ? "animate-spin motion-reduce:animate-none!" : ""} /> Refresh now
        </button>
      </div>

      <div>
        <button
          onClick={() => setShowState((v) => !v)}
          className="inline-flex items-center gap-1 text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
        >
          {showState ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Raw watermark table (
          {Object.keys(st.state).length} keys)
        </button>
        {showState && (
          <div className="mt-2 overflow-x-auto rounded-xl border border-[var(--color-border)]">
            <table className="w-full text-[12px]">
              <thead className="text-left text-[var(--color-text-muted)] bg-[var(--color-surface-2)]">
                <tr>
                  <th className="px-3 py-2">key</th>
                  <th className="px-3 py-2">value</th>
                  <th className="px-3 py-2">detail</th>
                  <th className="px-3 py-2">updated</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(st.state)
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([k, v]) => (
                    <tr key={k} className="border-t border-[var(--color-border)]">
                      <td className="px-3 py-1.5 font-mono">{k}</td>
                      <td className="px-3 py-1.5 font-mono break-all max-w-[48ch]">
                        <StateValue value={v.value} />
                      </td>
                      <td className="px-3 py-1.5 text-[var(--color-text-muted)]">{v.detail || ""}</td>
                      <td className="px-3 py-1.5 text-[var(--color-text-muted)] tabular whitespace-nowrap">
                        {v.updated_at}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/** A watermark value: a sha shortened, anything past `STATE_VALUE_CLAMP`
 *  characters (the jit_risk_fit JSON runs to kilobytes) clamped behind a toggle so one
 *  key cannot stretch the table. */
const STATE_VALUE_CLAMP = 160;

function StateValue({ value }: { value: string }) {
  const [full, setFull] = useState(false);
  if (!value) return <>—</>;
  if (value.length === 40) return <>{short(value)}</>;
  if (value.length <= STATE_VALUE_CLAMP || full) {
    return (
      <>
        {value}
        {value.length > STATE_VALUE_CLAMP && (
          <button
            type="button"
            onClick={() => setFull(false)}
            className="ml-2 text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline"
          >
            less
          </button>
        )}
      </>
    );
  }
  return (
    <>
      {value.slice(0, STATE_VALUE_CLAMP)}…
      <button
        type="button"
        onClick={() => setFull(true)}
        className="ml-2 text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline"
        aria-label={`show all ${value.length} characters`}
      >
        show all ({fmtInt(value.length)} chars)
      </button>
    </>
  );
}

// ---------------------------------------------------------------------------
// Explore
// ---------------------------------------------------------------------------

function UnitCard({ u, onOpen }: { u: CodeUnit; onOpen: (symbol: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {u.symbol ? (
              <button
                onClick={() => onOpen(u.symbol)}
                className="font-mono text-[13px] text-[var(--color-accent)] hover:underline text-left break-all"
              >
                {u.symbol}
              </button>
            ) : (
              <span className="font-mono text-[13px]">{u.path.split("/").pop()}</span>
            )}
            <span className="text-[11px] px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
              {u.kind || "unit"}
            </span>
            {u.tier && <span className="text-[11px] text-[var(--color-text-subtle)]">{u.tier}</span>}
          </div>
          <div className="mt-0.5 text-[11px] text-[var(--color-text-muted)] font-mono break-all">
            {u.path}
            {u.start_line ? `:${u.start_line}-${u.end_line}` : ""}
          </div>
          {u.description && <div className="mt-1.5 text-[12px] text-[var(--color-text-muted)]">{u.description}</div>}
        </div>
        <div className="text-right shrink-0 text-[11px] text-[var(--color-text-subtle)] tabular">
          {u.score > 0 && <div>score {u.score.toFixed(3)}</div>}
          {(u.churn > 0 || u.amp > 0) && (
            <div>
              churn {u.churn} · amp {u.amp}
            </div>
          )}
          {u.tested && <div className="text-[var(--color-success)]">tested</div>}
        </div>
      </div>
      {u.signature && (
        <pre className="mt-2 text-[11px] font-mono text-[var(--color-text-muted)] whitespace-pre-wrap break-all">
          {u.signature}
        </pre>
      )}
      {u.body && (
        <>
          <button
            onClick={() => setOpen((v) => !v)}
            className="mt-2 inline-flex items-center gap-1 text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
          >
            {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {open ? "hide" : "show"} body
            {u.elided ? " (elided)" : ""}
          </button>
          {open && (
            <pre className="mt-2 p-3 rounded-lg bg-[var(--color-bg)] border border-[var(--color-border)] text-[11px] font-mono overflow-x-auto max-h-96">
              {u.body}
            </pre>
          )}
        </>
      )}
    </div>
  );
}

function EdgeList({
  title,
  edges,
  side,
  onOpen,
}: {
  title: string;
  edges: CodeEdge[];
  side: "from" | "to";
  onOpen: (s: string) => void;
}) {
  const names = Array.from(new Set(edges.map((e) => (side === "from" ? e.from_symbol : e.to_symbol)).filter(Boolean)));
  return (
    <div>
      <div className="section-label mb-1.5">
        {title} <span className="tabular">({names.length})</span>
      </div>
      {names.length === 0 ? (
        <div className="text-[12px] text-[var(--color-text-subtle)]">none in the graph</div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {names.slice(0, 60).map((n) => (
            <button
              key={n}
              onClick={() => onOpen(n)}
              className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] hover:bg-[var(--color-surface-3)] break-all text-left"
            >
              {n}
            </button>
          ))}
          {names.length > 60 && (
            <span className="text-[11px] text-[var(--color-text-subtle)]">+{names.length - 60} more</span>
          )}
        </div>
      )}
    </div>
  );
}

function ExploreTab({ id }: { id: CodeId }) {
  const [q, setQ] = useState("");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Awaited<ReturnType<typeof codeSearch>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [map, setMap] = useState<string | null>(null);
  const [sym, setSym] = useState<{
    name: string;
    defs: CodeUnit[];
    callers: CodeEdge[];
    callees: CodeEdge[];
    ambiguous: boolean;
  } | null>(null);
  const [symBusy, setSymBusy] = useState(false);

  const run = async (query: string) => {
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    setSym(null);
    try {
      setRes(await codeSearch(id, query.trim(), { top_k: 10, path_prefix: prefix.trim() }));
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };

  const openSymbol = async (name: string) => {
    setSymBusy(true);
    try {
      const [s, callers, callees] = await Promise.all([
        codeSymbol(id, name),
        codeCallers(id, name).catch(() => ({ edges: [] as CodeEdge[], seed_symbols: [], truncated: false })),
        codeCallees(id, name).catch(() => ({ edges: [] as CodeEdge[], seed_symbols: [], truncated: false })),
      ]);
      setSym({ name, defs: s.definitions, callers: callers.edges, callees: callees.edges, ambiguous: s.ambiguous });
    } catch (e) {
      toast(`Symbol lookup failed: ${humanizeError(e)}`, "error");
    } finally {
      setSymBusy(false);
    }
  };

  const loadMap = async () => {
    try {
      setMap((await codeMap(id, { token_budget: 1500 })).map);
    } catch (e) {
      toast(`Map failed: ${humanizeError(e)}`, "error");
    }
  };

  return (
    <div className="space-y-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(q);
        }}
        className="flex gap-2"
      >
        <input
          aria-label="Search code"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search code: a question, a symbol name, or a file path…"
          className="flex-1 px-3 py-2 text-base rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)]"
        />
        <input
          aria-label="Path prefix"
          value={prefix}
          onChange={(e) => setPrefix(e.target.value)}
          placeholder="path prefix (optional)"
          className="w-56 px-3 py-2 text-sm rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] font-mono"
        />
        <button
          type="submit"
          disabled={busy || !q.trim()}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50 shadow-sm"
        >
          <Search size={13} /> Search
        </button>
      </form>
      <p className="text-[11px] text-[var(--color-text-subtle)]">
        Runs the same <span className="font-mono">code.search</span> / <span className="font-mono">code.symbol</span> /{" "}
        <span className="font-mono">code.callers</span> ops agents call. Click a symbol to see its definition and graph.
      </p>

      {error && <div className="text-[13px] text-[var(--color-danger)]">{error}</div>}

      {sym && (
        <div className="p-4 rounded-xl border border-[var(--color-accent)]/40 bg-[var(--color-surface)] space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div className="font-mono text-[14px] break-all">
              {sym.name}
              {sym.ambiguous && <span className="ml-2 text-[11px] text-[var(--color-warn)]">ambiguous</span>}
            </div>
            <button
              onClick={() => setSym(null)}
              className="text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            >
              close
            </button>
          </div>
          {sym.defs.length === 0 && (
            <div className="text-[12px] text-[var(--color-text-muted)]">No definition resolved.</div>
          )}
          <div className="space-y-2">
            {sym.defs.map((u) => (
              <UnitCard key={u.unit_id || u.path + u.start_line} u={u} onOpen={openSymbol} />
            ))}
          </div>
          <div className="grid md:grid-cols-2 gap-4">
            <EdgeList title="Callers" edges={sym.callers} side="from" onOpen={openSymbol} />
            <EdgeList title="Callees" edges={sym.callees} side="to" onOpen={openSymbol} />
          </div>
        </div>
      )}
      {symBusy && <Skeleton className="h-16 w-full" />}

      {busy && (
        <div className="space-y-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {res && !busy && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 text-[11px] text-[var(--color-text-muted)] tabular flex-wrap">
            <span>
              route <Mono>{res.route}</Mono>
            </span>
            <span>confidence {res.retrieval_confidence.toFixed(2)}</span>
            <Pill tone={res.retrieval_risk === "low" ? "ok" : res.retrieval_risk === "medium" ? "warn" : "bad"}>
              {res.retrieval_risk} risk
            </Pill>
            <span>{res.tokens_returned} tokens</span>
            {res.as_of?.commit && (
              <span title={`indexed ${res.as_of.indexed_at ?? ""}`}>
                as of <Mono>{short(res.as_of.commit)}</Mono>
              </span>
            )}
            {res.as_of && typeof res.as_of.lag_commits === "number" && (
              <Pill
                tone={res.as_of.stale ? "warn" : res.as_of.lag_commits === 0 ? "ok" : "muted"}
                title="as_of.stale is what an agent gates on before trusting a hit for HEAD-accurate work"
              >
                {res.as_of.lag_commits < 0
                  ? "lag unknown"
                  : res.as_of.stale
                    ? `stale · ${res.as_of.lag_commits} behind`
                    : res.as_of.lag_commits === 0
                      ? "at tip"
                      : `${res.as_of.lag_commits} behind`}
              </Pill>
            )}
          </div>
          {res.note && (
            <div
              className="text-[12px] text-[var(--color-warn)]"
              title="The vector leg covers less than the ready share of this repository's chunks; results come from the lexical leg alone until it does."
            >
              {res.note}
            </div>
          )}
          {res.abstain && res.abstain_code === "not_covered" && (
            <div className="p-3 rounded-lg border border-[var(--color-danger)]/30 bg-[var(--color-danger)]/10 text-[12px]">
              <b>Not covered.</b> {res.abstain_reason} An agent receives <Mono>abstain_code = not_covered</Mono> here,
              distinct from a genuine miss.
            </div>
          )}
          {res.abstain && res.abstain_code !== "not_covered" && (
            <div className="p-3 rounded-lg border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 text-[12px]">
              Abstained: {res.abstain_reason || "no confident match"}. Results below are best-effort.
            </div>
          )}
          {res.results.length === 0 && (
            <EmptyState
              icon={Search}
              title="No results"
              description="Try a symbol name, a path fragment, or a more specific question."
            />
          )}
          {res.results.map((u) => (
            <UnitCard key={u.unit_id || u.path + u.start_line} u={u} onOpen={openSymbol} />
          ))}
        </div>
      )}

      {!res && !busy && !sym && (
        <div className="p-4 rounded-xl border border-dashed border-[var(--color-border)]">
          <div className="flex items-center justify-between gap-3">
            <div className="text-[13px] text-[var(--color-text-muted)]">
              Not sure where to start? Load the repo orientation map agents get from{" "}
              <span className="font-mono">code.map</span>.
            </div>
            <button
              onClick={loadMap}
              className="px-2.5 py-1.5 text-[12px] text-[var(--color-text-muted)] bg-[var(--color-surface-2)] hover:bg-[var(--color-surface)] hover:text-[var(--color-text)] rounded-md whitespace-nowrap"
            >
              Show map
            </button>
          </div>
          {map && (
            <pre className="mt-3 text-[11px] font-mono whitespace-pre-wrap text-[var(--color-text-muted)] max-h-[32rem] overflow-auto">
              {map}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// PR packs
// ---------------------------------------------------------------------------

function PacksTab({ id, repo }: { id: CodeId; repo: string }) {
  const PAGE = 50;
  const [rows, setRows] = useState<CodeContextPackRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [pack, setPack] = useState<{ pr: number; text: string; tokens: number; source: string } | null>(null);
  const [prFilter, setPrFilter] = useState("");
  // Settle the typed PR number for 250 ms before asking, like every other search box on
  // the dashboard.
  const [prSettled, setPrSettled] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setPrSettled(prFilter), 250);
    return () => clearTimeout(t);
  }, [prFilter]);

  useAbortableEffect(
    (signal, live) => {
      setRows(null);
      const pr = parseInt(prSettled, 10);
      listCodeContextPacks(id, { limit: PAGE, offset, pr_number: Number.isFinite(pr) ? pr : 0, signal })
        .then((r) => {
          if (live()) {
            setRows(r.rows);
            setTotal(r.total);
            setError(null);
          }
        })
        .catch((e) => {
          if (live()) setError(humanizeError(e));
        });
    },
    [id.org, id.namespace, offset, prSettled],
  );

  const toggle = async (pr: number) => {
    if (open === pr) {
      setOpen(null);
      return;
    }
    setOpen(pr);
    setPack(null);
    try {
      const p = await codeContextPack(id, pr, 6000);
      setPack({ pr, text: p.context, tokens: p.tokens_used, source: p.source });
    } catch (e) {
      toast(`Pack load failed: ${humanizeError(e)}`, "error");
    }
  };

  if (error) return <EmptyState icon={Package} title="Couldn't load PR packs" description={error} />;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[12px] text-[var(--color-text-muted)] max-w-2xl">
          One precomputed briefing per merged PR, built by the fast tier as the merge lands. A review bot fetches it
          with <span className="font-mono">code.context_pack</span> at analysis start instead of exploring cold.
        </p>
        <input
          aria-label="Filter by PR number"
          value={prFilter}
          onChange={(e) => {
            setOffset(0);
            setPrFilter(e.target.value.replace(/\D/g, ""));
          }}
          placeholder="PR #"
          className="w-24 px-2 py-1.5 text-xs rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] tabular"
        />
      </div>

      {rows === null && (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      )}

      {rows !== null && rows.length === 0 && (
        <EmptyState
          icon={Package}
          title="No packs yet"
          description={
            prFilter
              ? `No precomputed pack for PR #${prFilter}. Unmerged PRs get an orientation pack on demand.`
              : "Packs appear as merges are ingested."
          }
        />
      )}

      {rows !== null && rows.length > 0 && (
        <div className="rounded-xl border border-[var(--color-border)] overflow-hidden">
          {rows.map((r) => (
            <div key={r.pr_number} className="border-b border-[var(--color-border)] last:border-b-0">
              <button
                onClick={() => void toggle(r.pr_number)}
                className="w-full text-left flex items-center gap-3 px-3 py-2.5 hover:bg-[var(--color-surface-2)]"
              >
                {open === r.pr_number ? (
                  <ChevronDown size={14} className="shrink-0 text-[var(--color-text-subtle)]" />
                ) : (
                  <ChevronRight size={14} className="shrink-0 text-[var(--color-text-subtle)]" />
                )}
                <a
                  href={`https://github.com/${repo}/pull/${r.pr_number}`}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="font-mono text-[12px] text-[var(--color-accent)] hover:underline shrink-0 tabular"
                >
                  #{r.pr_number}
                </a>
                <span className="text-[13px] truncate flex-1">
                  {r.title || <span className="text-[var(--color-text-subtle)]">(title not in history yet)</span>}
                </span>
                <span className="text-[11px] text-[var(--color-text-muted)] tabular shrink-0 hidden md:inline">
                  {r.author}
                </span>
                <span className="text-[11px] text-[var(--color-text-muted)] tabular shrink-0">
                  {fmtInt(r.pack_tokens)} tok
                </span>
                <span className="text-[11px] text-[var(--color-text-muted)] tabular shrink-0">
                  {relativeTime(r.created_at.replace(" ", "T") + "Z")}
                </span>
              </button>
              {open === r.pr_number && (
                <div className="px-4 pb-4 bg-[var(--color-bg)]">
                  {!pack || pack.pr !== r.pr_number ? (
                    <Skeleton className="h-16 w-full" />
                  ) : (
                    <>
                      <div className="text-[11px] text-[var(--color-text-muted)] py-2 tabular">
                        {pack.source} · {fmtInt(pack.tokens)} tokens · merge <Mono>{short(r.merge_commit_sha)}</Mono>
                      </div>
                      <pre className="text-[11px] font-mono whitespace-pre-wrap text-[var(--color-text-muted)] max-h-[32rem] overflow-auto">
                        {pack.text}
                      </pre>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {total > PAGE && (
        <div className="flex items-center justify-between text-[12px] text-[var(--color-text-muted)] tabular">
          <span>
            {offset + 1}–{Math.min(offset + PAGE, total)} of {fmtInt(total)}
          </span>
          <div className="flex gap-2">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE))}
              className="px-2.5 py-1 bg-[var(--color-surface-2)] rounded-md disabled:opacity-40"
            >
              Newer
            </button>
            <button
              disabled={offset + PAGE >= total}
              onClick={() => setOffset(offset + PAGE)}
              className="px-2.5 py-1 bg-[var(--color-surface-2)] rounded-md disabled:opacity-40"
            >
              Older
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Access
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Access: the maintainer's side of repository access. One rule per repository — open,
// restricted or closed — said in the same words the Repositories page uses, plus the two
// lists that rule produces: requests waiting for a maintainer, and the readers who
// already hold access. Never "public" or "private": every repository is listed and
// searchable once you read it; only the way in differs.
// ---------------------------------------------------------------------------

const JOIN_RULES = ["open", "approval", "closed"] as const;
type JoinRule = (typeof JOIN_RULES)[number];

const RULE_META: Record<
  JoinRule,
  { label: string; icon: LucideIcon; tone: Fresh["tone"]; headline: (org: string) => string; detail: string }
> = {
  open: {
    label: "Open",
    icon: Globe,
    tone: "ok",
    headline: (org) => `Anyone in ${org} joins instantly as a reader`,
    detail: "One click on the Repositories page, no waiting. Right for source that is already public.",
  },
  approval: {
    label: "Restricted",
    icon: Lock,
    tone: "warn",
    headline: () => "A maintainer approves each reader",
    detail:
      "People request access from the Repositories page; requests wait here until you approve or decline. Right for source that is not public.",
  },
  closed: {
    label: "Closed",
    icon: ShieldCheck,
    tone: "muted",
    headline: () => "Only readers a maintainer adds",
    detail: "Nobody can request; you add readers by name below. Use it for an index that is being rebuilt or retired.",
  },
};

function effectiveRule(ns: NamespaceInfo | null | undefined): JoinRule | null {
  if (!ns) return null;
  if (ns.effective_join_policy) return ns.effective_join_policy;
  if (ns.join_policy && ns.join_policy !== "inherit") return ns.join_policy;
  return ns.visibility === "public" ? "open" : "approval";
}

function AccessTab({
  id,
  siblings = [],
  repo: repoRow,
  onRetired,
}: {
  id: CodeId;
  siblings?: CodeRepoSummary[];
  repo?: CodeRepoSummary;
  onRetired?: () => void;
}) {
  const whoami = useStore((s) => s.whoami);
  const [orgReadable, setOrgReadable] = useState<boolean>(false);
  const [ns, setNs] = useState<NamespaceInfo | null | undefined>(undefined);
  const [grants, setGrants] = useState<GrantEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [subject, setSubject] = useState("");
  const [role, setRole] = useState<GrantRole>("viewer");
  const [busy, setBusy] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const load = useCallback(
    (signal?: AbortSignal, live: () => boolean = () => true) => {
      // Shared with the Health tab — one code.index_status per repository page.
      sharedIndexStatus(id)
        .then((s) => {
          if (live()) setOrgReadable(!!s.org_readable);
        })
        .catch(() => {});
      getNamespace(id, { signal })
        .then((n) => {
          if (live()) setNs(n);
        })
        .catch(() => {
          if (live()) setNs(null);
        });
      listGrants(id, { signal })
        .then((r) => {
          if (live()) {
            setGrants(r.rows);
            setError(null);
          }
        })
        .catch((e) => {
          if (live()) setError(humanizeError(e));
        });
    },
    // `id` is a fresh object each parent render; its two fields are the identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [id.org, id.namespace],
  );
  useAbortableEffect(load, [load]);

  const me = whoami?.subject;
  const myGrant = grants?.find((g) => g.subject === me);
  const canManage = holdsAdminGrant(whoami, myGrant);
  const roleWord = (r: string) => (r === "viewer" ? "reader" : r === "admin" ? "maintainer" : r);
  const active = (grants || []).filter((g) => g.status === "active");
  // Two shapes of "waiting" (namespace.join): no access yet (status pending),
  // or a reader asking to become a maintainer (active + pending_role).
  const waiting = (grants || []).filter((g) => g.status === "pending" || !!g.pending_role);
  const askedFor = (g: GrantEntry): RepoRole => ((g.pending_role || g.role) === "admin" ? "admin" : "viewer");
  const hasAdmin = active.some((g) => g.role === "admin");
  const rule = effectiveRule(ns);

  const setRule = async (next: JoinRule) => {
    if (!ns || next === rule) return;
    const n = waiting.length;
    const consequence =
      next === "open"
        ? `Everyone in ${id.org} can join instantly from now on. Current readers keep access.${n ? ` ${n} waiting request${n === 1 ? "" : "s"} still need${n === 1 ? "s" : ""} your approval — or the person simply joins.` : ""}`
        : next === "approval"
          ? "New readers wait for a maintainer's approval. Current readers keep access."
          : `Nobody can request access any more; maintainers add readers by name. Current readers keep access.${n ? ` ${n} waiting request${n === 1 ? "" : "s"} stay${n === 1 ? "s" : ""} until you decide.` : ""}`;
    if (
      !(await confirmAction({
        title: `Make ${id.namespace} ${RULE_META[next].label.toLowerCase()}?`,
        message: consequence,
        confirmLabel: `Make it ${RULE_META[next].label.toLowerCase()}`,
      }))
    )
      return;
    setBusy("rule");
    try {
      await reregisterNamespace(id, ns, { join_policy: next });
      toast(`${id.namespace} is now ${RULE_META[next].label.toLowerCase()}`, "ok");
      load();
    } catch (err) {
      toast(`Couldn't change the rule: ${humanizeError(err)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const decide = async (g: GrantEntry, decision: "approve" | "reject", as?: RepoRole) => {
    setBusy(g.subject);
    try {
      await approveGrant(id, g.subject, decision, as);
      const granted = as ?? askedFor(g);
      toast(
        decision === "approve"
          ? granted === "admin"
            ? `${g.subject} is now a maintainer of ${id.namespace}`
            : `${g.subject} can now read ${id.namespace}`
          : `Declined ${g.subject}`,
        "ok",
      );
      load();
    } catch (err) {
      toast(humanizeError(err), "error");
    } finally {
      setBusy(null);
    }
  };

  const approveAll = async () => {
    setBusy("all");
    let ok = 0;
    for (const g of waiting) {
      try {
        await approveGrant(id, g.subject, "approve");
        ok++;
      } catch {
        /* keep going */
      }
    }
    toast(`Approved ${ok} of ${waiting.length}`, ok === waiting.length ? "ok" : "error");
    setBusy(null);
    load();
  };

  const onAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const s = subject.trim();
    if (!s) return;
    setBusy("add");
    try {
      await createGrant(id, s, role);
      toast(`${s} is now a ${roleWord(role)}`, "ok");
      setSubject("");
      load();
    } catch (err) {
      toast(`Couldn't add: ${humanizeError(err)}`, "error");
    } finally {
      setBusy(null);
    }
  };

  const onRevoke = async (g: GrantEntry) => {
    if (
      !(await confirmAction({
        title: `Remove ${g.subject}?`,
        message: `${g.subject} stops reading ${id.namespace}. Tokens that name it stop working for it. ${rule === "open" ? "They can join again in one click." : "They can request again."}`,
        confirmLabel: "Remove",
        danger: true,
      }))
    )
      return;
    try {
      await revokeGrant(id, g.subject);
      toast(`Removed ${g.subject}`, "ok");
      load();
    } catch (err) {
      toast(`Couldn't remove: ${humanizeError(err)}`, "error");
    }
  };

  const RuleIcon = rule ? RULE_META[rule].icon : Lock;

  // A person waiting on several branches of this repository is one decision, not N.
  // Sibling queues are read lazily (one grants.list per branch) once there is someone
  // waiting and the caller may act.
  const others = siblings.filter((b) => b.namespace !== id.namespace && b.my_role === "admin");
  const [siblingWaiting, setSiblingWaiting] = useState<Record<string, CodeRepoSummary[]>>({});
  useEffect(() => {
    if (!canManage || waiting.length === 0 || others.length === 0) {
      setSiblingWaiting({});
      return;
    }
    let cancelled = false;
    (async () => {
      const found: Record<string, CodeRepoSummary[]> = {};
      await Promise.all(
        others.map(async (b) => {
          try {
            const r = await listGrants({ org: b.org, namespace: b.namespace });
            for (const g of r.rows) {
              if (g.status === "pending" || g.pending_role) (found[g.subject] ??= []).push(b);
            }
          } catch {
            /* a branch we cannot list is simply not offered */
          }
        }),
      );
      if (!cancelled) setSiblingWaiting(found);
    })();
    return () => {
      cancelled = true;
    };
    // `others` is rebuilt each render; the joined namespaces are its identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManage, waiting.length, others.map((b) => b.namespace).join("|")]);

  const approveEverywhere = async (g: GrantEntry) => {
    const targets = [
      { org: id.org, namespace: id.namespace },
      ...(siblingWaiting[g.subject] ?? []).map((b) => ({ org: b.org, namespace: b.namespace })),
    ];
    setBusy(g.subject);
    let ok = 0;
    for (const t of targets) {
      try {
        await approveGrant(t, g.subject, "approve");
        ok++;
      } catch {
        /* keep going */
      }
    }
    toast(`${g.subject} approved on ${ok} of ${targets.length} branches`, ok === targets.length ? "ok" : "error");
    setBusy(null);
    load();
  };

  return (
    <div className="space-y-5">
      {/* 1. The rule — one decision per repository, not one per person. */}
      <section
        className="p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]"
        aria-label="How people join"
      >
        {ns === undefined && <Skeleton className="h-16 w-full" />}
        {ns === null && (
          <div className="text-[12px] text-[var(--color-warn)] flex items-start gap-2">
            <AlertTriangle size={14} className="shrink-0 mt-0.5" />
            No directory row yet — the indexer registers one on its next pass, or you lack access to read it.
          </div>
        )}
        {ns && rule && (
          <>
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div className="min-w-0">
                <div className="text-[11px] uppercase tracking-wide text-[var(--color-text-subtle)]">
                  How people join
                </div>
                <div className="mt-1 text-[15px] font-medium inline-flex items-center gap-2">
                  <RuleIcon
                    size={15}
                    className={
                      rule === "open"
                        ? "text-[var(--color-ok)]"
                        : rule === "approval"
                          ? "text-[var(--color-warn)]"
                          : "text-[var(--color-text-muted)]"
                    }
                  />
                  {RULE_META[rule].headline(id.org)}
                  <Pill tone={RULE_META[rule].tone}>{RULE_META[rule].label.toLowerCase()}</Pill>
                  <span className="text-[12px] font-normal text-[var(--color-text-muted)]">read-only</span>
                </div>
                <p className="mt-1.5 text-[12px] text-[var(--color-text-muted)] max-w-2xl">{RULE_META[rule].detail}</p>
              </div>
              {canManage && (
                <div className="flex flex-col items-end gap-2">
                  <div
                    className="inline-flex rounded-md border border-[var(--color-border)] overflow-hidden"
                    role="radiogroup"
                    aria-label="Join rule"
                  >
                    {JOIN_RULES.map((r) => {
                      const Icon = RULE_META[r].icon;
                      const on = r === rule;
                      return (
                        <button
                          key={r}
                          type="button"
                          role="radio"
                          aria-checked={on}
                          title={RULE_META[r].headline(id.org)}
                          onClick={() => void setRule(r)}
                          disabled={busy !== null || on}
                          className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium border-r border-[var(--color-border)] last:border-r-0 disabled:cursor-default ${on ? "bg-[var(--color-accent-soft)] text-[var(--color-text)]" : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]"}`}
                        >
                          <Icon size={12} /> {RULE_META[r].label}
                        </button>
                      );
                    })}
                  </div>
                  <button
                    onClick={() => setSettingsOpen(true)}
                    className="text-[12px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:underline inline-flex items-center gap-1"
                  >
                    <Settings size={12} /> Description & more
                  </button>
                </div>
              )}
            </div>
            {orgReadable && (
              <div className="mt-3 p-2.5 rounded-lg border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 text-[12px] flex items-start gap-2">
                <AlertTriangle size={13} className="text-[var(--color-warn)] shrink-0 mt-0.5" />
                <span>
                  This deployment still lets every principal read every repository (<Mono>LOOM_CODE_ORG_READABLE</Mono>
                  ), so the rule above is not enforced until an operator turns that off.
                </span>
              </div>
            )}
          </>
        )}
      </section>

      {error && (
        <EmptyState
          icon={ShieldCheck}
          title="Readers are visible to readers"
          description={`${error}. Join this repository to see who else reads it.`}
        />
      )}
      {grants === null && !error && <Skeleton className="h-20 w-full" />}

      {/* 2. Requests waiting for a maintainer — first, because it is the only
             thing here with a deadline attached to someone else's day. */}
      {grants !== null && waiting.length > 0 && (
        <section
          className="rounded-xl border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/5 overflow-hidden"
          aria-label="Waiting for approval"
        >
          <div className="flex items-center justify-between gap-3 px-3.5 py-2.5 border-b border-[var(--color-warn)]/30">
            <div className="text-[13px] font-medium inline-flex items-center gap-2">
              <Clock size={14} className="text-[var(--color-warn)]" /> Waiting for a maintainer{" "}
              <span className="tabular text-[var(--color-text-muted)]">({waiting.length})</span>
            </div>
            {canManage && waiting.length > 1 && (
              <button
                onClick={() => void approveAll()}
                disabled={busy !== null}
                className="text-[12px] font-medium text-[var(--color-accent)] hover:underline disabled:opacity-50"
              >
                Approve all
              </button>
            )}
          </div>
          {waiting.map((g) => {
            const asked = askedFor(g);
            return (
              <div
                key={g.subject}
                className="flex items-center gap-3 px-3.5 py-2.5 border-b border-[var(--color-border)] last:border-b-0 text-[12px] flex-wrap"
              >
                <span className="font-mono flex-1 min-w-[12rem] truncate">{g.subject}</span>
                <span className="text-[var(--color-text-muted)]">
                  asks to be a <span className="font-medium text-[var(--color-text)]">{ROLE_WORD[asked]}</span>
                  {g.status !== "pending" && g.role === "viewer" && asked === "admin" ? " (reads today)" : ""}
                </span>
                <span className="text-[var(--color-text-subtle)] tabular hidden md:inline">
                  {g.pending_since ? relativeTime(g.pending_since) : ""}
                </span>
                {canManage ? (
                  <>
                    <button
                      onClick={() => void decide(g, "approve")}
                      disabled={busy !== null}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] font-medium disabled:opacity-50"
                    >
                      <Check size={12} /> Approve as {ROLE_WORD[asked]}
                    </button>
                    {(siblingWaiting[g.subject]?.length ?? 0) > 0 && (
                      <button
                        onClick={() => void approveEverywhere(g)}
                        disabled={busy !== null}
                        title={`Also waiting on ${siblingWaiting[g.subject].map((b) => b.branch).join(", ")}`}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-[var(--color-accent)]/50 text-[var(--color-accent)] hover:bg-[var(--color-accent-soft)] disabled:opacity-50"
                      >
                        <Check size={12} /> Approve on all {siblingWaiting[g.subject].length + 1} branches
                      </button>
                    )}
                    {asked === "admin" && (
                      <button
                        onClick={() => void decide(g, "approve", "viewer")}
                        disabled={busy !== null}
                        title="Admit as a reader instead of a maintainer"
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-50"
                      >
                        Reader instead
                      </button>
                    )}
                    <button
                      onClick={() => void decide(g, "reject")}
                      disabled={busy !== null}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-[var(--color-border)] text-[var(--color-text-muted)] hover:text-[var(--color-danger)] hover:border-[var(--color-danger)]/50 disabled:opacity-50"
                    >
                      <X size={12} /> Decline
                    </button>
                  </>
                ) : (
                  <span className="text-[var(--color-text-muted)]">waiting</span>
                )}
              </div>
            );
          })}
        </section>
      )}

      {/* 3. Who reads it today. */}
      {grants !== null && (
        <section className="space-y-3" aria-label="Readers">
          <div className="flex items-center justify-between gap-3">
            <div className="section-label">
              Readers <span className="tabular">({active.length})</span>
            </div>
          </div>
          {!hasAdmin && (
            <div className="p-3 rounded-lg border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 text-[12px]">
              Registered by the indexer, no maintainer recorded yet. An operator grants the first maintainer; after that
              the maintainer sets the rule above and approves readers.
            </div>
          )}
          {active.length === 0 ? (
            <div className="text-[12px] text-[var(--color-text-subtle)]">
              Nobody reads this repository yet.{" "}
              {rule === "open"
                ? "People join from the Repositories page."
                : rule === "approval"
                  ? "People request access from the Repositories page and appear above."
                  : "Add a reader below."}
            </div>
          ) : (
            <div className="rounded-xl border border-[var(--color-border)] overflow-hidden">
              {active.map((g) => (
                <div
                  key={g.subject}
                  className="flex items-center gap-3 px-3 py-2 border-b border-[var(--color-border)] last:border-b-0 text-[12px]"
                >
                  {g.role === "admin" ? (
                    <ShieldCheck size={12} className="text-[var(--color-accent)] shrink-0" />
                  ) : (
                    <Database size={12} className="text-[var(--color-text-subtle)] shrink-0" />
                  )}
                  <span className="font-mono flex-1 truncate">
                    {g.subject}
                    {g.subject === me && <span className="ml-2 text-[var(--color-text-subtle)]">(you)</span>}
                  </span>
                  <span className="px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
                    {roleWord(g.role)}
                    {g.pending_role ? " · asked to maintain" : ""}
                  </span>
                  <span
                    className="text-[var(--color-text-subtle)] tabular hidden md:inline"
                    title={`added by ${g.granted_by}`}
                  >
                    {relativeTime(g.granted_at)}
                  </span>
                  {canManage && g.subject !== me && (
                    <button
                      onClick={() => void onRevoke(g)}
                      title="Remove reader"
                      aria-label={`Remove ${g.subject}`}
                      className="p-1 rounded hover:bg-[var(--color-surface-2)] text-[var(--color-text-muted)] hover:text-[var(--color-danger)]"
                    >
                      <Trash2 size={12} />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {canManage && hasAdmin && (
            <form onSubmit={onAdd} className="flex flex-wrap gap-2 items-center" aria-label="Add a reader">
              <input
                value={subject}
                onChange={(e) => setSubject(e.target.value)}
                placeholder="email or service-account subject"
                aria-label="Subject to add"
                className="flex-1 min-w-[16rem] px-3 py-2 text-[13px] rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] outline-none focus:border-[var(--color-accent)] font-mono"
              />
              <select
                value={role}
                onChange={(e) => {
                  const v = oneOf(
                    REPO_ROLES.map((o) => o.value),
                    e.target.value,
                  );
                  if (v !== null) setRole(v);
                }}
                aria-label="Role"
                className="px-2 py-2 text-[13px] rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]"
              >
                {REPO_ROLES.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <button
                type="submit"
                disabled={busy !== null || !subject.trim()}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] rounded-md disabled:opacity-50"
              >
                <Plus size={13} /> Add
              </button>
              <span className="text-[11px] text-[var(--color-text-subtle)] basis-full">
                Adding someone by name works under every rule. Agents read with a token that names this repository.
              </span>
            </form>
          )}
        </section>
      )}

      {/* 4. Retire — the one irreversible verb, last, red, and
             behind a confirmation that spells out what goes. Operator-configured
             entries say who owns them instead. */}
      {canManage && repoRow && (
        <section
          className="rounded-xl border border-[var(--color-border)] p-4 flex items-start justify-between gap-4 flex-wrap"
          aria-label="Retire"
        >
          <div className="min-w-0">
            <div className="text-[13px] font-medium">Retire this index</div>
            <p className="mt-1 text-[12px] text-[var(--color-text-muted)] max-w-xl">
              {repoRow.source === "registry" ? (
                <>
                  Removes the index of{" "}
                  <b>
                    {repoRow.repo} @ {repoRow.branch}
                  </b>
                  : readers lose access, tokens naming it stop working, every indexed row is purged by an erasure you
                  can follow under Audit → Erasures, and the mirror is deleted when no other branch uses it. Adding it
                  again re-indexes from scratch.
                </>
              ) : (
                <>
                  This index comes from the deployment's configuration. An operator retires it by removing its entry and
                  swapping; the page cannot.
                </>
              )}
            </p>
          </div>
          <button
            disabled={repoRow.source !== "registry" || busy !== null}
            onClick={async () => {
              if (
                !(await confirmAction({
                  title: `Retire ${repoRow.repo} @ ${repoRow.branch}?`,
                  message: `The index ${id.namespace} is retired now: its readers are refused from this moment, its rows are purged by an erasure that runs in the background (Audit → Erasures), and the mirror goes if no other branch uses it. This cannot be undone; adding the repository again re-indexes from scratch.`,
                  confirmLabel: "Retire index",
                  danger: true,
                }))
              )
                return;
              setBusy("retire");
              try {
                const out = await removeCodeRepo(id.org, id.namespace);
                toast(
                  `Retired ${out.repo} @ ${out.branch} · ${out.purged_tables} tables purging as erasure ${out.erasure_id.slice(0, 8)}${out.mirror_removed ? " · mirror removed" : ""}`,
                  "ok",
                );
                onRetired?.();
              } catch (err) {
                toast(humanizeError(err), "error");
              } finally {
                setBusy(null);
              }
            }}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium rounded-md border border-[var(--color-danger)]/50 text-[var(--color-danger)] hover:bg-[var(--color-danger)]/10 disabled:opacity-50 whitespace-nowrap"
            title={
              repoRow.source === "registry"
                ? "Irreversible; a confirmation follows"
                : "Managed by the deployment config"
            }
          >
            <Trash2 size={13} /> Retire…
          </button>
        </section>
      )}

      <NamespaceSettingsDialog
        open={settingsOpen}
        onOpenChange={(o) => {
          setSettingsOpen(o);
          if (!o) load();
        }}
        org={id.org}
        namespace={id.namespace}
      />
    </div>
  );
}
