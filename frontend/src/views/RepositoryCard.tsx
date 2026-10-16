/**
 * RepositoryCard — a repository is one card with two facets: its code and
 * its record.
 *
 * The card is folded by default: one row — a disclosure button carrying the repository
 * (`Org/repo`), one open/restricted pill, the caller's standing in one phrase with a
 * one-line summary of both facets (branches, files, issues, PRs), and — right, or under
 * the header below 640 px — exactly one primary button. Opening it (the title, or the
 * chevron) reveals the facet rows: **Code** (the branch chips) and **Issues & PRs** (the
 * tracker namespace(s) whose `repos` name this repository, chips named by scope), at most
 * one quiet Leave link, and for maintainers the access panel and a Details disclosure —
 * source, host, database, origin, contract, breaker, the branch namespaces, and the admin
 * extension point. A maintainer's Add branch sits in the Code row beside the branch chips.
 *
 * Five words on both facets: Open · Join · Request access · Withdraw
 * request · Leave. A chip states where you stand and takes you somewhere;
 * it never changes anything by itself: join, request and withdraw confirm first. The
 * header's Join on an open branch is the one direct verb.
 */
import { useId, useState, type ReactNode } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Select } from "@clickhouse/click-ui/Select";
import type { CodeRepoSummary } from "../api";
import { absoluteTime, fmtInt, relativeTime } from "../lib/format";
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
import { Chip, MetaList, MetaRow, Panel, Tip, TipTarget } from "../components/ui";
import {
  branchState,
  canBeApproved,
  maintainsOutOfScope,
  NO_APPROVER,
  OUT_OF_SCOPE_MAINTAINER,
  primaryAction,
  RECORD_NOUN,
  soleMembership,
  verbForState,
  visibleBranches,
  type CodeFacet,
  type RecordFacet,
  type RecordNamespace,
  type RepositoryCard as CardModel,
  type Verb,
} from "../lib/repositoryCard";
import { RecordSourceDetails } from "./TrackerSources";
import { RepoDocsList } from "./RepoDocs";
import { oneOf } from "../lib/oneOf";

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

/** A status badge whose tooltip carries the detail; the wrapper takes focus so keyboard
 *  users reach the tip. */
function TipBadge({ text, state, tip }: { text: string; state: BadgeState; tip: string }) {
  return (
    <TipTarget tip={tip}>
      <Badge text={text} state={state} size="sm" />
    </TipTarget>
  );
}

const VERB_ICON: Record<Verb, IconName> = {
  Open: "chevron-right",
  Join: "users",
  "Request access": "lock",
  "Withdraw request": "clock",
  Leave: "chevron-right",
};

// ---------------------------------------------------------------------------
// Chips. One shape for a branch and for a record scope: the `Chip`
// primitive as a button (border, glyph, hover ring), the standing word after
// the name, the verb in the tooltip and the accessible name.
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
      selected={st === "member"}
      onClick={onClick}
      disabled={disabled || !onClick}
      ariaLabel={`${name}: ${disabledReason ? disabledReason : standingWord(st)}`}
      tip={tip}
      className={
        st === "pending"
          ? "border-warn/40 text-warn"
          : st === "closed" || st === "registering"
            ? "text-text-subtle"
            : ""
      }
    />
  );
}

/** A branch chip: member → Open, open/approval → the request dialog,
 *  pending → withdraw. `onJoin` is accepted for callers that still pass it
 *  and never called — a chip never joins directly. */
export function BranchChip({
  b,
  busy,
  onOpen,
  onJoin,
  onRequest,
  onCancel,
}: {
  b: CodeRepoSummary;
  busy: boolean;
  onOpen: () => void;
  onJoin?: () => void;
  onRequest: () => void;
  onCancel: () => void;
}) {
  void onJoin;
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
 *  instant-vs-pending before anything is sent, pending → withdraw. The header's Join
 *  button is the one direct verb. */
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
      <div className="section-label inline-flex items-center gap-1.5 text-text-muted">
        <Icon name={icon} size="xs" aria-hidden="true" /> {label}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function Verbs({ children }: { children: ReactNode }) {
  return <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5">{children}</div>;
}

export interface CodeActions {
  onOpen: (b: CodeRepoSummary) => void;
  /** The header's direct join of an open branch. */
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
  /** A maintainer sets the repository's access (every branch it maintains). */
  onSetAccess?: (
    facet: CodeFacet,
    classification: "standard" | "restricted",
    join_policy: "open" | "approval" | "closed",
    opening: boolean,
  ) => void;
  /** A maintainer binds the tracker record the repository answers from ("" clears). */
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

function CodeFacetRow({
  facet: c,
  busy,
  actions,
  leaveInline,
}: {
  facet: CodeFacet;
  busy: string | null;
  actions: CodeActions;
  leaveInline: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const shown = visibleBranches(c, expanded);
  const hidden = c.branches.length - shown.length;
  const head = c.head;
  const building = stageLabel(head);
  const f = !building && head.git_head ? freshness(head) : null;
  const anyBusy = busy !== null;
  const openable = c.joinable.filter((b) => branchState(b) === "open").length;
  return (
    <FacetRow icon="git-merge" label="Code">
      <p className="text-xs text-text-muted tabular-nums flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        <span>
          {c.branches.length} branch{c.branches.length === 1 ? "" : "es"}
        </span>
        {head.counts && head.counts.code_files ? (
          <span>
            · {fmtInt(head.counts.code_files)} files · {fmtInt(head.counts.code_symbols)} symbols at {head.branch}
          </span>
        ) : null}
        {head.indexed_at && <span>· indexed {relativeTime(head.indexed_at.replace(" ", "T") + "Z")}</span>}
        {f && <TipBadge text={f.label} state={FRESH_STATE[f.tone]} tip={f.detail} />}
        {building && <TipBadge text={building.label} state={FRESH_STATE[building.tone]} tip={building.title} />}
      </p>
      <ul className="mt-2 flex flex-wrap items-center gap-2 list-none p-0 m-0" aria-label={`${c.repo} branches`}>
        {shown.map((b) => (
          <li key={b.namespace} className="inline-flex max-w-full">
            <BranchChip
              b={b}
              busy={anyBusy}
              onOpen={() => actions.onOpen(b)}
              onRequest={() => actions.onRequest(b, "viewer")}
              onCancel={() => actions.onCancel(b)}
            />
          </li>
        ))}
      </ul>
      <Verbs>
        {(hidden > 0 || expanded) && (
          <Button type="empty" htmlType="button" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
            {expanded ? "Show fewer" : `Show ${hidden} more`}
          </Button>
        )}
        {c.joinable.length > 1 && (
          <Tip
            content={
              openable === c.joinable.length
                ? "Instant, read-only"
                : "Open branches join instantly; restricted ones go to a maintainer"
            }
          >
            <Button type="empty" htmlType="button" onClick={() => actions.onJoinAll(c.branches)} disabled={anyBusy}>
              {openable === 0 ? "Request access to" : "Join"} all {c.joinable.length} branches
            </Button>
          </Tip>
        )}
        {c.maintained && actions.onAddBranch && (
          <Tip content="Index another branch of this repository. It shares the mirror; you maintain it.">
            <Button
              type="empty"
              htmlType="button"
              iconLeft="git-merge"
              onClick={() => actions.onAddBranch?.(c)}
              disabled={anyBusy}
            >
              Add branch
            </Button>
          </Tip>
        )}
        {leaveInline && c.joined.length === 1 && (
          <Button type="empty" htmlType="button" onClick={() => actions.onLeave(c.joined[0])} disabled={anyBusy}>
            Leave · {c.joined[0].branch}
          </Button>
        )}
        {c.joined.length > 1 && (
          <Button type="empty" htmlType="button" onClick={() => actions.onLeaveAll(c.branches)} disabled={anyBusy}>
            Leave all {c.joined.length} branches
          </Button>
        )}
        {c.branches.length > 1 && (
          <Tip content="Pick exactly which branches you read">
            <Button type="empty" htmlType="button" onClick={() => actions.onManage(c)} disabled={anyBusy}>
              Choose branches…
            </Button>
          </Tip>
        )}
        {!c.maintained && c.pendingCount === 0 && branchState(c.primary) !== "closed" && (
          <Tip content="Maintainers approve readers and set how people join. A current maintainer decides.">
            <Button
              type="empty"
              htmlType="button"
              onClick={() => actions.onRequest(c.primary, "admin")}
              disabled={anyBusy}
            >
              Request maintainer access
            </Button>
          </Tip>
        )}
      </Verbs>
    </FacetRow>
  );
}

function RecordFacetRow({
  facet: r,
  busy,
  actions,
  leaveInline,
}: {
  facet: RecordFacet;
  busy: string | null;
  actions: RecordActionProps;
  leaveInline: boolean;
}) {
  const anyBusy = busy !== null;
  const f = r.freshness;
  const unapprovable = r.namespaces.some((n) => n.state === "approval" && canBeApproved(n.ns) === false);
  return (
    <FacetRow icon="database" label="Tracker">
      <p className="text-xs text-text-muted tabular-nums flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
        {f ? (
          <>
            {r.sources.length > 0 && <span className="text-text">{r.sources.map((x) => x.name).join(", ")}</span>}
            {r.sources.length > 0 && <span aria-hidden="true">·</span>}
            <span>
              {fmtInt(f.issues)} issues · {fmtInt(f.prs)} PRs
            </span>
            {f.last_refresh_at && (
              <TipTarget tip={`Last refresh ${absoluteTime(f.last_refresh_at)}`} underline>
                · refreshed {relativeTime(f.last_refresh_at)}
              </TipTarget>
            )}
          </>
        ) : r.unconfigured ? (
          <TipBadge
            text="no credential"
            state="neutral"
            tip="The tracker is declared but has no credential yet — nothing can be read from it until one is added."
          />
        ) : (
          <span>read live · counts not available right now</span>
        )}
      </p>
      <ul className="mt-2 flex flex-wrap items-center gap-2 list-none p-0 m-0" aria-label="tracker scopes">
        {r.namespaces.map((n) => (
          <li key={n.ns.namespace} className="inline-flex max-w-full">
            <RecordChip
              r={n}
              busy={anyBusy}
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
      {(leaveInline || r.joined.length > 1) && r.joined.length > 0 && (
        <Verbs>
          {r.joined.map((n) => (
            <Button
              key={n.ns.namespace}
              type="empty"
              htmlType="button"
              onClick={() => actions.onLeave(n)}
              disabled={anyBusy}
            >
              Leave · {r.joined.length > 1 ? n.label : RECORD_NOUN}
            </Button>
          ))}
        </Verbs>
      )}
    </FacetRow>
  );
}

// ---------------------------------------------------------------------------
// Details (maintainers only)
// ---------------------------------------------------------------------------

function Details({ card, adminOpsPresent }: { card: CardModel; adminOpsPresent: boolean }) {
  return (
    <details className="group mt-3 border-t border-border pt-2">
      <summary className="inline-flex items-center gap-1 min-h-9 px-1 -mx-1 rounded text-xs text-text-muted hover:text-text cursor-pointer list-none pointer-coarse:min-h-11 pointer-coarse:min-w-11">
        <Icon name="chevron-right" size="xs" aria-hidden="true" className="transition-transform group-open:rotate-90" />{" "}
        Details
      </summary>
      <div className="mt-2 space-y-3">
        {card.code && (
          // biome-ignore lint/a11y/useSemanticElements: a named group of read-only details of the card, not form controls
          <div role="group" aria-label="Code details">
            <MetaList>
              {card.code.branches.map((b) => (
                <MetaRow key={b.namespace} label={b.branch} mono>
                  {b.namespace}
                  {b.git_head ? ` · ${b.git_head.slice(0, 12)}` : ""}
                </MetaRow>
              ))}
              <MetaRow label="origin">
                {card.code.head.source === "registry" ? "added from the dashboard" : "configured by the operator"}
              </MetaRow>
            </MetaList>
          </div>
        )}
        {card.record?.sources.map((s) => (
          <RecordSourceDetails key={s.name} source={s} manageable={canAdminister(s, adminOpsPresent)} />
        ))}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

/** The folded row's one-line summary of both facets: "3 branches · 31,539 files · 22,579 issues · 65,328 PRs". */
function cardSummary(card: Pick<CardModel, "code" | "record">): string {
  const parts: string[] = [];
  if (card.code) {
    const n = card.code.branches.length;
    parts.push(`${n} branch${n === 1 ? "" : "es"}`);
    const files = card.code.head.counts?.code_files;
    if (files) parts.push(`${fmtInt(files)} files`);
  }
  if (card.record) {
    const f = card.record.freshness;
    if (f) parts.push(`${fmtInt(f.issues)} issues`, `${fmtInt(f.prs)} PRs`);
    else if (card.record.unconfigured) parts.push(`${RECORD_NOUN}: no credential`);
    // No counts yet — the standing phrase already names the tracker
  }
  return parts.join(" · ");
}

export function RepositoryCard({
  card,
  busy,
  code,
  record,
  adminOpsPresent = false,
  defaultOpen = false,
}: {
  card: CardModel;
  busy: string | null;
  code: CodeActions;
  record: RecordActionProps;
  adminOpsPresent?: boolean;
  /** A card is folded until the person opens it. */
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  const anyBusy = busy !== null;
  const primary = primaryAction(card);
  const sole = soleMembership(card);
  const outOfScope = card.code?.branches.some(maintainsOutOfScope) ?? false;
  const summary = cardSummary(card);
  // The branch whose namespace holds the repository's documents
  const docsNs = card.code?.branches.find((b) => b.docs_namespace)?.docs_namespace ?? "";
  const docs = docsNs ? (card.code?.branches.find((b) => b.namespace === docsNs) ?? null) : null;

  const fire = (p: NonNullable<typeof primary>) => {
    if (p.facet === "code" && p.branch) {
      const b = p.branch;
      if (p.verb === "Open") code.onOpen(b);
      else if (p.verb === "Join") code.onJoin(b);
      else if (p.verb === "Request access") code.onRequest(b, "viewer");
      else code.onCancel(b);
    } else if (p.record) {
      const r = p.record;
      if (p.verb === "Open") record.onOpen(r);
      else if (p.verb === "Join") record.onJoin(r, false);
      else if (p.verb === "Request access") record.onRequest(r);
      else record.onCancel(r);
    }
  };
  const leaveSole = () => {
    if (!sole) return;
    if (sole.branch) code.onLeave(sole.branch);
    else if (sole.record) record.onLeave(sole.record);
  };
  // Join and Request access are the card's call to action; Open and Withdraw are not.
  const primaryType = primary?.verb === "Open" || primary?.verb === "Withdraw request" ? "secondary" : "primary";

  return (
    <Panel as="section" aria-label={card.repo} className={open ? "p-pane sm:p-4" : "px-2 py-1.5 sm:px-3 sm:py-2"}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 min-w-0">
            {/* The disclosure: the heading is the button, so the row opens from its
                name (WAI-ARIA disclosure pattern); the chevron says which way. */}
            <h2 className="text-lg font-semibold tracking-tight min-w-0 max-w-full">
              <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                aria-controls={open ? bodyId : undefined}
                className="inline-flex items-start gap-1.5 min-h-9 -ml-1 pl-1 pr-2 py-1.5 rounded text-left hover:bg-surface-2 pointer-coarse:min-h-11"
              >
                <Icon
                  name={open ? "chevron-down" : "chevron-right"}
                  size="sm"
                  aria-hidden="true"
                  className="mt-1 shrink-0"
                />
                <span className="break-words">{card.repo}</span>
              </button>
            </h2>
            {/* A static pill, not a target: the page intro says what open and
                restricted mean, so it carries no tooltip and no 44 px box. */}
            <Badge
              text={card.restricted ? "restricted" : "open"}
              state={card.restricted ? "warning" : "success"}
              size="sm"
            />
            {card.restricted && open && (
              // Membership reads it; a client token reads it only when added at mint
              // (Setup → For my own tools).
              <span className="text-xs text-text-muted" data-testid="repo-client-tokens-note">
                your client tokens: only when added at Setup
              </span>
            )}
          </div>
          {(card.standing || (!open && summary)) && (
            <p className="mt-0.5 pl-6 text-xs text-text-muted tabular-nums flex flex-wrap items-center gap-x-1.5">
              {card.standing && <span>{card.standing}</span>}
              {!open && summary && (
                <>
                  {card.standing && <span aria-hidden="true">·</span>}
                  <span>{summary}</span>
                </>
              )}
            </p>
          )}
        </div>
        {primary && (
          <div className="flex flex-wrap items-center gap-2 pl-6 sm:pl-0 sm:shrink-0">
            <Button
              type={primaryType}
              htmlType="button"
              iconLeft={VERB_ICON[primary.verb]}
              onClick={() => fire(primary)}
              disabled={anyBusy}
              className="w-full sm:w-auto"
            >
              {primary.verb} · {primary.qualifier}
            </Button>
          </div>
        )}
      </div>

      {open && (
        <div id={bodyId}>
          {(sole || outOfScope) && (
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6">
              {sole && (
                <Button type="empty" htmlType="button" onClick={leaveSole} disabled={anyBusy}>
                  Leave · {sole.qualifier}
                </Button>
              )}
              {outOfScope && <p className="text-xs text-text-muted">{OUT_OF_SCOPE_MAINTAINER}</p>}
            </div>
          )}

          <div className="mt-3 space-y-3">
            {card.code && (
              <CodeFacetRow facet={card.code} busy={busy} actions={code} leaveInline={!sole && card.joinedCount > 1} />
            )}
            {card.record && (
              <RecordFacetRow
                facet={card.record}
                busy={busy}
                actions={record}
                leaveInline={!sole && card.joinedCount > 1}
              />
            )}
            {docs && (
              // The documents agents read before changing this code
              <FacetRow icon="document" label="Docs">
                <RepoDocsList
                  org={docs.org}
                  docsNamespace={docs.namespace}
                  repo={card.repo}
                  maintained={docs.my_role === "admin"}
                  busy={anyBusy}
                />
              </FacetRow>
            )}
            {card.code &&
              card.code.branches.some((b) => b.my_role === "admin") &&
              (code.onSetAccess || code.onSetSource) && (
                <RepoAccessPanel card={card} facet={card.code} busy={anyBusy} actions={code} />
              )}
          </div>

          {card.restricted && card.joinedCount === 0 && card.pendingCount === 0 && (
            <p className="mt-3 text-xs text-text-muted">{RESTRICTED_SENTENCE}</p>
          )}

          {card.maintained && <Details card={card} adminOpsPresent={adminOpsPresent} />}
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The maintainer's panel — access (classification, join policy) and the tracker record
// the repository answers from. Shown when the person maintains a branch; every write goes
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
    <section
      aria-label={`Maintain ${card.repo}`}
      className="rounded-lg border border-border bg-surface-2/40 p-3 space-y-3"
    >
      <div className="text-xs font-medium text-text-muted">Maintain · access and tracker</div>
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
      {actions.onSetSource && <TrackerLinkRow card={card} facet={facet} busy={busy} actions={actions} />}
    </section>
  );
}

/**
 * The repository's trackers as they are — `bound_trackers` on the maintainer's row
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
    <div className="space-y-2 border-t border-border pt-3" role="group" aria-label="Tracker link">
      <div className="text-xs text-text-muted font-medium">{many ? `Trackers · ${linked.length}` : "Tracker"}</div>
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
      <p className={`text-sm ${warn ? "text-warn" : "text-text-muted"} flex items-start gap-1.5 min-w-0 sm:flex-1`}>
        {warn ? (
          <Icon name="warning" size="sm" color="var(--color-warn)" aria-hidden="true" className="shrink-0 mt-0.5" />
        ) : (
          <Icon name="check" size="sm" color="var(--color-success)" aria-hidden="true" className="shrink-0 mt-0.5" />
        )}
        <span className="min-w-0">{line}</span>
        {many && primary && <Badge text="primary" state="neutral" size="sm" className="shrink-0" />}
      </p>
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
