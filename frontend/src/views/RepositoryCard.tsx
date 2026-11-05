/**
 * RepositoryCard — a repository is one list row with two facets: its code and its record.
 *
 * The row is folded by default:
 *
 * - line 1: `owner/repo` (the disclosure button), one health badge for the worst branch the
 *   person can see (its detail in a tooltip), and `Restricted` when it is;
 * - line 2, muted: "3 branches · 31,539 files · indexed 2h ago · you read 2 of 3";
 * - right, or under line 2 below 640 px: the one inline action, a Click UI split button —
 *   Open (its menu: each branch you read), Join or Request access (its menu: each branch
 *   you could add, Join all, Choose branches…, Request maintainer access), or a `Requested`
 *   badge whose menu withdraws the request.
 *
 * Opened, it shows a Click UI table of the branches (health, lag, last indexed, files,
 * symbols, your access, and a menu per branch: Open, Refresh, Leave, Join, Withdraw, a
 * maintainer's Remove branch) with Add branch at its foot, the tracker line (the tracker
 * scopes whose `repos` name the repository, and a maintainer's links), a folded Settings
 * for maintainers (access, join policy, the details), then the docs and the document
 * sources they are pulled from.
 *
 * Five words on both facets: Open · Join · Request access · Withdraw request · Leave. A
 * branch name is never in a button's label; it is in a menu item or the table.
 */
import { memo, useId, useState, type ReactNode } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import { SplitButton } from "../components/ui/SplitButton";
import { Table, type TableRowType } from "@clickhouse/click-ui/Table";
import type { CodeRepoSummary } from "../api";
import { absoluteTime, fmtInt, listTime, parseChTime } from "../lib/format";
import { stageLabel } from "../lib/codeStage";
import { freshness, type FreshTone } from "../lib/codeFreshness";
import {
  bindableRecords,
  canAdminister,
  freshnessOf,
  type BindableRecord,
  type TrackerNamespaceState,
  type TrackerSource,
} from "../lib/trackerSources";
import { Chip, ListRow, MetaList, MetaRow, TipTarget } from "../components/ui";
import { ListTime } from "../components/ui/ListTime";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import {
  branchState,
  canBeApproved,
  maintainsOutOfScope,
  NO_APPROVER,
  OUT_OF_SCOPE_MAINTAINER,
  primaryAction,
  RECORD_NOUN,
  verbForState,
  type CodeFacet,
  type RecordFacet,
  type RecordNamespace,
  type RepositoryCard as CardModel,
} from "../lib/repositoryCard";
import { RecordSourceDetails } from "./TrackerSources";
import { RepoDocsList } from "./RepoDocs";
import { RepoSourcesList } from "./RepoSources";
import { oneOf } from "../lib/oneOf";
import { canCallOn } from "../lib/canCall";
import { useStore } from "../lib/state";

/** The reader-grant sentence, said once for the whole card: a token reads through its
 *  subject's — or, for an `sa:` token, its creator's — reader grant, so nothing has to
 *  name the repository. */
export const RESTRICTED_SENTENCE =
  "Restricted repository. Request access asks its maintainers; you read nothing until one approves. One approval covers you as a person: every token minted for you, and the service accounts you create, read it without naming it.";

export const FRESH_STATE: Record<FreshTone, BadgeState> = {
  ok: "success",
  warn: "warning",
  bad: "danger",
  muted: "neutral",
};

// ---------------------------------------------------------------------------
// Health: one vocabulary for a branch's index
// ---------------------------------------------------------------------------

export interface Health {
  text: string;
  state: BadgeState;
  /** What the tooltip says: the detail, the heads and the lag per tier. */
  detail: string;
  /** Higher is worse: the row shows its worst branch. */
  rank: number;
}

const BUILDING = new Set(["queued", "cloning", "indexing", "embedding"]);
const DAY_MS = 24 * 3600_000;
const short = (sha: string) => sha.slice(0, 12);

function heads(b: CodeRepoSummary): string {
  const fetched = b.clone_head && b.clone_head !== b.git_head ? `, fetched ${short(b.clone_head)}` : "";
  return `Indexed ${short(b.git_head)}${fetched}; fast tier ${b.fast_tier_lag} behind, compiler tier ${b.scip_lag} behind.`;
}

/** A branch's index health, worst cause first. */
export function branchHealth(b: CodeRepoSummary): Health {
  const stage = b.stage || "";
  const building = stageLabel(b);
  if (stage === "error")
    return { text: "Index failed", state: "danger", rank: 6, detail: building?.title || "The last index failed." };
  if (BUILDING.has(stage))
    return { text: "Indexing", state: "info", rank: 1, detail: `${building?.label}. ${building?.title}` };
  if (!b.git_head || !b.indexed_at)
    return { text: "Not indexed", state: "neutral", rank: 3, detail: "Nothing is indexed on this branch yet." };
  if (b.searchable === false)
    return { text: "Not searchable", state: "neutral", rank: 2, detail: building?.title ?? "Not searchable yet." };
  const behind = b.fast_tier_lag > 0 ? b.fast_tier_lag : b.scip_lag;
  if (behind > 0)
    return {
      text: `Behind ${fmtInt(behind)} commit${behind === 1 ? "" : "s"}`,
      state: "warning",
      rank: 5,
      detail: `${freshness(b).detail}. ${heads(b)}`,
    };
  const at = parseChTime(b.indexed_at);
  if (at && Date.now() - at.getTime() > DAY_MS && b.clone_head && b.clone_head !== b.git_head)
    return {
      text: "Stale",
      state: "warning",
      rank: 4,
      detail: `Indexed ${listTime(b.indexed_at)} and the remote has moved since. ${heads(b)}`,
    };
  return {
    text: "Up to date",
    state: "success",
    rank: 0,
    detail: `The index matches the fetched branch tip. ${heads(b)}`,
  };
}

/** The worst health among the branches, its detail naming the branch. */
export function worstHealth(branches: readonly CodeRepoSummary[]): Health | null {
  let worst: { b: CodeRepoSummary; h: Health } | null = null;
  for (const b of branches) {
    const h = branchHealth(b);
    if (!worst || h.rank > worst.h.rank) worst = { b, h };
  }
  return worst && { ...worst.h, detail: `${worst.b.branch}: ${worst.h.detail}` };
}

function HealthBadge({ health }: { health: Health }) {
  return (
    // Lifted over the row's stretched disclosure, so a hover reaches the tooltip.
    <TipTarget tip={health.detail} className="list-row-control inline-flex rounded">
      <Badge text={health.text} state={health.state} size="sm" />
    </TipTarget>
  );
}

/** A status badge whose tooltip carries the detail; the wrapper takes focus so keyboard
 *  users reach the tip. */
function TipBadge({ text, state, tip }: { text: string; state: BadgeState; tip: string }) {
  return (
    <TipTarget tip={tip}>
      <Badge text={text} state={state} size="sm" />
    </TipTarget>
  );
}

// ---------------------------------------------------------------------------
// Chips. One shape for a branch (the repository detail's header) and for a record
// scope: the `Chip` primitive as a button, the standing word after the name, the verb in
// the tooltip and the accessible name.
// ---------------------------------------------------------------------------

const STATE_META: Record<TrackerNamespaceState, { word: string; icon: IconName }> = {
  member: { word: "reading", icon: "check" },
  pending: { word: "requested", icon: "clock" },
  open: { word: "", icon: "plus" },
  approval: { word: "", icon: "lock" },
  closed: { word: "closed", icon: "lock" },
  registering: { word: "registering…", icon: "sand-glass" },
};

const HINT: Record<TrackerNamespaceState, string> = {
  member: "You read this — Open it",
  pending: "Waiting for a maintainer — Withdraw request",
  open: "Not joined — Join (open: instant, read-only)",
  approval: "Not joined — Request access (a maintainer approves)",
  closed: "Not joinable — a maintainer adds readers directly",
  registering: "Being registered by the server; Join becomes available once its row is written",
};

/** The accessible name's standing part: "reading" · "requested" · "not joined, join" · … */
function standingWord(st: TrackerNamespaceState): string {
  const verb = verbForState(st);
  if (st === "member" || st === "pending") return STATE_META[st].word;
  if (verb) return `not joined, ${verb.toLowerCase()}`;
  return STATE_META[st].word || st;
}

function StandingChip({
  name,
  mono = false,
  state: st,
  disabled,
  disabledReason,
  onClick,
  extraTip,
}: {
  name: string;
  mono?: boolean;
  state: TrackerNamespaceState;
  disabled: boolean;
  disabledReason?: string | null;
  onClick?: () => void;
  extraTip?: string;
}) {
  const meta = STATE_META[st];
  const label = meta.word ? `${name} · ${meta.word}` : name;
  const tip = disabledReason ?? `${HINT[st]}${extraTip ? ` · ${extraTip}` : ""}`;
  return (
    <Chip
      label={label}
      mono={mono}
      icon={<Icon name={meta.icon} size="xs" aria-hidden="true" />}
      onClick={onClick}
      disabled={disabled || !onClick}
      ariaLabel={`${name}: ${disabledReason ? disabledReason : standingWord(st)}`}
      tip={tip}
      className={st === "closed" || st === "registering" ? "text-text-muted" : ""}
    />
  );
}

/** A branch chip (the repository detail's branch switcher): member → Open, open/approval
 *  → the request dialog, pending → withdraw. A chip never joins directly. */
export function BranchChip({
  b,
  busy,
  onOpen,
  onRequest,
  onCancel,
}: {
  b: CodeRepoSummary;
  busy: boolean;
  onOpen: () => void;
  onRequest: () => void;
  onCancel: () => void;
}) {
  const st = branchState(b);
  const onClick =
    st === "member" ? onOpen : st === "open" || st === "approval" ? onRequest : st === "pending" ? onCancel : undefined;
  return (
    <StandingChip
      name={b.branch}
      mono
      state={st}
      disabled={busy}
      onClick={onClick}
      extraTip={`${b.repo} @ ${b.branch}`}
    />
  );
}

/** A record chip: exactly the BranchChip rule (a chip never mutates) — member → Open,
 *  open/approval → `onRequest`, which the row wires to a confirmation that says
 *  instant-vs-pending before anything is sent, pending → withdraw. */
function RecordChip({
  r,
  busy,
  onOpen,
  onRequest,
  onCancel,
}: {
  r: RecordNamespace;
  busy: boolean;
  onOpen: () => void;
  onRequest: () => void;
  onCancel: () => void;
}) {
  const st = r.state;
  const approvable = st === "approval" ? canBeApproved(r.ns) : null;
  const onClick =
    st === "member" ? onOpen : st === "open" || st === "approval" ? onRequest : st === "pending" ? onCancel : undefined;
  const name = r.scope === "private" ? `${r.label} (restricted)` : r.label;
  return (
    <StandingChip
      name={name}
      state={st}
      disabled={busy || approvable === false}
      disabledReason={approvable === false ? NO_APPROVER : null}
      onClick={onClick}
      extraTip={r.ns.repos.join(", ")}
    />
  );
}

// ---------------------------------------------------------------------------
// Facet rows
// ---------------------------------------------------------------------------

function FacetRow({ icon, label, children }: { icon: IconName; label: string; children: ReactNode }) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a named group of read-only details of the card, not form controls
    <div
      className="grid grid-cols-1 gap-x-4 gap-y-1 border-t border-border pt-3 sm:grid-cols-[6.5rem_minmax(0,1fr)]"
      role="group"
      aria-label={label}
    >
      <div className="section-label inline-flex items-center gap-1.5 self-start text-text-muted">
        <Icon name={icon} size="xs" aria-hidden="true" /> {label}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export interface CodeActions {
  onOpen: (b: CodeRepoSummary) => void;
  /** The direct join of an open branch (the row's Join, a menu's "Join <branch>"). */
  onJoin: (b: CodeRepoSummary) => void;
  /** The request dialog (role choice Reader / Maintainer, instant-vs-pending said before the click). */
  onRequest: (b: CodeRepoSummary, role?: "viewer" | "admin") => void;
  onCancel: (b: CodeRepoSummary) => void;
  onLeave: (b: CodeRepoSummary) => void;
  onJoinAll: (bs: CodeRepoSummary[]) => void;
  onLeaveAll: (bs: CodeRepoSummary[]) => void;
  onManage: (facet: CodeFacet) => void;
  /** Absent where the deployment cannot add branches from the page (no sandbox). */
  onAddBranch?: (facet: CodeFacet) => void;
  /** An ingest pass on a branch the person reads (`code.refresh`). */
  onRefresh?: (b: CodeRepoSummary) => void;
  /** A maintainer retires a branch added from the dashboard (`code.repo.remove`). */
  onRemove?: (b: CodeRepoSummary) => void;
  /** A maintainer sets the repository's access (every branch it maintains). */
  onSetAccess?: (
    facet: CodeFacet,
    classification: "standard" | "restricted",
    join_policy: "open" | "approval" | "closed",
    opening: boolean,
  ) => void;
  /** The repository's trackers in order, primary first ([] unlinks all). */
  onSetSource?: (facet: CodeFacet, tracker_namespaces: string[]) => void;
  /** The registered records the binding picks from, and the register dialog (absent when
   *  the deployment turned self-service off). */
  trackerSources?: TrackerSource[];
  onRegisterSource?: (facet: CodeFacet) => void;
}

export interface RecordActionProps {
  onOpen: (r: RecordNamespace) => void;
  onJoin: (r: RecordNamespace, confirm: boolean) => void;
  onRequest: (r: RecordNamespace) => void;
  onCancel: (r: RecordNamespace) => void;
  onLeave: (r: RecordNamespace) => void;
  /** The org the admin verbs act in, and the re-read after one. */
  org?: string;
  onSourcesChanged?: () => void;
}

// ---------------------------------------------------------------------------
// The branch table
// ---------------------------------------------------------------------------

/** Cells the phone layout leaves out: there a branch stacks as Branch · Health · Last indexed. */
const WIDE_ONLY = "[@media(max-width:768px)]:hidden";
const BRANCH_HEADERS = [
  { label: "Branch" },
  { label: "Health", width: "190px" },
  { label: "Behind", width: "84px" },
  { label: "Last indexed", width: "112px" },
  { label: "Files", width: "96px" },
  { label: "Symbols", width: "104px" },
  { label: "Access", width: "104px" },
  { label: <span className="sr-only">Actions</span>, width: "52px" },
];
const ACCESS_WORD: Partial<Record<TrackerNamespaceState, string>> = { member: "Reading", pending: "Requested" };

// While a write is out, a second one waits for it: every item is disabled.
function branchMenu(b: CodeRepoSummary, actions: CodeActions, busy: boolean): RowMenuItem[] {
  const st = branchState(b);
  const items: RowMenuItem[] = [];
  if (st === "member") {
    items.push({ label: "Open", icon: "arrow-right", onSelect: () => actions.onOpen(b) });
    const refresh = actions.onRefresh;
    if (refresh) items.push({ label: "Refresh", icon: "refresh", onSelect: () => refresh(b) });
    items.push({ label: "Leave", icon: "cross", onSelect: () => actions.onLeave(b) });
  } else if (st === "open") items.push({ label: "Join", icon: "users", onSelect: () => actions.onJoin(b) });
  else if (st === "approval")
    items.push({ label: "Request access", icon: "lock", onSelect: () => actions.onRequest(b, "viewer") });
  else if (st === "pending")
    items.push({ label: "Withdraw request", icon: "clock", onSelect: () => actions.onCancel(b) });
  const remove = actions.onRemove;
  if (remove && b.my_role === "admin" && b.source === "registry")
    items.push({ label: "Remove branch", icon: "trash", danger: true, onSelect: () => remove(b) });
  return items.map((item) => ({ ...item, disabled: busy }));
}

function branchRow(b: CodeRepoSummary, actions: CodeActions, busy: boolean): TableRowType {
  const behind = b.fast_tier_lag > 0 ? b.fast_tier_lag : b.scip_lag;
  const counts = b.counts?.code_files ? b.counts : null;
  const items = branchMenu(b, actions, busy);
  return {
    id: b.namespace,
    items: [
      { label: <span className="font-mono">{b.branch}</span> },
      { label: <HealthBadge health={branchHealth(b)} /> },
      { label: <span className="tabular">{behind > 0 ? fmtInt(behind) : "—"}</span>, className: WIDE_ONLY },
      { label: b.indexed_at ? <ListTime time={b.indexed_at} quiet /> : "—" },
      { label: <span className="tabular">{counts ? fmtInt(counts.code_files) : "—"}</span>, className: WIDE_ONLY },
      { label: <span className="tabular">{counts ? fmtInt(counts.code_symbols) : "—"}</span>, className: WIDE_ONLY },
      { label: ACCESS_WORD[branchState(b)] ?? "—", className: WIDE_ONLY },
      {
        label: items.length > 0 ? <RowMenu name={`${b.repo} @ ${b.branch}`} items={items} /> : null,
        className: "[@media(max-width:768px)]:ml-auto",
      },
    ],
  };
}

function BranchTable({ facet, busy, actions }: { facet: CodeFacet; busy: boolean; actions: CodeActions }) {
  const add = facet.maintained ? actions.onAddBranch : undefined;
  return (
    <div className="repo-branches" aria-busy={busy || undefined}>
      <Table
        aria-label={`${facet.repo} branches`}
        size="sm"
        headers={BRANCH_HEADERS}
        rows={facet.branches.map((b) => branchRow(b, actions, busy))}
      />
      {add && (
        <Button
          type="empty"
          htmlType="button"
          iconLeft="plus"
          onClick={() => add(facet)}
          disabled={busy}
          className="mt-1"
        >
          Add branch
        </Button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The tracker line
// ---------------------------------------------------------------------------

function RecordFacetLine({
  facet: r,
  busy,
  actions,
}: {
  facet: RecordFacet;
  busy: boolean;
  actions: RecordActionProps;
}) {
  const f = r.freshness;
  const unapprovable = r.namespaces.some((n) => n.state === "approval" && canBeApproved(n.ns) === false);
  return (
    <>
      <div className="text-xs text-text-muted tabular-nums flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        {f ? (
          <>
            {r.sources.length > 0 && <span className="text-text">{r.sources.map((x) => x.name).join(", ")}</span>}
            {r.sources.length > 0 && <span aria-hidden="true">·</span>}
            <span>
              {fmtInt(f.issues)} issues · {fmtInt(f.prs)} PRs
            </span>
            {f.last_refresh_at && (
              <TipTarget tip={`Last refresh ${absoluteTime(f.last_refresh_at)}`} underline>
                · refreshed {listTime(f.last_refresh_at)}
              </TipTarget>
            )}
          </>
        ) : r.unconfigured ? (
          <TipBadge
            text="No credential"
            state="neutral"
            tip="The tracker is declared but has no credential yet — nothing can be read from it until one is added."
          />
        ) : (
          <span>read live · counts not available right now</span>
        )}
      </div>
      <ul className="mt-2 flex flex-wrap items-center gap-2 list-none p-0 m-0" aria-label="Tracker scopes">
        {r.namespaces.map((n) => (
          <li key={n.ns.namespace} className="inline-flex max-w-full">
            <RecordChip
              r={n}
              busy={busy}
              onOpen={() => actions.onOpen(n)}
              // open → the confirm-first join (`confirm: true`); approval → the request confirmation.
              onRequest={() => (n.state === "open" ? actions.onJoin(n, true) : actions.onRequest(n))}
              onCancel={() => actions.onCancel(n)}
            />
          </li>
        ))}
      </ul>
      {unapprovable && (
        <p className="mt-1 text-xs text-text-muted">{NO_APPROVER[0].toUpperCase() + NO_APPROVER.slice(1)}.</p>
      )}
      {r.joined.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5">
          {r.joined.map((n) => (
            <Button
              key={n.ns.namespace}
              type="empty"
              htmlType="button"
              onClick={() => actions.onLeave(n)}
              disabled={busy}
            >
              Leave · {r.joined.length > 1 ? n.label : RECORD_NOUN}
            </Button>
          ))}
        </div>
      )}
    </>
  );
}

function TrackerLine({
  card,
  busy,
  code,
  record,
  trackerNote,
}: {
  card: CardModel;
  busy: boolean;
  code: CodeActions;
  record: RecordActionProps;
  trackerNote?: string;
}) {
  const linkable = !trackerNote && !!code.onSetSource && !!card.code?.branches.some((b) => b.my_role === "admin");
  return (
    <FacetRow icon="database" label="Tracker">
      {card.record ? (
        <RecordFacetLine facet={card.record} busy={busy} actions={record} />
      ) : trackerNote ? (
        <p className="text-xs text-text-muted">{trackerNote}</p>
      ) : (
        !linkable && <p className="text-xs text-text-muted">Not linked</p>
      )}
      {linkable && card.code && <TrackerLinkRow card={card} facet={card.code} busy={busy} actions={code} />}
    </FacetRow>
  );
}

// ---------------------------------------------------------------------------
// Settings (maintainers only)
// ---------------------------------------------------------------------------

function Settings({
  card,
  busy,
  code,
  adminOpsPresent,
}: {
  card: CardModel;
  busy: boolean;
  code: CodeActions;
  adminOpsPresent: boolean;
}) {
  const access = card.code && code.onSetAccess && card.code.branches.some((b) => b.my_role === "admin");
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  return (
    <div className="border-t border-border pt-2">
      <Button
        type="empty"
        htmlType="button"
        iconLeft={open ? "chevron-down" : "chevron-right"}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((v) => !v)}
      >
        Settings
      </Button>
      <div id={bodyId} hidden={!open} className="mt-2 space-y-3">
        {access && card.code && <RepoAccessPanel card={card} facet={card.code} busy={busy} actions={code} />}
        {card.code && (
          // biome-ignore lint/a11y/useSemanticElements: a named group of read-only details of the card, not form controls
          <div role="group" aria-label="Code details">
            <MetaList>
              {card.code.branches.map((b) => (
                <MetaRow key={b.namespace} label={b.branch} mono>
                  {b.namespace}
                  {b.git_head ? ` · ${short(b.git_head)}` : ""}
                </MetaRow>
              ))}
              <MetaRow label="Origin">
                {card.code.head.source === "registry" ? "added from the dashboard" : "configured by the operator"}
              </MetaRow>
            </MetaList>
          </div>
        )}
        {card.record?.sources.map((s) => (
          <RecordSourceDetails key={s.name} source={s} manageable={canAdminister(s, adminOpsPresent)} />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The row
// ---------------------------------------------------------------------------

/** Line 2: "3 branches · 31,539 files · indexed 2h ago · you read 2 of 3"; a record-only
 *  row: "22,579 issues · 65,328 PRs · not joined". */
export function rowSummary(card: Pick<CardModel, "code" | "record">): string {
  const parts: string[] = [];
  const c = card.code;
  if (c) {
    const n = c.branches.length;
    parts.push(`${n} branch${n === 1 ? "" : "es"}`);
    const files = c.head.counts?.code_files;
    if (files) parts.push(`${fmtInt(files)} files`);
    if (c.head.indexed_at && c.head.git_head) parts.push(`indexed ${listTime(c.head.indexed_at)}`);
    if (c.joinedCount > 0) parts.push(`you read ${c.joinedCount} of ${n}`);
    else if (c.pendingCount > 0) parts.push(`${c.pendingCount} branch${c.pendingCount === 1 ? "" : "es"} requested`);
    else parts.push("not joined");
  } else if (card.record) {
    const r = card.record;
    if (r.freshness) parts.push(`${fmtInt(r.freshness.issues)} issues`, `${fmtInt(r.freshness.prs)} PRs`);
    else if (r.unconfigured) parts.push(`${RECORD_NOUN}: no credential`);
    parts.push(r.joined.length > 0 ? "reading" : r.pending.length > 0 ? "requested" : "not joined");
  }
  return parts.join(" · ");
}

/** "the public tracker", or the namespace when two scopes share a label. */
function recordName(r: RecordNamespace): string {
  return r.label === "Public" || r.label === "Private" ? `the ${r.label.toLowerCase()} ${RECORD_NOUN}` : r.label;
}

interface MenuEntry {
  label: string;
  onSelect: () => void;
}

/** The split button's menu: what can be done to each branch (or tracker scope), then the
 *  repository's own verbs. */
function actionMenu(card: CardModel, code: CodeActions, record: RecordActionProps): MenuEntry[][] {
  const each: MenuEntry[] = [];
  const all: MenuEntry[] = [];
  const c = card.code;
  if (c) {
    for (const b of c.branches) {
      const st = branchState(b);
      if (st === "member") each.push({ label: `Open ${b.branch}`, onSelect: () => code.onOpen(b) });
      else if (st === "open") each.push({ label: `Join ${b.branch}`, onSelect: () => code.onJoin(b) });
      else if (st === "approval")
        each.push({ label: `Request access to ${b.branch}`, onSelect: () => code.onRequest(b, "viewer") });
    }
    const openable = c.joinable.filter((b) => branchState(b) === "open").length;
    if (c.joinable.length > 1)
      all.push({
        label: `${openable === 0 ? "Request access to" : "Join"} all ${c.joinable.length} branches`,
        onSelect: () => code.onJoinAll(c.branches),
      });
    if (c.joined.length > 1)
      all.push({ label: `Leave all ${c.joined.length} branches`, onSelect: () => code.onLeaveAll(c.branches) });
    if (c.branches.length > 1) all.push({ label: "Choose branches…", onSelect: () => code.onManage(c) });
    if (!c.maintained && c.pendingCount === 0 && branchState(c.primary) !== "closed")
      all.push({ label: "Request maintainer access", onSelect: () => code.onRequest(c.primary, "admin") });
  } else if (card.record) {
    for (const n of card.record.namespaces) {
      if (n.state === "member") each.push({ label: `Open ${recordName(n)}`, onSelect: () => record.onOpen(n) });
      else if (n.state === "open")
        each.push({ label: `Join ${recordName(n)}`, onSelect: () => record.onJoin(n, false) });
      else if (n.state === "approval")
        each.push({ label: `Request access to ${recordName(n)}`, onSelect: () => record.onRequest(n) });
    }
  }
  return [each, all].filter((g) => g.length > 0);
}

/** The withdraw menu behind a `Requested` badge. */
function withdrawMenu(card: CardModel, code: CodeActions, record: RecordActionProps, busy: boolean): RowMenuItem[] {
  const branches = card.code?.pending ?? [];
  const scopes = card.record?.pending ?? [];
  const one = branches.length + scopes.length === 1;
  return [
    ...branches.map((b) => ({
      label: one ? "Withdraw request" : `Withdraw request for ${b.branch}`,
      icon: "clock" as const,
      onSelect: () => code.onCancel(b),
      disabled: busy,
    })),
    ...scopes.map((n) => ({
      label: one ? "Withdraw request" : `Withdraw request for ${recordName(n)}`,
      icon: "clock" as const,
      onSelect: () => record.onCancel(n),
      disabled: busy,
    })),
  ];
}

function RowAction({
  card,
  busy,
  code,
  record,
}: {
  card: CardModel;
  busy: boolean;
  code: CodeActions;
  record: RecordActionProps;
}) {
  const primary = primaryAction(card);
  if (!primary) return null;
  if (primary.verb === "Withdraw request") {
    return (
      <span className="inline-flex items-center gap-1">
        <Badge text="Requested" state="info" size="sm" />
        <RowMenu name={card.repo} items={withdrawMenu(card, code, record, busy)} />
      </span>
    );
  }
  const fire = () => {
    const { branch: b, record: r } = primary;
    if (b) {
      if (primary.verb === "Open") code.onOpen(b);
      else if (primary.verb === "Join") code.onJoin(b);
      else code.onRequest(b, "viewer");
    } else if (r) {
      if (primary.verb === "Open") record.onOpen(r);
      else if (primary.verb === "Join") record.onJoin(r, false);
      else record.onRequest(r);
    }
  };
  const groups = actionMenu(card, code, record);
  const items = (g: MenuEntry[]) => g.map((m) => ({ label: m.label, onClick: m.onSelect }));
  return (
    <span className="repo-action">
      <SplitButton
        menuLabel={`More actions for ${card.repo}`}
        htmlType="button"
        disabled={busy}
        onClick={fire}
        menu={
          groups.length > 1 ? groups.map((g) => ({ type: "group" as const, items: items(g) })) : items(groups[0] ?? [])
        }
      >
        {primary.verb}
      </SplitButton>
    </span>
  );
}

export const RepositoryCard = memo(function RepositoryCard({
  card,
  busy,
  code,
  record,
  adminOpsPresent = false,
  defaultOpen = false,
  trackerNote,
}: {
  card: CardModel;
  busy: string | null;
  code: CodeActions;
  record: RecordActionProps;
  adminOpsPresent?: boolean;
  /** A row is folded until the person opens it. */
  defaultOpen?: boolean;
  /** Why no tracker can be shown here (the deployment serves none, or the read failed). */
  trackerNote?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  const anyBusy = busy !== null;
  const health = card.code ? worstHealth(card.code.branches) : null;
  const outOfScope = card.code?.branches.some(maintainsOutOfScope) ?? false;
  const summary = rowSummary(card);
  // The branch whose namespace holds the repository's documents
  const docsNs = card.code?.branches.find((b) => b.docs_namespace)?.docs_namespace ?? "";
  const docs = docsNs ? (card.code?.branches.find((b) => b.namespace === docsNs) ?? null) : null;
  const whoami = useStore((s) => s.whoami);
  const settings = card.maintained || (!!code.onSetAccess && !!card.code?.branches.some((b) => b.my_role === "admin"));

  return (
    <section aria-label={card.repo}>
      <ListRow>
        <div className="relative flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 min-w-0">
              {/* The disclosure: the heading is the button, stretched over the row's head,
                  so the row opens from anywhere on it (WAI-ARIA disclosure pattern). */}
              <h2 className="min-w-0 max-w-full text-sm font-semibold">
                <button
                  type="button"
                  data-row-open=""
                  onClick={() => setOpen((v) => !v)}
                  aria-expanded={open}
                  aria-controls={open ? bodyId : undefined}
                  className="list-row-open inline-flex items-start gap-1 break-words"
                >
                  <Icon
                    name="chevron-right"
                    size="sm"
                    aria-hidden="true"
                    className={`mt-0.5 shrink-0 ${open ? "rotate-90" : ""}`}
                  />
                  {card.repo}
                </button>
              </h2>
              {health && <HealthBadge health={health} />}
              {card.restricted && <Badge size="sm" state="neutral" text="Restricted" icon="lock" iconDir="start" />}
            </div>
            {summary && <p className="mt-0.5 text-xs text-text-muted tabular-nums break-words">{summary}</p>}
          </div>
          <div className="list-row-control sm:shrink-0">
            <RowAction card={card} busy={anyBusy} code={code} record={record} />
          </div>
        </div>

        {open && (
          <div id={bodyId} className="mt-3 space-y-3">
            {outOfScope && <p className="text-xs text-text-muted">{OUT_OF_SCOPE_MAINTAINER}</p>}
            {card.restricted && (
              // Membership reads it; a client token reads it only when added at mint
              // (Settings → For my own tools).
              <p className="text-xs text-text-muted" data-testid="repo-client-tokens-note">
                Your client tokens read it only when added in Settings.
              </p>
            )}
            {card.code && <BranchTable facet={card.code} busy={anyBusy} actions={code} />}
            <TrackerLine card={card} busy={anyBusy} code={code} record={record} trackerNote={trackerNote} />
            {settings && <Settings card={card} busy={anyBusy} code={code} adminOpsPresent={adminOpsPresent} />}
            {docs && (
              // The documents agents read before changing this code
              <FacetRow icon="document" label="Docs">
                <RepoDocsList
                  org={docs.org}
                  docsNamespace={docs.namespace}
                  repo={card.repo}
                  writable={canCallOn("knowledge.add", docs, whoami)}
                  busy={anyBusy}
                />
              </FacetRow>
            )}
            {docs && (
              // Where those documents are pulled from, and each source's last sync
              <FacetRow icon="connect" label="Sources">
                <RepoSourcesList
                  org={docs.org}
                  docsNamespace={docs.namespace}
                  repo={card.repo}
                  manageable={canCallOn("knowledge.source.add", docs, whoami)}
                  busy={anyBusy}
                />
              </FacetRow>
            )}
            {card.restricted && card.joinedCount === 0 && card.pendingCount === 0 && (
              <p className="text-xs text-text-muted">{RESTRICTED_SENTENCE}</p>
            )}
          </div>
        )}
      </ListRow>
    </section>
  );
});

// ---------------------------------------------------------------------------
// The maintainer's access (classification, join policy), in Settings. Every write goes
// through one op, audited.
// ---------------------------------------------------------------------------

type RepoClass = "standard" | "restricted";
type JoinPolicy = "open" | "approval" | "closed";
const CLASSES: ReadonlyArray<{ value: RepoClass; label: string }> = [
  { value: "standard", label: "standard — org readers may join" },
  { value: "restricted", label: "restricted — named token and a grant" },
];
const POLICIES: ReadonlyArray<{ value: JoinPolicy; label: string }> = [
  { value: "open", label: "open — joins at once, read-only" },
  { value: "approval", label: "approval — a maintainer approves" },
  { value: "closed", label: "closed — invitation only" },
];

function RepoAccessPanel({
  card,
  facet,
  busy,
  actions,
}: {
  card: CardModel;
  facet: CodeFacet;
  busy: boolean;
  actions: CodeActions;
}) {
  const primary = facet.primary;
  const currentClass: RepoClass = card.restricted ? "restricted" : "standard";
  const currentPolicy =
    oneOf(
      POLICIES.map((o) => o.value),
      String(primary.join_policy ?? ""),
    ) ?? "approval";
  const [cls, setCls] = useState<RepoClass>(currentClass);
  const [policy, setPolicy] = useState<JoinPolicy>(currentPolicy);
  const accessDirty = cls !== currentClass || policy !== currentPolicy;
  const openingRestricted = currentClass === "restricted" && cls === "standard";
  const restrictedOpen = cls === "restricted" && policy === "open";
  return (
    <section aria-label={`Access to ${card.repo}`} className="space-y-3">
      {actions.onSetAccess && (
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-full sm:w-72">
            <Select
              label="Access"
              value={cls}
              disabled={busy}
              onSelect={(value) => {
                const v = oneOf(
                  CLASSES.map((o) => o.value),
                  value,
                );
                if (v === null) return;
                setCls(v);
                if (v === "restricted" && policy === "open") setPolicy("approval");
              }}
            >
              {CLASSES.map((o) => (
                <Select.Item key={o.value} value={o.value}>
                  {o.label}
                </Select.Item>
              ))}
            </Select>
          </div>
          <div className="w-full sm:w-72">
            <Select
              label="Join policy"
              value={policy}
              disabled={busy}
              onSelect={(value) => {
                const v = oneOf(
                  POLICIES.map((o) => o.value),
                  value,
                );
                if (v !== null) setPolicy(v);
              }}
            >
              {POLICIES.map((o) => (
                <Select.Item key={o.value} value={o.value} disabled={o.value === "open" && cls === "restricted"}>
                  {o.label}
                </Select.Item>
              ))}
            </Select>
          </div>
          <Button
            type={openingRestricted ? "primary" : "secondary"}
            htmlType="button"
            disabled={busy || !accessDirty || restrictedOpen}
            onClick={() => actions.onSetAccess?.(facet, cls, policy, openingRestricted)}
          >
            {openingRestricted ? `Open ${card.repo}` : "Save access"}
          </Button>
          {openingRestricted && (
            <span className="text-xs text-warn">
              Opening a restricted index: you will be asked to type the namespace.
            </span>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * The repository's trackers as they are, on a maintainer's tracker line — `bound_trackers` on the maintainer's row
 * (primary first). The first is the primary: its numbers are the repository's, so a
 * lookup by number (#123, a PR's facts) reads it; every linked tracker is searched
 * (duplicates, similar reports, bisect). Each line says what agents get from it; Make
 * primary / Unlink per tracker, Link another (trackers listing the repository first), Add
 * a tracker.
 */
function TrackerLinkRow({
  card,
  facet,
  busy,
  actions,
}: {
  card: CardModel;
  facet: CodeFacet;
  busy: boolean;
  actions: CodeActions;
}) {
  const sources = actions.trackerSources ?? [];
  const records = bindableRecords(sources, card.repo);
  const linked = facet.primary.bound_trackers ?? [];
  const [editing, setEditing] = useState(false);
  const unlinked = records.filter((r) => !linked.includes(r.namespace));
  const [pick, setPick] = useState("");
  const covering = unlinked.filter((r) => r.covers);
  const others = unlinked.filter((r) => !r.covers);
  const picked = records.find((r) => r.namespace === pick);
  const set = (next: string[]) => actions.onSetSource?.(facet, next);
  const many = linked.length > 1;
  const primaryLabel = records.find((r) => r.namespace === linked[0])?.label ?? linked[0];

  return (
    // biome-ignore lint/a11y/useSemanticElements: a named group of read-only details of the card, not form controls and their link actions
    <div className="space-y-2" role="group" aria-label="Tracker link">
      {linked.length === 0 ? (
        <p className="text-sm text-text-muted">
          Not linked. Agents in this repository's checkout see no issues or PRs.
        </p>
      ) : (
        <ul className="space-y-1.5" aria-label="Linked trackers">
          {linked.map((ns, i) => (
            <LinkedTracker
              key={ns}
              ns={ns}
              primary={i === 0}
              many={many}
              repo={card.repo}
              records={records}
              sources={sources}
              busy={busy}
              onPrimary={() => set([ns, ...linked.filter((x) => x !== ns)])}
              onUnlink={() => set(linked.filter((x) => x !== ns))}
            />
          ))}
        </ul>
      )}
      {many && (
        <p className="text-xs text-text-muted">
          Agents search all {linked.length}; a lookup by number (#123, a PR&apos;s facts) reads{" "}
          <strong className="font-medium">{primaryLabel}</strong>, whose numbers are this repository&apos;s.
        </p>
      )}
      {!editing ? (
        <div className="flex flex-wrap items-center gap-2">
          {unlinked.length > 0 && (
            <Button
              type="secondary"
              htmlType="button"
              disabled={busy}
              onClick={() => {
                setPick(covering[0]?.namespace ?? "");
                setEditing(true);
              }}
            >
              {linked.length ? "Link another tracker" : "Link a tracker"}
            </Button>
          )}
          {actions.onRegisterSource && (
            <Button
              type="secondary"
              htmlType="button"
              iconLeft="plus"
              disabled={busy}
              onClick={() => actions.onRegisterSource?.(facet)}
            >
              Add a tracker…
            </Button>
          )}
        </div>
      ) : (
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <div className="w-full sm:w-auto sm:min-w-64">
              <Select
                value={pick || undefined}
                placeholder="Choose a tracker…"
                triggerProps={{ "aria-label": "Tracker" }}
                disabled={busy}
                onSelect={(value) => setPick(value)}
              >
                {covering.length > 0 && (
                  <Select.Group heading={`Trackers listing ${card.repo}`}>
                    {covering.map((r) => (
                      <Select.Item key={r.namespace} value={r.namespace}>
                        {r.label}
                      </Select.Item>
                    ))}
                  </Select.Group>
                )}
                {others.length > 0 && (
                  <Select.Group heading={`Other trackers — they answer nothing for ${card.repo}`}>
                    {others.map((r) => (
                      <Select.Item key={r.namespace} value={r.namespace}>
                        {r.label}
                      </Select.Item>
                    ))}
                  </Select.Group>
                )}
              </Select>
            </div>
            <Button type="secondary" htmlType="button" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button
              type="primary"
              htmlType="button"
              disabled={busy || !pick}
              onClick={() => {
                set([...linked, pick]);
                setEditing(false);
              }}
            >
              Link
            </Button>
          </div>
          {picked && !picked.covers && (
            <p className="text-xs text-warn">
              {picked.label} lists {picked.repos.join(", ") || "no repository"}, not {card.repo}: it would answer
              nothing here.
            </p>
          )}
          {pick && linked.length > 0 && (
            <p className="text-xs text-text-muted">
              It is searched alongside {primaryLabel}; {primaryLabel} stays primary.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** One linked tracker: what agents get from it, and its verbs. */
function LinkedTracker({
  ns,
  primary,
  many,
  repo,
  records,
  sources,
  busy,
  onPrimary,
  onUnlink,
}: {
  ns: string;
  primary: boolean;
  many: boolean;
  repo: string;
  records: BindableRecord[];
  sources: TrackerSource[];
  busy: boolean;
  onPrimary: () => void;
  onUnlink: () => void;
}) {
  const known = records.find((r) => r.namespace === ns);
  const source = known ? sources.find((x) => x.name === known.source) : undefined;
  const fresh = source ? freshnessOf(source) : null;
  let line: ReactNode;
  let warn = false;
  if (!known) {
    line = (
      <>
        Linked to <span className="font-mono">{ns}</span>, which is not in the tracker list — retired, or private to
        you.
      </>
    );
    warn = true;
  } else if (!known.covers) {
    line = (
      <>
        Linked to <strong className="font-medium">{known.label}</strong>, but it lists{" "}
        {known.repos.join(", ") || "no repository"}, not {repo} — agents get nothing from it here.
      </>
    );
    warn = true;
  } else {
    line = (
      <>
        Linked to <strong className="font-medium text-text">{known.label}</strong>
        {fresh ? (
          <>
            {" "}
            — {fmtInt(fresh.issues)} issues · {fmtInt(fresh.prs)} PRs
          </>
        ) : null}
        .{many ? null : <> Agents in this repository&apos;s checkout see them.</>}
      </>
    );
  }
  const name = known?.label ?? ns;
  return (
    <li className="flex flex-col sm:flex-row sm:items-start gap-x-2 gap-y-1">
      <div className={`text-sm ${warn ? "text-warn" : "text-text-muted"} flex items-start gap-1.5 min-w-0 sm:flex-1`}>
        {warn ? (
          <Icon name="warning" size="sm" color="var(--color-warn)" aria-hidden="true" className="shrink-0 mt-0.5" />
        ) : (
          <Icon name="check" size="sm" color="var(--color-success)" aria-hidden="true" className="shrink-0 mt-0.5" />
        )}
        <span className="min-w-0">{line}</span>
        {many && primary && <Badge text="Primary" state="neutral" size="sm" className="shrink-0" />}
      </div>
      <span className="flex items-center gap-1.5 pl-5 sm:pl-0 sm:shrink-0">
        {many && !primary && (
          <Button
            type="secondary"
            htmlType="button"
            disabled={busy}
            aria-label={`Make ${name} primary`}
            onClick={onPrimary}
          >
            Make primary
          </Button>
        )}
        <Button type="secondary" htmlType="button" disabled={busy} aria-label={`Unlink ${name}`} onClick={onUnlink}>
          Unlink
        </Button>
      </span>
    </li>
  );
}
