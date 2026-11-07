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
import { lazy, Suspense, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import { Table } from "@clickhouse/click-ui/Table";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextField } from "@clickhouse/click-ui/TextField";
import { VerticalStepper } from "@clickhouse/click-ui/VerticalStepper";
import { PageContainer } from "../layout/PageContainer";
import { useStore, useIdentity } from "../lib/state";
import { holdsAdminGrant } from "../lib/role";
import { fmtInt, relativeTime, humanizeError, listTime, parseChTime, refreshDetail } from "../lib/format";
import { useAbortableEffect, useVisiblePoll } from "../lib/hooks";
import { EmptyState } from "../components/EmptyState";
import { IconButton } from "../components/IconButton";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { RowSkeleton, Skeleton, SkeletonCard } from "../components/Skeleton";
import { toast } from "../components/Toast";
import { ListPager, usePageSize } from "../components/ui/ListPager";
import { ListState } from "../components/ui/ListState";
import { ListToolbar } from "../components/ui/ListToolbar";
import { BusyButton } from "../components/ui/BusyButton";
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
import { RepositoryCard, BranchChip, FRESH_STATE, type CodeActions, type RecordActionProps } from "./RepositoryCard";
import { useTrackerSources, useRecordActions, TRACKER_SOURCES_ABSENT } from "./TrackerSources";
import { TrackersPanel } from "./TrackersPanel";
import {
  Alert,
  Chip,
  ExternalLink,
  fieldError,
  ListRow,
  ListTime,
  Loading,
  LoadingSkeleton,
  PageHeader,
  Stat,
  TabTrigger,
  TextClamp,
  Tip,
  TipTarget,
} from "../components/ui";
import { isRecord } from "../lib/guards";
import { oneOf } from "../lib/oneOf";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { useRowNav } from "../lib/useRowNav";

// The register dialog loads on first open.
const RegisterTrackerSourceDialog = lazy(() =>
  import("../flows/TrackerSourceDialogs").then((m) => ({ default: m.RegisterTrackerSourceDialog })),
);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

const short = (sha: string) => (sha || "").slice(0, 12);

function Mono({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <span className={`font-mono text-xs ${className}`}>{children}</span>;
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
// Repositories home. One card per repository; each branch is a row of its table, with its
// state and a menu of its own. A person is joined to nothing by default: an open
// repository joins in one click, a restricted one is requested and a maintainer approves.
// Every surface here says read-only.
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
  // One tab stop for the list, as every row list (lib/useRowNav): the arrows move between
  // repositories, and only the current one's controls are in the tab order.
  const repoList = useRowNav();
  const actions = useJoinActions(onChanged);
  const phone = useMedia(PHONE_QUERY);
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
  // Most orgs hold a handful of repositories: the pager draws once there are more than fit
  // the smallest page.
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = usePageSize("repositories");
  // The record facet — the tracker namespaces whose `repos` name a repository — is read
  // once here and joined onto the cards by repository.
  const tracker = useTrackerSources(org);
  const record = useRecordActions(org, tracker.refresh);
  const cards = useMemo(() => buildCards(repos ?? [], tracker.sources), [repos, tracker.sources]);
  // The cards filter by the text a render behind the box, so a keystroke paints the box
  // first; the memoised cards render again only when what they show changes.
  const listQuery = useDeferredValue(query).trim();
  const visible = useMemo(() => {
    const q = listQuery.toLowerCase();
    if (!q) return cards;
    return cards.filter(
      (c) =>
        c.repo.toLowerCase().includes(q) ||
        (c.code?.branches ?? []).some(
          (b) => b.branch.toLowerCase().includes(q) || b.namespace.toLowerCase().includes(q),
        ) ||
        (c.record?.namespaces ?? []).some((n) => n.ns.namespace.toLowerCase().includes(q)),
    );
  }, [cards, listQuery]);
  const pageCards = visible.slice(page * pageSize, (page + 1) * pageSize);
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
  const busy = actions.busy ?? record.busy;
  // A failed poll after a read keeps the cards; only a failed first read replaces them.
  const loaded = repos !== null;
  const empty = loaded && repos.length === 0 && cards.length === 0;
  // The toolbar's count: the whole list, or what the filter matches.
  const count = !loaded
    ? undefined
    : listQuery
      ? `${visible.length} match${visible.length === 1 ? "" : "es"}`
      : `${totals.repos} repositor${totals.repos === 1 ? "y" : "ies"} · ${totals.branches} branch${totals.branches === 1 ? "" : "es"}`;
  // Said on each repository's tracker line when no tracker can be shown.
  const trackerNote =
    tracker.status.kind === "absent"
      ? TRACKER_SOURCES_ABSENT
      : tracker.status.kind === "error"
        ? `Couldn't load the trackers — ${tracker.status.message}`
        : undefined;

  const bindSource = async (g: RepoGroup, tracker_namespaces: string[]) => {
    try {
      const out = await setCodeRepoSource(g.primary.org, g.primary.namespace, tracker_namespaces);
      // the card's tracker lines say what each link means; the toast confirms, with the
      // server's warning (trackers that do not cover the repository, branches that read the
      // links too) underneath
      toast(
        out.tracker_namespaces.length
          ? `${g.primary.repo} reads ${out.tracker_namespaces.join(" + ")}`
          : `${g.primary.repo} unlinked`,
        "ok",
        out.warning ? { detail: out.warning } : {},
      );
      onChanged();
    } catch (e) {
      toast(`Couldn't link the tracker: ${humanizeError(e)}`, "error");
    }
  };
  const refreshBranch = async (b: CodeRepoSummary) => {
    try {
      const r = await codeRefresh({ org: b.org, namespace: b.namespace });
      toast(
        r.already_running
          ? `An ingest pass is already running on ${b.repo} @ ${b.branch}`
          : `Ingest pass triggered on ${b.repo} @ ${b.branch}`,
        "ok",
      );
    } catch (e) {
      toast(`Refresh failed: ${humanizeError(e)}`, "error");
    }
  };
  const removeBranch = async (b: CodeRepoSummary) => {
    if (
      !(await confirmAction({
        title: `Retire ${b.repo} @ ${b.branch}?`,
        message: `The index ${b.namespace} is retired now: its readers are refused from this moment, its rows are purged by an erasure that runs in the background (Audit → Erasures), and the mirror goes if no other branch uses it. This cannot be undone; adding the branch again re-indexes from scratch.`,
        confirmLabel: "Retire index",
        danger: true,
      }))
    )
      return;
    try {
      const out = await removeCodeRepo(b.org, b.namespace);
      toast(`Retired ${out.repo} @ ${out.branch} · its rows are purging in the background`, "ok");
      onChanged();
    } catch (e) {
      toast(humanizeError(e), "error");
    }
  };
  /** The links a repository holds now, primary first. */
  const linksOf = (g: RepoGroup): string[] => g.primary.bound_trackers ?? [];

  // Every branch the person maintains takes the new access. Opening asks for one typed name,
  // the primary's, and that confirms opening every branch the person maintains.
  const setAccess = async (
    g: RepoGroup,
    classification: "standard" | "restricted",
    join_policy: "open" | "approval" | "closed",
    opening: boolean,
  ) => {
    const mine = g.branches.filter((b) => b.my_role === "admin");
    if (opening) {
      const typed =
        window.prompt(
          `Opening ${g.primary.repo} to every reader of the org. Type the namespace (${g.primary.namespace}) to confirm.`,
        ) ?? "";
      if (typed.trim() !== g.primary.namespace) {
        toast("Not opened — the namespace did not match", "error");
        return;
      }
    }
    let done = 0;
    for (const b of mine) {
      try {
        await setCodeRepoAccess(b.org, b.namespace, classification, join_policy, opening ? b.namespace : "");
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
  };
  // The cards call the latest render's handlers through objects that keep their identity
  // while the data they carry is unchanged, so a memoised card renders again only when its
  // own fields change.
  const now = { onOpen, onChanged, actions, record, refreshBranch, removeBranch, setAccess, bindSource };
  const handlers = useRef(now);
  handlers.current = now;

  const cardCode = useMemo<CodeActions>(
    () => ({
      onOpen: (b) => handlers.current.onOpen(b.namespace),
      onJoin: (b) => void handlers.current.actions.join(b),
      onRequest: (b, role) => setRequesting({ repo: b, role: role ?? "viewer" }),
      onCancel: (b) => void handlers.current.actions.cancel(b),
      onLeave: (b) => void handlers.current.actions.leave(b),
      onJoinAll: (bs) => void handlers.current.actions.joinAll(bs),
      onLeaveAll: (bs) => void handlers.current.actions.leaveAll(bs),
      onManage: setManaging,
      onAddBranch: canAdd ? setAddingBranch : undefined,
      onRefresh: (b) => void handlers.current.refreshBranch(b),
      onRemove: (b) => void handlers.current.removeBranch(b),
      onSetAccess: (g, classification, join_policy, opening) =>
        void handlers.current.setAccess(g, classification, join_policy, opening),
      onSetSource: (g, namespaces) => void handlers.current.bindSource(g, namespaces),
      trackerSources: tracker.sources,
      onRegisterSource: tracker.selfService ? (g) => setRegistering({ group: g }) : undefined,
    }),
    [canAdd, tracker.sources, tracker.selfService],
  );
  const cardRecord = useMemo<RecordActionProps>(
    () => ({
      onOpen: (r) => handlers.current.record.open(r.ns),
      onJoin: (r, confirm) => void handlers.current.record.join(r.ns, confirm),
      onRequest: (r) => void handlers.current.record.request(r.ns),
      onCancel: (r) => void handlers.current.record.cancel(r.ns),
      onLeave: (r) => void handlers.current.record.leave(r.ns),
      org,
      onSourcesChanged: () => {
        void tracker.refresh();
        handlers.current.onChanged();
      },
    }),
    [org, tracker.refresh],
  );

  return (
    <PageContainer>
      <PageHeader
        title="Repositories"
        actions={
          org && (
            <>
              {tracker.selfService && (
                <Tip content="Connect a ClickHouse database of issues, PRs and incidents. You become its admin.">
                  <Button
                    type="secondary"
                    htmlType="button"
                    iconLeft="plus"
                    onClick={() => setRegistering({ group: null })}
                  >
                    Add a tracker
                  </Button>
                </Tip>
              )}
              {/* The empty state carries the one Add repository while the list is empty. */}
              {canAdd && !empty && (
                <Tip content="Index a Git repository. You become its maintainer.">
                  <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAdding(true)}>
                    Add repository
                  </Button>
                </Tip>
              )}
            </>
          )
        }
      />
      {!empty && (
        <p className="text-sm text-text-muted mb-3 max-w-[72ch]">
          Indexed code branches and their trackers of issues and PRs, read-only.{" "}
          {/* As tall as the text: a 44 px touch target overflows it instead of stretching the line. */}
          <span className="inline-flex h-5 items-center align-middle">
            <IconButton
              icon="info-in-circle"
              size="xs"
              label="About repositories"
              tooltip="You can search and ask about a branch's code; the tracker is read live. An open repository joins in one click; a restricted one is requested and a maintainer approves."
            />
          </span>
        </p>
      )}

      {pendingRows.length > 0 && (
        <Alert
          className="mb-3"
          state="info"
          customIcon="clock"
          text={
            <span className="break-words">
              <strong className="font-medium">Waiting for a maintainer.</strong>{" "}
              {pendingRows.map((r) => `${r.repo} @ ${r.branch}`).join(" · ")}. You'll be able to read as soon as a
              maintainer approves; nothing else to do.
            </span>
          }
        />
      )}

      {!empty && (
        <ListToolbar label="Search and filters" count={count}>
          <div className="min-w-0 flex-1">
            <SearchField
              value={query}
              onChange={(text) => {
                setQuery(text);
                setPage(0);
              }}
              // A phone's field, beside the count, holds the short form only.
              placeholder={phone ? "Filter by name" : "Filter repositories or branches"}
              aria-label="Filter repositories"
            />
          </div>
        </ListToolbar>
      )}

      {repos === null && !error && (
        <LoadingSkeleton label="Loading repositories">
          <div className="space-y-2">
            <RowSkeleton />
            <RowSkeleton />
            <RowSkeleton />
          </div>
        </LoadingSkeleton>
      )}
      {error && repos === null && (
        <ListState kind="error" things="repositories" error={error} onRetry={() => onChanged()} />
      )}
      {error && repos !== null && (
        <div className="mb-3 text-xs text-text-muted" role="status">
          {`Couldn't refresh the repositories (${error}); showing the last read.`}
        </div>
      )}

      {empty && (
        <EmptyState
          icon="code"
          title="Index your first repository"
          description={
            canAdd
              ? "Loom indexes a Git branch so you and your agents can search and ask about its code."
              : sandbox === "warn"
                ? "This server cannot add repositories from this page, because it runs without the repository-job sandbox. Ask the operator to list them in LOOM_CODE_REPOS_JSON."
                : "This server cannot index repositories until its repository-job sandbox runs. Ask the operator."
          }
          action={org && canAdd ? { label: "Add repository", onClick: () => setAdding(true) } : undefined}
        >
          {canAdd && <OnboardingSteps />}
        </EmptyState>
      )}

      {loaded && visible.length > 0 && (
        // Folded rows, so the list is a compact stack; a row grows only when opened. A new
        // page scrolls here and takes focus.
        <div ref={repoList} id="repositories-list" tabIndex={-1} className="space-y-2">
          {pageCards.map((c) => (
            <RepositoryCard
              key={c.repo}
              card={c}
              busy={busy}
              code={cardCode}
              record={cardRecord}
              adminOpsPresent={tracker.selfService}
              trackerNote={trackerNote}
            />
          ))}
        </div>
      )}
      {loaded && visible.length > 0 && (
        <ListPager
          label="Repository pages"
          page={page}
          pageSize={pageSize}
          shown={pageCards.length}
          total={visible.length}
          onPage={setPage}
          onPageSize={(n) => {
            setPageSize(n);
            setPage(0);
          }}
          topId="repositories-list"
        />
      )}
      {loaded && cards.length > 0 && visible.length === 0 && (
        <ListState kind="filtered" things="repositories or branches" filter={listQuery} onClear={() => setQuery("")} />
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

/** The empty page's three steps, in Click UI's VerticalStepper. The steps
 *  say what happens; none is a control, so their buttons stay disabled (no tab stop). */
function OnboardingSteps() {
  const steps = [
    ["Add a Git remote and a branch", "You become the repository's maintainer."],
    ["Loom indexes it in the background", "Minutes for about 10,000 files; the row says how far it is."],
    ["Agents search it with code.search", "Join the repository, then mint a token for your agents."],
  ];
  return (
    <div className="mt-6 w-full max-w-md text-left">
      <VerticalStepper aria-label="How a repository is indexed">
        {steps.map(([label, hint], i) => (
          <VerticalStepper.Step
            key={label}
            label={label}
            status={i === 0 ? "active" : "incomplete"}
            collapsed={false}
            disabled
          >
            <p className="text-sm text-text-muted">{hint}</p>
          </VerticalStepper.Step>
        ))}
      </VerticalStepper>
    </div>
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
    <Modal
      open={repo !== null}
      onOpenChange={onOpenChange}
      title={repo ? `Join ${repo.repo} @ ${repo.branch}` : ""}
      description={`${
        restricted
          ? "This repository is restricted. Your request goes to its maintainers; you read nothing until one approves."
          : "This repository is open: readers join instantly. Asking to maintain goes to a current maintainer first."
      } Everything on a repository is read-only.`}
      className="max-w-[500px] w-full"
    >
      {repo && (
        <>
          <ChoiceGroup
            label="Join as"
            value={role}
            onChange={setRole}
            options={[
              { value: "viewer", label: "Reader", hint: "Search, ask, grep and explore the index." },
              {
                value: "admin",
                label: "Maintainer",
                hint: "Approve readers, name maintainers, set how people join. Always needs a current maintainer's approval.",
              },
            ]}
          />

          <ul className="mt-3 text-xs text-text-muted space-y-1 list-disc pl-5">
            <li>Nothing is shared about you beyond your sign-in identity.</li>
            <li>
              One approval covers you as a person: every token minted for you, and the service accounts you create, read
              it without naming it.
            </li>
          </ul>
          <ModalFooter>
            <Button type="secondary" htmlType="button" onClick={() => onOpenChange(false)}>
              Not now
            </Button>
            <Button
              type="primary"
              htmlType="button"
              iconLeft={instant ? "users" : "clock"}
              loading={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onConfirm(repo, role);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {verb}
            </Button>
          </ModalFooter>
        </>
      )}
    </Modal>
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
  if (!group) return null;
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
  const leavingOnly = plan.leave.length > 0 && plan.join.length === 0;
  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title={`Branches of ${group.repo}`}
      description={`Tick the branches you want to read. Everything is read-only; ${
        group.restricted
          ? "this repository is restricted, so new branches go to a maintainer for approval"
          : "open branches join instantly"
      }.`}
      className="max-w-[520px] w-full"
    >
      <div className="mb-3 flex items-center gap-3 text-xs">
        <Button type="empty" htmlType="button" onClick={() => setAll(true)}>
          All {selectable.length}
        </Button>
        <Button type="empty" htmlType="button" onClick={() => setAll(false)}>
          None
        </Button>
        <span className="text-text-muted">·</span>
        <span className="text-text-muted tabular">
          {want.size} of {selectable.length} selected
        </span>
      </div>
      {/* biome-ignore lint/a11y/useSemanticElements: the list of branch rows, named for assistive technology; its checkboxes carry their own labels */}
      <div role="group" aria-label="Branches">
        <div className="rounded-xl border border-border divide-y divide-border">
          {rows.map((b) => {
            const st = branchState(b);
            const on = want.has(b.namespace);
            const changed =
              (st === "member" && !on) || (st === "pending" && !on) || ((st === "open" || st === "approval") && on);
            return (
              <div key={b.namespace} className="flex items-center gap-3 px-3 py-2 text-sm">
                <div className="min-w-0 flex-1">
                  <Checkbox
                    checked={on}
                    disabled={st === "closed"}
                    onCheckedChange={(x) =>
                      setWant((prev) => {
                        const n = new Set(prev);
                        if (x === true) n.add(b.namespace);
                        else n.delete(b.namespace);
                        return n;
                      })
                    }
                    aria-label={`${b.branch}: ${rowWord(b)}`}
                    label={<span className="font-mono truncate">{b.branch}</span>}
                  />
                </div>
                {st === "approval" && <Icon name="lock" size="xs" color="var(--color-warn)" aria-hidden="true" />}
                <span className={`text-xs tabular ${changed ? "font-medium text-text" : "text-text-muted"}`}>
                  {rowWord(b)}
                </span>
              </div>
            );
          })}
        </div>
      </div>
      <ModalFooter>
        <span className="mr-auto text-xs text-text-muted tabular">
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
        <Button type="secondary" htmlType="button" onClick={() => onOpenChange(false)}>
          Cancel
        </Button>
        <Button
          type={leavingOnly ? "danger" : "primary"}
          htmlType="button"
          iconLeft="check"
          loading={busy}
          disabled={changes === 0}
          onClick={async () => {
            setBusy(true);
            try {
              await onApply(plan);
            } finally {
              setBusy(false);
            }
          }}
        >
          Apply{changes ? ` ${changes} change${changes === 1 ? "" : "s"}` : ""}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
// ---------------------------------------------------------------------------
// Adding a repository or a branch. Four questions, one button; the worker does the rest
// and the card shows its stage.
// ---------------------------------------------------------------------------

const LABEL = "block text-xs font-medium text-text-muted mb-1";
/** The Select value standing for "detect the producer from the tree" (an empty producer). */
const DETECT = "detect";

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
  const close = (v: boolean) => {
    onOpenChange(v);
    if (!v) reset();
  };
  return (
    <Modal
      open={open}
      onOpenChange={close}
      title="Add a repository"
      description={`One branch of a Git repository becomes a read-only index in ${org}. You become its maintainer. Two separate questions below: whether the Git host lets Loom fetch the source, and who may read the index.`}
      className="max-w-[540px] w-full"
    >
      <form onSubmit={submit}>
        <div className="space-y-5">
          <fieldset className="space-y-3">
            <legend className={LABEL}>Fetching from Git</legend>
            <div className="grid grid-cols-[1fr_auto] gap-3 items-end">
              <div>
                <TextField
                  id="add-repo"
                  label="Repository"
                  value={repo}
                  onChange={(v) => {
                    setRepo(v);
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
                />
                <div className="text-xs text-text-muted mt-1">owner/name, or paste an HTTPS/SSH Git URL</div>
              </div>
              <Button
                type="secondary"
                htmlType="button"
                iconLeft="git-merge"
                loading={looking}
                disabled={!repoValid}
                onClick={() => void lookup()}
                className="mb-5"
              >
                {remote ? "Refresh branches" : "Find branches"}
              </Button>
            </div>
            {remote ? (
              <fieldset>
                <legend className={LABEL}>Branches to index</legend>
                {remote.warnings.map((warning) => (
                  <div key={warning} className="mb-2 text-xs text-warn">
                    {warning}
                  </div>
                ))}
                <div className="text-xs text-text-muted mb-1.5">
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
                    <div className="flex-1">
                      <SearchField
                        aria-label="Search branches"
                        value={search}
                        onChange={(v) => setSearch(v)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            void lookup(search.trim());
                          }
                        }}
                        placeholder={`Search ${fmtInt(remote.total)} branches…`}
                      />
                    </div>
                    <span className="text-xs text-text-muted whitespace-nowrap tabular">
                      showing {shownBranches.length} of {fmtInt(remote.total)}
                      {remote.truncated && q && !shownBranches.some((b) => b.name.toLowerCase().includes(q))
                        ? " — press Enter to search the remote's full list"
                        : ""}
                    </span>
                  </div>
                )}
                <ul className="flex flex-wrap gap-2 list-none p-1 m-0 max-h-56 overflow-y-auto" aria-label="Branches">
                  {shownBranches.map((b) => {
                    const taken = indexedSet.has(b.name);
                    const on = taken || picked.has(b.name);
                    const release = remote.suggested.includes(b.name);
                    const suffix = taken ? " · indexed" : release ? " · release" : "";
                    const tip = taken
                      ? `Already indexed here as ${remote.indexed.find((x) => x.branch === b.name)?.namespace}`
                      : release
                        ? "Release branch"
                        : b.name === remote.default_branch
                          ? "Default branch"
                          : undefined;
                    return (
                      <li key={b.name}>
                        <Chip
                          label={`${b.name}${suffix}`}
                          mono
                          selected={on}
                          disabled={taken}
                          tip={tip}
                          icon={<Icon name={on ? "check" : "plus"} size="xs" aria-hidden="true" />}
                          onClick={() => toggle(b.name)}
                        />
                      </li>
                    );
                  })}
                </ul>
                {visibleSuggested.length > 0 &&
                  (() => {
                    const room = Math.max(0, remote.cap - chosen.length);
                    const take = visibleSuggested.slice(0, room);
                    return take.length > 0 ? (
                      <Button
                        type="empty"
                        htmlType="button"
                        onClick={() => setPicked((prev) => new Set([...prev, ...take]))}
                        className="mt-2"
                      >
                        Select the {take.length} newest release branch{take.length === 1 ? "" : "es"} shown
                        {take.length < visibleSuggested.length
                          ? ` (${visibleSuggested.length - take.length} more would exceed your ${remote.cap})`
                          : ""}
                      </Button>
                    ) : null;
                  })()}
                {overCap && (
                  <div role="alert" className="mt-2 text-xs text-danger">
                    {chosen.length} chosen, but only {Math.max(0, remote.cap)} more index
                    {remote.cap === 1 ? "" : "es"} may be added in this org.
                  </div>
                )}
              </fieldset>
            ) : (
              <div className="w-48">
                <TextField id="add-branch" label="Branch" value={branch} onChange={(v) => setBranch(v)} />
                <div className="text-xs text-text-muted mt-1">
                  Or press Find branches to choose from what the remote shows.
                </div>
              </div>
            )}
            {showToken ? (
              <div role={tokenNeeded === "refused" ? "alert" : undefined}>
                {tokenNeeded === "refused" && (
                  <div className="text-xs text-text mb-1.5">
                    The remote will not show <Mono>{cleanRepo}</Mono> without a token.
                  </div>
                )}
                <PasswordField
                  id="add-token"
                  label="Access token"
                  value={token}
                  onChange={(v) => setToken(v)}
                  placeholder="host access token"
                  autoComplete="off"
                  autoFocus={tokenNeeded === "refused"}
                />
                <div className="text-xs text-text-muted mt-1">
                  A fine-grained token with read access to Contents on this repository is enough. Stored encrypted, used
                  only to fetch, never shown again.
                </div>
              </div>
            ) : (
              <div className="text-xs text-text-muted">
                A public repository needs no token; Loom asks the remote first and only asks you for one if it refuses.{" "}
                <Button type="empty" htmlType="button" onClick={() => setTokenNeeded("asked")}>
                  It is private — add a token now
                </Button>
              </div>
            )}
          </fieldset>

          <ChoiceGroup
            label="Who can read the index in Loom"
            value={rule}
            onChange={setRule}
            options={[
              { value: "open", label: "Open", hint: "Anyone in the org joins as a reader at once." },
              {
                value: "restricted",
                label: "Restricted",
                hint: "You approve each reader. Access is per person; the source's own visibility does not change this.",
              },
            ]}
          />

          <Button
            type="empty"
            htmlType="button"
            iconLeft={advanced ? "chevron-down" : "chevron-right"}
            aria-expanded={advanced}
            onClick={() => setAdvanced((v) => !v)}
          >
            Advanced
          </Button>
          {advanced && (
            <div className="grid sm:grid-cols-2 gap-3">
              <div>
                <TextField
                  id="add-prefixes"
                  label="Only these paths"
                  value={prefixes}
                  onChange={(v) => setPrefixes(v)}
                  placeholder="src/, include/"
                />
                <div className="text-xs text-text-muted mt-1">Comma-separated prefixes. Empty = the whole tree.</div>
              </div>
              <div>
                <TextField
                  id="add-exclude"
                  label="Skip these paths"
                  value={exclude}
                  onChange={(v) => setExclude(v)}
                  placeholder="**/generated/**, third_party/**"
                />
                <div className="text-xs text-text-muted mt-1">
                  Globs, comma-separated. Vendored and generated code usually.
                </div>
              </div>
              <div>
                <Checkbox
                  id="add-skip-generated"
                  label="Skip generated files"
                  checked={skipGenerated}
                  onCheckedChange={(x) => setSkipGenerated(x === true)}
                />
                <div className="mt-1 text-xs text-text-muted">
                  Files that say so in their header (<span className="font-mono">Code generated … DO NOT EDIT</span>,{" "}
                  <span className="font-mono">@generated</span>) and the repository's own{" "}
                  <span className="font-mono">.gitattributes</span> rules. Their sources stay indexed.
                </div>
              </div>
              <div>
                <Select
                  id="add-producer"
                  label="Compiler tier"
                  value={producer || DETECT}
                  onSelect={(value) => {
                    const v = oneOf(
                      PRODUCERS.map((o) => o.value),
                      value === DETECT ? "" : value,
                    );
                    if (v !== null) setProducer(v);
                  }}
                >
                  {PRODUCERS.map((o) => (
                    <Select.Item key={o.value || DETECT} value={o.value || DETECT}>
                      {o.label}
                    </Select.Item>
                  ))}
                </Select>
                <div className="text-xs text-text-muted mt-1">
                  Exact cross-file calls on top of the parser tier; detected from go.mod, tsconfig, CMakeLists or
                  pyproject when left to the tree.
                </div>
              </div>
            </div>
          )}

          {error && <Alert role="alert" state="danger" text={error} />}
        </div>

        <ModalFooter>
          <span className="mr-auto text-xs text-text-muted">
            The remote is asked first; nothing is written if it refuses.
          </span>
          <Button type="secondary" htmlType="button" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" iconLeft="plus" loading={busy} disabled={!valid}>
            {tokenNeeded === "refused"
              ? "Add with token"
              : remote && chosen.length > 1
                ? `Add ${chosen.length} branches`
                : "Add and start indexing"}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
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
    <Modal
      open={group !== null}
      onOpenChange={onOpenChange}
      title={`Add a branch of ${group?.repo ?? ""}`}
      description={`Same repository, another branch, its own index. It inherits the join rule (${group?.restricted ? "restricted" : "open"}), the paths and the token; you maintain it.`}
      className="max-w-[440px] w-full"
    >
      {group && src && (
        <form onSubmit={submit}>
          <TextField
            id="add-branch-name"
            label="Branch"
            value={branch}
            onChange={(v) => setBranch(v)}
            placeholder="26.9"
            autoFocus
            {...fieldError("add-branch-name-error", error)}
          />
          <div className="text-xs text-text-muted mt-1">
            Already indexed: {group.branches.map((b) => b.branch).join(", ")}
          </div>
          <ModalFooter>
            <Button type="secondary" htmlType="button" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="primary" htmlType="submit" iconLeft="plus" loading={busy} disabled={!valid}>
              Add branch
            </Button>
          </ModalFooter>
        </form>
      )}
    </Modal>
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
      <Button type="empty" htmlType="button" iconLeft="chevron-left" onClick={onBack} className="mb-3">
        Repositories
      </Button>
      <div className="mb-5">
        <PageHeader title={repo.repo}>
          <Mono className="text-sm px-2 py-0.5 rounded bg-surface-2 text-text-muted">{repo.branch}</Mono>
          <TipTarget tip={f.detail}>
            <Badge text={f.label} state={FRESH_STATE[f.tone]} size="sm" />
          </TipTarget>
          <span className="text-xs text-text-muted">read-only</span>
        </PageHeader>
        <p className="text-sm text-text-muted">
          namespace <Mono>{repo.namespace}</Mono> · answers are as of <Mono>{short(repo.git_head) || "—"}</Mono>
        </p>
        {others.length > 0 && (
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            <span className="text-xs text-text-muted">Branches</span>
            {others.map((b) => (
              <BranchChip
                key={b.namespace}
                b={b}
                busy={actions.busy === b.namespace}
                onOpen={() => onSwitch(b.namespace)}
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

      <Tabs defaultValue="health">
        <Tabs.TriggersList className="mb-5" aria-label={`${repo.repo} @ ${repo.branch}`}>
          <TabTrigger value="health" icon="activity">
            Health
          </TabTrigger>
          <TabTrigger value="explore" icon="search">
            Explore
          </TabTrigger>
          <TabTrigger value="packs" icon="cards">
            PR packs
          </TabTrigger>
          <TabTrigger value="access" icon="secure">
            Access
          </TabTrigger>
        </Tabs.TriggersList>
        {/* Each tab is keyed by branch: another branch starts each one fresh (its status,
            results, open pack and filter), and the tab the reader is on stays selected. */}
        <Tabs.Content value="health">
          <HealthTab key={repo.namespace} id={id} />
        </Tabs.Content>
        <Tabs.Content value="explore">
          <ExploreTab key={repo.namespace} id={id} />
        </Tabs.Content>
        <Tabs.Content value="packs">
          <PacksTab key={repo.namespace} id={id} repo={repo.repo} />
        </Tabs.Content>
        <Tabs.Content value="access">
          <AccessTab
            key={repo.namespace}
            id={id}
            siblings={siblings}
            repo={repo}
            onRetired={() => {
              onJoined();
              onBack();
            }}
          />
        </Tabs.Content>
      </Tabs>
    </PageContainer>
  );
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

// One icon per compiler-tier producer on the Health tab.
const TIER_ICONS: Record<TierIcon, IconName> = {
  cpp: "share-network",
  typescript: "brackets",
  go: "code-in-square",
  python: "brackets",
  none: "share-network",
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
  const lastRun = parseChTime(w.last_run_at);
  if (!lastRun) return { text: "pending", tone: "muted" };
  const age = (now - lastRun.getTime()) / 1000;
  return age <= 2 * w.interval_seconds + w.last_duration_ms / 1000
    ? { text: "current", tone: "ok" }
    : { text: "stale", tone: "warn" };
}

// A commit watermark shows as a short sha, a time watermark to the minute.
export const watermarkText = (w: string) => (/^[0-9a-f]{7,40}$/.test(w) ? short(w) : w.slice(0, 16).replace("T", " "));

function StageCard({
  icon,
  title,
  watermark,
  lag,
  freshness,
  updatedAt,
  workers,
  note,
  tone,
}: {
  icon: IconName;
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
    <div className="p-4 rounded-xl border border-border bg-surface">
      <div className="flex items-center justify-between gap-2">
        <div className="inline-flex items-center gap-2 text-sm font-medium">
          <Icon name={icon} size="sm" aria-hidden="true" /> {title}
        </div>
        <Badge text={l.text} state={FRESH_STATE[l.tone]} size="sm" />
      </div>
      <div className="mt-2 text-xs text-text-muted">
        watermark <Mono className="text-text">{watermarkText(watermark) || "—"}</Mono>
        {updatedAt && <> · advanced {listTime(updatedAt)}</>}
      </div>
      {note && <div className={`mt-1 text-xs ${tone === "warn" ? "text-warn" : "text-text-muted"}`}>{note}</div>}
      {workers.length > 0 && (
        <div className="mt-3 space-y-1">
          {workers.map(([name, w]) => (
            <div key={name} className="flex items-center justify-between text-xs tabular">
              <Mono className="text-xs text-text-muted">{name}</Mono>
              {!w ? (
                <span className="text-text-muted">not on this replica</span>
              ) : !w.enabled ? (
                <span className="text-text-muted">disabled on this replica</span>
              ) : w.consecutive_failures > 0 ? (
                <TipTarget tip={w.last_error}>
                  <span className="text-danger">
                    {w.consecutive_failures}× failing · {w.last_error.slice(0, 60)}
                  </span>
                </TipTarget>
              ) : (
                <span className="text-text-muted">
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

export function HealthTab({ id }: { id: CodeId }) {
  const [st, setSt] = useState<CodeIndexStatus | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showState, setShowState] = useState(false);
  // undefined while the first read is out, null once a read failed.
  const [cov, setCov] = useState<CodeCoverage | null | undefined>(undefined);
  const [recent, setRecent] = useState<CodeRecentCommitRow[] | "failed" | null>(null);
  const [errs, setErrs] = useState<{ rows: CodeIndexErrorRow[]; total: number } | null | undefined>(undefined);
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
          if (live()) setError(e);
        });
      // A failed poll after a good read keeps that read, as the status above does; only a
      // section with nothing read yet says its read failed.
      codeCoverage(id, { signal })
        .then((c) => {
          if (live()) setCov(c);
        })
        .catch(() => {
          if (live()) setCov((c) => c ?? null);
        });
      codeRecentCommits(id, 15, { signal })
        .then((r) => {
          if (live()) setRecent(r.rows);
        })
        .catch(() => {
          // Said as a failure: "no commits recorded yet" would be a claim.
          if (live()) setRecent((r) => (Array.isArray(r) ? r : "failed"));
        });
      codeIndexErrors(id, 100, { signal })
        .then((e) => {
          if (live()) setErrs(e);
        })
        .catch(() => {
          if (live()) setErrs((x) => x ?? null);
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
          if (live()) setSpend((x) => x ?? null);
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

  // A failed first read is the tab's state; a failed poll after one keeps the last status
  // on screen and says it could not refresh.
  if (error && !st)
    return (
      <ListState
        kind="error"
        things="index status"
        error={error}
        onRetry={() => {
          setError(null);
          load();
        }}
      />
    );
  if (!st) return <Loading label="Loading the index health" />;

  const W = st.workers;
  const gitHeadAt = st.state.git_head?.updated_at;
  const ago = (t: string) => (t ? listTime(t) : "—");

  return (
    <div className="space-y-6">
      {!!error && (
        <div className="text-xs text-text-muted" role="status">
          {`Couldn't refresh the index status (${humanizeError(error)}); showing the last read.`}
        </div>
      )}
      <div className="text-xs text-text-muted">
        {st.provider || "git"} · {st.profile || "generic"} profile
        {st.compiler_candidates?.length ? ` · compiler candidates: ${st.compiler_candidates.join(", ")}` : ""}
      </div>
      {!!st.quality_warnings?.length && (
        <Alert
          state="warning"
          text={
            <span className="block space-y-1">
              {st.quality_warnings.map((warning) => (
                <span key={warning} className="block">
                  {warning}
                </span>
              ))}
            </span>
          }
        />
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
            icon="cpu"
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
            icon="git-merge"
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
                  ? `PRs ${cov.pr_history_from.slice(0, 10)} → ${cov.pr_history_to.slice(0, 10)} · last fetch ${cov.pr_history_last_fetch_at ? listTime(cov.pr_history_last_fetch_at) : "pending"}${cov.pr_history_auth === "anonymous" ? " · anonymous GitHub API (60 req/h; set LOOM_CODE_GITHUB_TOKEN for headroom)" : ""}`
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
        {cov === undefined ? (
          <Loading layout="inline" label="Loading coverage" />
        ) : cov === null ? (
          <div className="text-xs text-text-muted">Couldn't load coverage.</div>
        ) : (
          <div className="space-y-2">
            <Table
              size="sm"
              headers={[
                { label: "Language" },
                { label: "Files" },
                { label: "Last file indexed" },
                { label: "Chunks · embedded · described" },
                { label: "Syntax symbols (tree-sitter)" },
                { label: "Compiler symbols (scip)" },
              ]}
              rows={cov.languages
                .filter((l) => l.files > 0 || l.chunks > 0 || l.symbols_scip > 0)
                .map((l) => {
                  const embPct = l.chunks ? Math.round((l.chunks_embedded / l.chunks) * 1000) / 10 : 0;
                  return {
                    id: l.lang || "(other)",
                    items: [
                      {
                        label: l.lang ? (
                          <span className="font-mono">{l.lang}</span>
                        ) : (
                          <span className="text-text-muted">(outside indexed paths)</span>
                        ),
                      },
                      {
                        label: (
                          <>
                            {fmtInt(l.files)}
                            {l.files_errored > 0 && (
                              <span className="ml-1 text-warn">· {l.files_errored} with index errors</span>
                            )}
                          </>
                        ),
                      },
                      {
                        label: l.files_last_at ? (
                          <TipTarget tip={l.files_last_at}>{ago(l.files_last_at)}</TipTarget>
                        ) : (
                          ago(l.files_last_at)
                        ),
                      },
                      {
                        label: (
                          <>
                            {fmtInt(l.chunks)} ·{" "}
                            <span className={embPct < 99.5 && l.chunks > 0 ? "text-warn" : ""}>{embPct}%</span> ·{" "}
                            {fmtInt(l.chunks_described)}
                          </>
                        ),
                      },
                      {
                        label: l.symbols_treesitter ? (
                          <>
                            {fmtInt(l.symbols_treesitter)}{" "}
                            <span className="text-text-muted">
                              ·{" "}
                              {l.symbols_treesitter_last_at ? (
                                <TipTarget tip={l.symbols_treesitter_last_at}>
                                  {ago(l.symbols_treesitter_last_at)}
                                </TipTarget>
                              ) : (
                                ago(l.symbols_treesitter_last_at)
                              )}
                            </span>
                          </>
                        ) : (
                          <span className="text-text-muted">—</span>
                        ),
                      },
                      {
                        label: l.symbols_scip ? (
                          <>
                            {fmtInt(l.symbols_scip)}{" "}
                            <span className="text-text-muted">
                              ·{" "}
                              {l.symbols_scip_last_at ? (
                                <TipTarget tip={l.symbols_scip_last_at}>{ago(l.symbols_scip_last_at)}</TipTarget>
                              ) : (
                                ago(l.symbols_scip_last_at)
                              )}
                            </span>
                          </>
                        ) : (
                          <span className="text-text-muted">not compiler-indexed</span>
                        ),
                      },
                    ],
                  };
                })}
            />
            <div className="text-xs text-text-muted">
              Symbols agents can trust for exact callers/callees are the compiler (scip) column; languages with only
              tree-sitter symbols answer search and definitions but their call graph is heuristic.
              {cov.tiers.map((t) => (
                <span key={t.tier} className="ml-3">
                  {t.tier} edges {fmtInt(t.edges)} · {ago(t.edges_last_at)}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        <div className="p-4 rounded-xl border border-border bg-surface">
          <div className="inline-flex items-center gap-2 text-sm font-medium mb-2">
            <Icon name="history" size="sm" aria-hidden="true" /> Recently ingested commits
          </div>
          {recent === null ? (
            <Loading layout="inline" label="Loading recent commits" />
          ) : recent === "failed" ? (
            <div className="text-xs text-text-muted">Couldn't load the recent commits.</div>
          ) : recent.length === 0 ? (
            <div className="text-xs text-text-muted">No commits recorded yet.</div>
          ) : (
            <div className="space-y-1.5">
              {recent.map((c) => (
                <div key={c.commit} className="flex items-start gap-2 text-xs">
                  <Mono className="text-text-muted shrink-0">{short(c.commit).slice(0, 8)}</Mono>
                  {c.subject ? (
                    <TextClamp text={c.subject} reach="tooltip" className="flex-1 min-w-0" />
                  ) : (
                    <span className="flex-1 text-text-muted">(subject unavailable)</span>
                  )}
                  {c.pr_number > 0 && <span className="text-xs text-text-muted tabular shrink-0">#{c.pr_number}</span>}
                  <span className="text-xs text-text-muted tabular shrink-0">
                    <TipTarget tip={`${c.files} files · committed ${c.committed_at}`}>
                      {c.files}f · {c.ingested_at ? listTime(c.ingested_at) : "—"}
                    </TipTarget>
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="p-4 rounded-xl border border-border bg-surface">
          <button
            type="button"
            onClick={() => setShowErrs((v) => !v)}
            aria-expanded={showErrs}
            className="inline-flex items-center gap-1.5 min-h-9 -ml-1 pl-1 pr-2 w-full rounded text-left text-sm font-medium hover:bg-surface-2 pointer-coarse:min-h-11"
          >
            <Icon
              name={showErrs ? "chevron-down" : "chevron-right"}
              size="sm"
              aria-hidden="true"
              className="shrink-0"
            />
            <Icon
              name="warning"
              size="sm"
              color={errs && errs.total > 0 ? "var(--color-warn)" : undefined}
              aria-hidden="true"
            />
            Files with index errors{" "}
            {errs ? <span className="tabular text-text-muted">({fmtInt(errs.total)})</span> : null}
          </button>
          {errs === undefined && <Loading layout="inline" label="Loading the index errors" className="mt-2" />}
          {errs === null && <div className="mt-2 text-xs text-text-muted">Couldn't load the index errors.</div>}
          {errs && errs.total === 0 && (
            <div className="mt-2 text-xs text-text-muted">
              Every file under the indexed prefixes parsed and chunked.
            </div>
          )}
          {errs && errs.total > 0 && !showErrs && (
            <div className="mt-2 text-xs text-text-muted">
              Search still works for these files' neighbours; the files themselves have no chunks or symbols.
            </div>
          )}
          {errs && showErrs && errs.rows.length > 0 && (
            <div className="mt-2 max-h-64 overflow-auto space-y-1">
              {errs.rows.map((r) => (
                <div key={r.path} className="text-xs flex gap-2">
                  <TextClamp text={r.path} reach="tooltip" mono className="text-xs flex-1 min-w-0" />
                  <TextClamp text={r.error} reach="tooltip" className="text-warn max-w-[45%]" />
                </div>
              ))}
              {errs.total > errs.rows.length && (
                <div className="text-xs text-text-muted">+{errs.total - errs.rows.length} more</div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="p-4 rounded-xl border border-border bg-surface flex items-start justify-between gap-4">
        <div>
          <div className="inline-flex items-center gap-2 text-sm font-medium">
            <Icon name="git-merge" size="sm" aria-hidden="true" /> CI merge hook
          </div>
          <div className="mt-1 text-xs text-text-muted">
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
              <span className="text-warn">
                Never received. The poller keeps the index fresh on its own; the hook only shortens latency after a
                merge.
              </span>
            )}
          </div>
        </div>
        <Button type="primary" htmlType="button" iconLeft="refresh" loading={refreshing} onClick={onRefresh}>
          Refresh now
        </Button>
      </div>

      <div>
        <Button
          type="empty"
          htmlType="button"
          iconLeft={showState ? "chevron-down" : "chevron-right"}
          aria-expanded={showState}
          onClick={() => setShowState((v) => !v)}
        >
          Raw watermark table ({Object.keys(st.state).length} keys)
        </Button>
        {showState && (
          <div className="mt-2">
            <Table
              size="sm"
              headers={[{ label: "Key" }, { label: "Value" }, { label: "Detail" }, { label: "Updated" }]}
              rows={Object.entries(st.state)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([k, v]) => ({
                  id: k,
                  items: [
                    { label: <span className="font-mono">{k}</span> },
                    {
                      label: (
                        <span className="font-mono break-all">
                          <StateValue value={v.value} />
                        </span>
                      ),
                      overflowMode: "wrap",
                    },
                    { label: v.detail || "" },
                    { label: <span className="tabular whitespace-nowrap">{v.updated_at}</span> },
                  ],
                }))}
            />
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
          <Button type="empty" htmlType="button" onClick={() => setFull(false)} className="ml-2">
            Less
          </Button>
        )}
      </>
    );
  }
  return (
    <>
      {value.slice(0, STATE_VALUE_CLAMP)}…
      <Button
        type="empty"
        htmlType="button"
        onClick={() => setFull(true)}
        className="ml-2"
        aria-label={`Show all ${value.length} characters`}
      >
        Show all ({fmtInt(value.length)} chars)
      </Button>
    </>
  );
}

// ---------------------------------------------------------------------------
// Explore
// ---------------------------------------------------------------------------

function UnitCard({ u, onOpen }: { u: CodeUnit; onOpen: (symbol: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="p-3.5 rounded-xl border border-border bg-surface">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {u.symbol ? (
              <Link
                component="button"
                type="button"
                size="md"
                weight="mono"
                onClick={() => onOpen(u.symbol)}
                className="text-left break-all"
              >
                {u.symbol}
              </Link>
            ) : (
              <span className="font-mono text-sm">{u.path.split("/").pop()}</span>
            )}
            <Badge text={u.kind || "unit"} state="neutral" size="sm" />
            {u.tier && <span className="text-xs text-text-muted">{u.tier}</span>}
          </div>
          <div className="mt-0.5 text-xs text-text-muted font-mono break-all">
            {u.path}
            {u.start_line ? `:${u.start_line}-${u.end_line}` : ""}
          </div>
          {u.description && <div className="mt-1.5 text-xs text-text-muted">{u.description}</div>}
        </div>
        <div className="text-right shrink-0 text-xs text-text-muted tabular">
          {u.score > 0 && <div>score {u.score.toFixed(3)}</div>}
          {(u.churn > 0 || u.amp > 0) && (
            <div>
              churn {u.churn} · amp {u.amp}
            </div>
          )}
          {u.tested && <div className="text-success">tested</div>}
        </div>
      </div>
      {u.signature && (
        <pre className="mt-2 text-xs font-mono text-text-muted whitespace-pre-wrap break-all">{u.signature}</pre>
      )}
      {u.body && (
        <>
          <Button
            type="empty"
            htmlType="button"
            iconLeft={open ? "chevron-down" : "chevron-right"}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="mt-2"
          >
            {open ? "Hide" : "Show"} body{u.elided ? " (elided)" : ""}
          </Button>
          {open && (
            <pre className="mt-2 p-3 rounded-lg bg-bg border border-border text-xs font-mono overflow-x-auto max-h-96">
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
        <div className="text-xs text-text-muted">none in the graph</div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {names.slice(0, 60).map((n) => (
            <button
              type="button"
              key={n}
              onClick={() => onOpen(n)}
              className="font-mono text-xs px-1.5 py-0.5 rounded bg-surface-2 hover:bg-surface-3 break-all text-left"
            >
              {n}
            </button>
          ))}
          {names.length > 60 && <span className="text-xs text-text-muted">+{names.length - 60} more</span>}
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
  const [mapBusy, setMapBusy] = useState(false);
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
    setMapBusy(true);
    try {
      setMap((await codeMap(id, { token_budget: 1500 })).map);
    } catch (e) {
      toast(`Map failed: ${humanizeError(e)}`, "error");
    } finally {
      setMapBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run(q);
        }}
        className="flex flex-wrap gap-2 items-end"
      >
        <div className="flex-1 min-w-64">
          <SearchField
            aria-label="Search code"
            value={q}
            onChange={(v) => setQ(v)}
            placeholder="Search code: a question, a symbol name, or a file path…"
          />
        </div>
        <div className="w-56">
          <TextField
            aria-label="Path prefix"
            value={prefix}
            onChange={(v) => setPrefix(v)}
            placeholder="path prefix (optional)"
          />
        </div>
        <BusyButton type="primary" htmlType="submit" iconLeft="search" disabled={!q.trim()} busy={busy}>
          Search
        </BusyButton>
      </form>
      <p className="text-xs text-text-muted">
        Runs the same <span className="font-mono">code.search</span> / <span className="font-mono">code.symbol</span> /{" "}
        <span className="font-mono">code.callers</span> ops agents call. Click a symbol to see its definition and graph.
      </p>

      {error && <div className="text-sm text-danger">{error}</div>}

      {sym && (
        <div className="p-4 rounded-xl border border-accent/40 bg-surface space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div className="font-mono text-sm break-all inline-flex items-center gap-2 flex-wrap">
              {sym.name}
              {sym.ambiguous && <Badge text="Ambiguous" state="warning" size="sm" />}
            </div>
            <Button type="empty" htmlType="button" onClick={() => setSym(null)}>
              Close
            </Button>
          </div>
          {sym.defs.length === 0 && <div className="text-xs text-text-muted">No definition resolved.</div>}
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
      {symBusy && <Loading layout="inline" label="Loading the symbol" />}

      {busy && !res && (
        <LoadingSkeleton label="Loading search results">
          <div className="space-y-2">
            <SkeletonCard />
            <SkeletonCard />
          </div>
        </LoadingSkeleton>
      )}

      {/* A new search keeps the last results on screen, marked busy, until it answers. */}
      {res && (
        <div className="space-y-2" aria-busy={busy || undefined}>
          <div className="flex items-center gap-3 text-xs text-text-muted tabular flex-wrap">
            <span>
              route <Mono>{res.route}</Mono>
            </span>
            <span>confidence {res.retrieval_confidence.toFixed(2)}</span>
            <Badge
              text={`${res.retrieval_risk} risk`}
              state={res.retrieval_risk === "low" ? "success" : res.retrieval_risk === "medium" ? "warning" : "danger"}
              size="sm"
            />
            <span>{res.tokens_returned} tokens</span>
            {res.as_of?.commit && (
              <TipTarget tip={`indexed ${res.as_of.indexed_at ?? "at an unknown time"}`}>
                <span>
                  as of <Mono>{short(res.as_of.commit)}</Mono>
                </span>
              </TipTarget>
            )}
            {res.as_of && typeof res.as_of.lag_commits === "number" && (
              <TipTarget tip="as_of.stale is what an agent gates on before trusting a hit for HEAD-accurate work">
                <Badge
                  text={
                    res.as_of.lag_commits < 0
                      ? "lag unknown"
                      : res.as_of.stale
                        ? `stale · ${res.as_of.lag_commits} behind`
                        : res.as_of.lag_commits === 0
                          ? "at tip"
                          : `${res.as_of.lag_commits} behind`
                  }
                  state={res.as_of.stale ? "warning" : res.as_of.lag_commits === 0 ? "success" : "neutral"}
                  size="sm"
                />
              </TipTarget>
            )}
          </div>
          {res.note && (
            <div className="text-xs text-warn">
              <TipTarget tip="The vector leg covers less than the ready share of this repository's chunks; results come from the lexical leg alone until it does.">
                {res.note}
              </TipTarget>
            </div>
          )}
          {res.abstain && res.abstain_code === "not_covered" && (
            <Alert
              state="danger"
              text={
                <>
                  <strong className="font-medium">Not covered.</strong> {res.abstain_reason} An agent receives{" "}
                  <Mono>abstain_code = not_covered</Mono> here, distinct from a genuine miss.
                </>
              }
            />
          )}
          {res.abstain && res.abstain_code !== "not_covered" && (
            <Alert
              state="warning"
              text={`Abstained: ${res.abstain_reason || "no confident match"}. Results below are best-effort.`}
            />
          )}
          {res.results.length === 0 && (
            <EmptyState
              icon="search"
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
        <div className="p-4 rounded-xl border border-dashed border-border">
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm text-text-muted">
              Not sure where to start? Load the repo orientation map agents get from{" "}
              <span className="font-mono">code.map</span>.
            </div>
            <BusyButton type="secondary" htmlType="button" onClick={loadMap} busy={mapBusy}>
              Show map
            </BusyButton>
          </div>
          {map && (
            <pre className="mt-3 text-xs font-mono whitespace-pre-wrap text-text-muted max-h-[32rem] overflow-auto">
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

export function PacksTab({ id, repo }: { id: CodeId; repo: string }) {
  const [pageSize, setPageSize] = usePageSize("packs");
  const [rows, setRows] = useState<CodeContextPackRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<unknown>(null);
  // Retry reads again through the effect.
  const [reads, setReads] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const [pack, setPack] = useState<{ pr: number; text: string; tokens: number; source: string } | null>(null);
  const [prFilter, setPrFilter] = useState("");
  const packNav = useRowNav({ enabled: (rows?.length ?? 0) > 0 });
  // Settle the typed PR number for 250 ms before asking, like every other search box on
  // the dashboard.
  const [prSettled, setPrSettled] = useState("");
  // The page goes back to the first when the filter settles, in the same render, so one read
  // asks for the new filter's first page.
  useEffect(() => {
    if (prFilter === prSettled) return;
    const t = setTimeout(() => {
      setPrSettled(prFilter);
      setOffset(0);
    }, 250);
    return () => clearTimeout(t);
  }, [prFilter, prSettled]);

  // A new filter or page keeps the rows on screen, marked busy, until it answers; another
  // branch's rows are not this one's, so a new branch starts from the skeleton.
  const [refreshing, setRefreshing] = useState(false);
  // The offset the rows on screen came from, and what they were read for: the pager pages
  // from that offset while it is still what the list shows.
  const [rowsOffset, setRowsOffset] = useState(0);
  const [rowsKey, setRowsKey] = useState("");
  // The PR filter alone: the total holds across a page-size change.
  const [rowsPr, setRowsPr] = useState<string | null>(null);
  // Another branch mounts a fresh tab (RepoDetail keys each tab by branch).
  useAbortableEffect(
    (signal, live) => {
      setRefreshing(true);
      setError(null);
      const pr = parseInt(prSettled, 10);
      listCodeContextPacks(id, { limit: pageSize, offset, pr_number: Number.isFinite(pr) ? pr : 0, signal })
        .then((r) => {
          if (live()) {
            setRows(r.rows);
            setRowsOffset(offset);
            setRowsKey(`${prSettled}\n${pageSize}`);
            setRowsPr(prSettled);
            setTotal(r.total);
            setRefreshing(false);
          }
        })
        .catch((e) => {
          if (live()) {
            setError(e);
            setRefreshing(false);
          }
        });
    },
    [id.org, id.namespace, offset, pageSize, prSettled, reads],
  );
  const rowsCurrent = rowsKey === `${prSettled}\n${pageSize}`;
  const pagerOffset = rowsCurrent ? rowsOffset : offset;

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
      // Fold the row back rather than leave it loading.
      setOpen((o) => (o === pr ? null : o));
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-text-muted max-w-2xl">
          One precomputed briefing per merged PR, built by the fast tier as the merge lands. A review bot fetches it
          with <span className="font-mono">code.context_pack</span> at analysis start instead of exploring cold.
        </p>
        <div className="w-28 shrink-0">
          <TextField
            aria-label="Filter by PR number"
            value={prFilter}
            onChange={(v) => setPrFilter(v.replace(/\D/g, ""))}
            placeholder="PR #"
          />
        </div>
      </div>

      {/* A failed read keeps the filter in view and offers Retry, as every list does. */}
      {error !== null && (
        <ListState kind="error" things="PR packs" error={error} onRetry={() => setReads((n) => n + 1)} />
      )}

      {error === null && rows === null && (
        <LoadingSkeleton label="Loading PR packs">
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        </LoadingSkeleton>
      )}

      {/* Rows expand inline into their pack, so this stays a list of disclosure rows
          rather than a table. The pager moves focus here, and the empty state sits inside,
          so focus stays when a page steps back to an empty first page. */}
      {error === null && rows !== null && (
        <div id="packs-list" tabIndex={-1} aria-busy={refreshing || undefined}>
          {rows.length === 0 && pagerOffset === 0 && rowsCurrent && !refreshing && (
            <EmptyState
              icon="cards"
              title="No packs yet"
              description={
                prSettled
                  ? `No precomputed pack for PR #${prSettled}. Unmerged PRs get an orientation pack on demand.`
                  : "Packs appear as merges are ingested."
              }
            />
          )}
          {rows.length > 0 && (
            <div ref={packNav} className="space-y-2">
              {rows.map((r) => (
                <ListRow key={r.pr_number} active={open === r.pr_number}>
                  {/* The open button stretches over this head only, not the pack under it. */}
                  <div className="relative flex min-w-0 flex-1 items-center gap-3">
                    <button
                      type="button"
                      data-row-open=""
                      onClick={() => void toggle(r.pr_number)}
                      aria-expanded={open === r.pr_number}
                      className="list-row-open min-w-0 flex-1 flex items-center gap-3"
                    >
                      <Icon
                        name="chevron-right"
                        size="sm"
                        aria-hidden="true"
                        className={`shrink-0 ${open === r.pr_number ? "rotate-90" : ""}`}
                      />
                      <span className="font-mono text-xs tabular shrink-0">#{r.pr_number}</span>
                      <span className="text-sm font-semibold truncate flex-1">
                        {r.title || <span className="font-normal text-text-muted">(title not in history yet)</span>}
                      </span>
                      <span className="text-xs text-text-muted tabular shrink-0 hidden md:inline">{r.author}</span>
                      <span className="text-xs text-text-muted tabular shrink-0">{fmtInt(r.pack_tokens)} tokens</span>
                      <ListTime time={r.created_at} quiet className="text-xs text-text-muted shrink-0" />
                    </button>
                    <span className="list-row-control shrink-0">
                      <ExternalLink
                        href={`https://github.com/${repo}/pull/${r.pr_number}`}
                        label={`PR #${r.pr_number} on GitHub`}
                      />
                    </span>
                  </div>
                  {open === r.pr_number && (
                    <div className="mt-2">
                      {!pack || pack.pr !== r.pr_number ? (
                        <Loading layout="inline" label="Loading the pack" className="py-2" />
                      ) : (
                        <>
                          <div className="text-xs text-text-muted py-2 tabular">
                            {pack.source} · {fmtInt(pack.tokens)} tokens · merge{" "}
                            <Mono>{short(r.merge_commit_sha)}</Mono>
                          </div>
                          <pre className="text-xs font-mono whitespace-pre-wrap text-text-muted max-h-[32rem] overflow-auto">
                            {pack.text}
                          </pre>
                        </>
                      )}
                    </div>
                  )}
                </ListRow>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Kept on an empty later page (packs retired meanwhile), so it steps back to the last. */}
      {error === null && rows !== null && (rows.length > 0 || pagerOffset > 0) && (
        <ListPager
          label="PR pack pages"
          page={Math.floor(pagerOffset / pageSize)}
          resetKey={`${id.org}\n${id.namespace}\n${prSettled}`}
          busy={refreshing}
          pageSize={pageSize}
          shown={rowsCurrent ? rows.length : 0}
          // Another filter's total is not this list's; a new page size keeps it.
          total={rowsPr === prSettled ? total : undefined}
          onPage={(n) => setOffset(n * pageSize)}
          onPageSize={(n) => {
            setPageSize(n);
            setOffset(0);
          }}
          topId="packs-list"
        />
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
  { label: string; icon: IconName; tone: Fresh["tone"]; headline: (org: string) => string; detail: string }
> = {
  open: {
    label: "Open",
    icon: "globe",
    tone: "ok",
    headline: (org) => `Anyone in ${org} joins instantly as a reader`,
    detail: "One click on the Repositories page, no waiting. Right for source that is already public.",
  },
  approval: {
    label: "Restricted",
    icon: "lock",
    tone: "warn",
    headline: () => "A maintainer approves each reader",
    detail:
      "People request access from the Repositories page; requests wait here until you approve or decline. Right for source that is not public.",
  },
  closed: {
    label: "Closed",
    icon: "secure",
    tone: "muted",
    headline: () => "Only readers a maintainer adds",
    detail: "Nobody can request; you add readers by name below. Use it for an index that is being rebuilt or retired.",
  },
};
const RULE_COLOR: Record<JoinRule, string> = {
  open: "var(--color-success)",
  approval: "var(--color-warn)",
  closed: "var(--color-text-muted)",
};

function effectiveRule(ns: NamespaceInfo | null | undefined): JoinRule | null {
  if (!ns) return null;
  if (ns.effective_join_policy) return ns.effective_join_policy;
  if (ns.join_policy && ns.join_policy !== "inherit") return ns.join_policy;
  return ns.visibility === "public" ? "open" : "approval";
}

export function AccessTab({
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
      <section className="p-4 rounded-xl border border-border bg-surface" aria-label="How people join">
        {ns === undefined && <Loading layout="inline" label="Loading the join rule" />}
        {ns === null && (
          <div className="text-xs text-warn flex items-start gap-2">
            <Icon name="warning" size="sm" color="var(--color-warn)" aria-hidden="true" className="shrink-0 mt-0.5" />
            No directory row yet — the indexer registers one on its next pass, or you lack access to read it.
          </div>
        )}
        {ns && rule && (
          <>
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div className="min-w-0">
                <div className="text-xs text-text-muted">How people join</div>
                <div className="mt-1 text-lg font-medium inline-flex items-center gap-2 flex-wrap">
                  <Icon name={RULE_META[rule].icon} size="sm" color={RULE_COLOR[rule]} aria-hidden="true" />
                  {RULE_META[rule].headline(id.org)}
                  <Badge text={RULE_META[rule].label} state={FRESH_STATE[RULE_META[rule].tone]} size="sm" />
                  <span className="text-xs font-normal text-text-muted">read-only</span>
                </div>
                <p className="mt-1.5 text-xs text-text-muted max-w-2xl">{RULE_META[rule].detail}</p>
              </div>
              {canManage && (
                <div className="flex flex-col items-end gap-2">
                  {/* A select, not radios: arrow keys in a radio group would ask for one
                      confirmation per press. Cancelling leaves the current rule selected. */}
                  <Select
                    label="Join rule"
                    value={rule}
                    onSelect={(v) => {
                      const next = oneOf(JOIN_RULES, v);
                      if (next) void setRule(next);
                    }}
                    disabled={busy !== null}
                  >
                    {JOIN_RULES.map((r) => (
                      <Select.Item
                        key={r}
                        value={r}
                        label={RULE_META[r].label}
                        description={RULE_META[r].headline(id.org)}
                      />
                    ))}
                  </Select>
                  <Button type="empty" htmlType="button" iconLeft="gear" onClick={() => setSettingsOpen(true)}>
                    Description & more
                  </Button>
                </div>
              )}
            </div>
            {orgReadable && (
              <Alert
                className="mt-3"
                state="warning"
                text={
                  <>
                    This deployment still lets every principal read every repository (
                    <Mono>LOOM_CODE_ORG_READABLE</Mono>), so the rule above is not enforced until an operator turns that
                    off.
                  </>
                }
              />
            )}
          </>
        )}
      </section>

      {error && (
        <EmptyState
          icon="secure"
          title="Readers are visible to readers"
          description={`${error}. Join this repository to see who else reads it.`}
        />
      )}
      {grants === null && !error && <Loading label="Loading readers" />}

      {/* 2. Requests waiting for a maintainer — first, because it is the only
             thing here with a deadline attached to someone else's day. */}
      {grants !== null && waiting.length > 0 && (
        <section
          className="rounded-xl border border-border bg-surface overflow-hidden"
          aria-label="Waiting for approval"
        >
          <div className="flex items-center justify-between gap-3 px-3.5 py-2.5 border-b border-border">
            <div className="text-sm font-medium inline-flex items-center gap-2">
              <Icon name="clock" size="sm" color="var(--color-warn)" aria-hidden="true" /> Waiting for a maintainer{" "}
              <span className="tabular text-text-muted">({waiting.length})</span>
            </div>
            {canManage && waiting.length > 1 && (
              <Button type="empty" htmlType="button" onClick={() => void approveAll()} disabled={busy !== null}>
                Approve all
              </Button>
            )}
          </div>
          {waiting.map((g) => {
            const asked = askedFor(g);
            const alsoOn = siblingWaiting[g.subject] ?? [];
            return (
              <div
                key={g.subject}
                className="flex items-center gap-3 px-3.5 py-2.5 border-b border-border last:border-b-0 text-xs flex-wrap"
              >
                <span className="font-mono flex-1 min-w-48 truncate">{g.subject}</span>
                <span className="text-text-muted">
                  asks to be a <span className="font-medium text-text">{ROLE_WORD[asked]}</span>
                  {g.status !== "pending" && g.role === "viewer" && asked === "admin" ? " (reads today)" : ""}
                </span>
                <span className="text-text-muted tabular hidden md:inline">
                  <ListTime time={g.pending_since} quiet />
                </span>
                {canManage ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {asked === "admin" && (
                      <Tip content="Admit as a reader instead of a maintainer">
                        <Button
                          type="secondary"
                          htmlType="button"
                          onClick={() => void decide(g, "approve", "viewer")}
                          disabled={busy !== null}
                        >
                          Reader instead
                        </Button>
                      </Tip>
                    )}
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="cross"
                      onClick={() => void decide(g, "reject")}
                      disabled={busy !== null}
                    >
                      Decline
                    </Button>
                    {alsoOn.length > 0 && (
                      <Tip content={`Also waiting on ${alsoOn.map((b) => b.branch).join(", ")}`}>
                        <Button
                          type="secondary"
                          htmlType="button"
                          iconLeft="check"
                          onClick={() => void approveEverywhere(g)}
                          disabled={busy !== null}
                        >
                          Approve on all {alsoOn.length + 1} branches
                        </Button>
                      </Tip>
                    )}
                    {/* One primary per surface: a page of requests has none per row. */}
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="check"
                      onClick={() => void decide(g, "approve")}
                      disabled={busy !== null}
                    >
                      Approve as {ROLE_WORD[asked]}
                    </Button>
                  </div>
                ) : (
                  <span className="text-text-muted">waiting</span>
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
            <Alert
              state="warning"
              text="Registered by the indexer, no maintainer recorded yet. An operator grants the first maintainer; after that the maintainer sets the rule above and approves readers."
            />
          )}
          {active.length === 0 ? (
            <div className="text-xs text-text-muted">
              Nobody reads this repository yet.{" "}
              {rule === "open"
                ? "People join from the Repositories page."
                : rule === "approval"
                  ? "People request access from the Repositories page and appear above."
                  : "Add a reader below."}
            </div>
          ) : (
            <Table
              aria-label="Readers"
              size="sm"
              headers={[
                { label: "Reader" },
                { label: "Role", width: "200px" },
                { label: "Added", width: "220px" },
                ...(canManage ? [{ label: <span className="sr-only">Actions</span>, width: "56px" }] : []),
              ]}
              rows={active.map((g) => ({
                id: g.subject,
                items: [
                  {
                    label: (
                      <span className="inline-flex min-w-0 items-center gap-2 font-mono">
                        <Icon
                          name={g.role === "admin" ? "secure" : "database"}
                          size="xs"
                          aria-hidden="true"
                          className="shrink-0"
                        />
                        <span className="truncate">{g.subject}</span>
                        {g.subject === me && <span className="font-sans text-text-muted">(you)</span>}
                      </span>
                    ),
                  },
                  {
                    label: (
                      <Badge
                        text={`${roleWord(g.role)}${g.pending_role ? " · asked to maintain" : ""}`}
                        state="neutral"
                        size="sm"
                      />
                    ),
                  },
                  {
                    label: (
                      <span className="text-text-muted">
                        <ListTime time={g.granted_at} quiet /> by {g.granted_by}
                      </span>
                    ),
                  },
                  ...(canManage
                    ? [
                        {
                          label:
                            g.subject !== me ? (
                              <IconButton
                                icon="trash"
                                size="sm"
                                label={`Remove ${g.subject}`}
                                tooltip="Remove reader"
                                onClick={() => void onRevoke(g)}
                              />
                            ) : null,
                        },
                      ]
                    : []),
                ],
              }))}
            />
          )}

          {canManage && hasAdmin && (
            <form onSubmit={onAdd} className="flex flex-wrap gap-2 items-end" aria-label="Add a reader">
              <div className="flex-1 min-w-64">
                <TextField
                  value={subject}
                  onChange={(v) => setSubject(v)}
                  placeholder="login handle (sign-in namespace), verified email or service-account subject"
                  aria-label="Subject to add"
                />
              </div>
              <div className="w-full sm:w-80">
                <Select
                  value={role}
                  triggerProps={{ "aria-label": "Role" }}
                  onSelect={(value) => {
                    const v = oneOf(
                      REPO_ROLES.map((o) => o.value),
                      value,
                    );
                    if (v !== null) setRole(v);
                  }}
                >
                  {REPO_ROLES.map((o) => (
                    <Select.Item key={o.value} value={o.value}>
                      {o.label}
                    </Select.Item>
                  ))}
                </Select>
              </div>
              <Button type="primary" htmlType="submit" iconLeft="plus" disabled={busy !== null || !subject.trim()}>
                Add
              </Button>
              <span className="text-xs text-text-muted basis-full">
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
          className="rounded-xl border border-border p-4 flex items-start justify-between gap-4 flex-wrap"
          aria-label="Retire"
        >
          <div className="min-w-0">
            <div className="text-sm font-medium">Retire this index</div>
            <p className="mt-1 text-xs text-text-muted max-w-xl">
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
          <Tip
            content={
              repoRow.source === "registry"
                ? "Irreversible; a confirmation follows"
                : "Managed by the deployment config"
            }
          >
            <Button
              type="danger"
              htmlType="button"
              iconLeft="trash"
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
            >
              Retire…
            </Button>
          </Tip>
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
