import { useState } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import type { TrackerItem, TrackerSection } from "../../../api";
import { IconButton } from "../../../components/IconButton";
import {
  Chip,
  ExternalLink,
  Loading,
  MetaList,
  MetaRow,
  Panel,
  SectionHeading,
  TextClamp,
  Tip,
} from "../../../components/ui";
import { shorten } from "../model";
import { areaClass, type AreaClass, type Cell, type DuplicateRow, type Issue } from "./model";

/**
 * PatternsInspector — the right pane in Patterns mode.
 *
 * A cell: component, severity, the counts, the issue list (one clamped line
 * each: `#number` title, severity chip, flags), "Open on GitHub", and
 * "Similar" (runs `similar_bugs` with the issue's title as the text). The
 * list is the cell: the server filtered the read to the severity, and "N of M shown"
 * appears only when the read's ceiling was hit. The restricted class — incidents and
 * escalations in the component — is rows on a restricted tracker namespace (one line
 * each: ref, title, severity, opened, what the row knows), an honest "none in the window"
 * when empty, and on a standard namespace one line — "restricted — not available with
 * this token" — never an error.
 *
 * An issue: title, severity, labels, caused_by / fixed_by with their titles
 * when the second `tracker.item` read knows them, the `[tracker]` line, and the
 * actions: show it in the tracker graph, walk from `#number` in the co-mention
 * graph when the number is an entity there, open on GitHub, copy.
 */

export interface SimilarHit {
  number: number;
  title: string;
  severity: string;
  component: string;
  url: string;
  open: boolean;
  partial: boolean;
}

export interface PatternsInspectorProps {
  trackerNamespace: string;
  cell: { component: string; severity: string; label: string } | null;
  cellCounts: Cell | null;
  issues: Issue[] | null;
  /** The read's row ceiling (`params.limit`; 300 for a cell). */
  issuesRead: number;
  /** The ceiling was reached, so the list may be shorter than the cell. */
  issuesCapped: boolean;
  issuesStatus: "idle" | "loading" | "served" | "empty" | "tracker_unavailable" | "restricted_required" | "error";
  issuesError: string | null;
  restricted: string[];
  /** The cell's area plan, for the restricted class (incidents, escalations). */
  sections: TrackerSection[];
  /** The answering tracker namespace is restricted: the class above is served. */
  restrictedNamespace: boolean;
  /** The `duplicates` rows of the selected issue. */
  duplicates: DuplicateRow[];
  issue: Issue | null;
  item: TrackerItem | null;
  itemStatus: "idle" | "loading" | "served" | "unknown" | "tracker_unavailable" | "error";
  neighbours: Map<number, TrackerItem>;
  similar: SimilarHit[] | null;
  similarBusy: boolean;
  similarFor: string | null;
  isEntity: (n: number) => boolean;
  onPickIssue: (n: number | null) => void;
  onShowGraph: () => void;
  onSimilar: (title: string) => void;
  onWalk: (entity: string) => void;
  onClearCell: () => void;
}

const RESTRICTED_WORDS: Record<string, string> = {
  area_incidents: "incidents",
  area_escalations: "escalations",
  customers_in_area: "customers",
};

function sevTone(s: string): string {
  return s === "critical" || s === "major" ? "text-danger" : s === "medium" ? "text-warn" : "text-text-muted";
}

const AREA_CLASS: Array<{ template: string; label: string; hint: string }> = [
  {
    template: "area_incidents",
    label: "Incidents",
    hint: "area_incidents: core incidents (production crashes, logical errors) whose component is this one, newest first, most severe first, in the window. Served only through a restricted tracker namespace.",
  },
  {
    template: "area_escalations",
    label: "Escalations",
    hint: "area_escalations: support escalations naming this component, newest first, most severe first, in the window. Served only through a restricted tracker namespace.",
  },
];

function AreaClassList({
  a,
  label,
  hint,
  trackerNamespace,
}: {
  a: AreaClass;
  label: string;
  hint: string;
  trackerNamespace: string;
}) {
  if (a.status === "unplanned" || a.status === "restricted_required") return null;
  const caption =
    a.status === "served" && a.capped
      ? `first ${a.docs.length} in ${a.days} days`
      : `restricted class · ${a.days} days`;
  return (
    <div className="mt-3">
      <SectionHeading
        label={label}
        count={a.status === "served" ? a.docs.length : undefined}
        caption={caption}
        hint={`${hint} Answered by ${trackerNamespace}${a.capped ? `; the read stops at ${a.docs.length} rows, most severe then newest first` : ""}.`}
      />
      {a.status === "empty" && <p className="text-xs text-text-muted">None in the last {a.days} days.</p>}
      {a.status === "tracker_unavailable" && (
        <p className="text-xs text-warn">The tracker source could not be read; nothing here is a claim.</p>
      )}
      {a.status === "served" && a.docs.length > 0 && (
        <ul className="space-y-1" aria-label={`${label} in this component`}>
          {a.docs.map((d) => (
            <li key={d.source_id}>
              <Tip content={`${d.source_id} — ${d.title}${d.detail ? ` — ${d.detail}` : ""}`}>
                <div className="text-xs rounded-md border border-border bg-surface px-2 py-1.5 min-h-7">
                  <div className="flex items-center gap-1.5">
                    <Icon
                      name="lock"
                      size="xs"
                      className="shrink-0"
                      color="var(--color-text-muted)"
                      aria-hidden="true"
                    />
                    <span className="font-mono shrink-0 text-text-muted">{d.ref}</span>
                    <TextClamp text={d.title} lines={1} max={160} reach="parent" className="flex-1 min-w-0" />
                    <span className={`shrink-0 ${sevTone(d.severity)}`}>{d.severity}</span>
                    {d.url && <ExternalLink href={d.url} label={`Open ${d.ref}`} />}
                  </div>
                  <div className="mt-0.5 pl-4 text-xs text-text-muted truncate tabular-nums">
                    {d.open ? "open" : "closed"}
                    {d.opened_at ? ` · opened ${d.opened_at.slice(0, 10)}` : ""}
                    {d.detail ? ` · ${d.detail}` : ""}
                  </div>
                </div>
              </Tip>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Flags({ i }: { i: Issue }) {
  const f = [
    i.untested ? "untested" : null,
    i.flaky ? "flaky" : null,
    i.bisected ? `bisected${i.bisect_outcome ? `: ${i.bisect_outcome}` : ""}` : null,
  ].filter((x): x is string => typeof x === "string");
  if (!f.length) return null;
  return <span className="text-xs text-text-muted shrink-0">{f.join(" · ")}</span>;
}

export function PatternsInspector(p: PatternsInspectorProps) {
  const [copied, setCopied] = useState(false);
  const copy = (text: string) => {
    navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };
  const restrictedLine = p.restricted.length > 0 && (
    <div className="mt-2 text-xs text-text-muted inline-flex items-start gap-1">
      <Icon name="lock" size="xs" className="mt-0.5 shrink-0" aria-hidden="true" />
      <span>
        {p.restricted.map((t) => RESTRICTED_WORDS[t] ?? t).join(", ")}: restricted — not available with this token
      </span>
    </div>
  );

  if (p.issue) {
    const i = p.issue;
    const q = p.item?.tracker;
    const titled = (n: number) => {
      const it = p.neighbours.get(n);
      return it ? `#${n} ${shorten(it.title, 48)}` : `#${n}`;
    };
    return (
      <Panel
        as="aside"
        aria-label="Inspector"
        tone="surface-2"
        scroll
        header={
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <SectionHeading label="Issue" caption={p.cell ? `${p.cell.label} · ${p.cell.severity}` : undefined} />
              <div className="text-sm font-semibold break-words">
                <span className="font-mono">#{i.number}</span> {i.title}
              </div>
            </div>
            <IconButton label="Back to the cell" onClick={() => p.onPickIssue(null)} icon="cross" size="sm" />
          </div>
        }
      >
        <MetaList className="mt-2" aria-label="Issue detail">
          <MetaRow label="severity">
            <span className={sevTone(i.severity)}>{i.severity}</span>
          </MetaRow>
          <MetaRow label="component">{i.component || <span className="text-text-muted">none</span>}</MetaRow>
          <MetaRow label="labels">
            {i.labels.length ? (
              <span className="flex flex-wrap gap-1">
                {i.labels.map((l) => (
                  <Chip key={l} label={l} />
                ))}
              </span>
            ) : (
              <span className="text-text-muted">none</span>
            )}
          </MetaRow>
          <MetaRow label="flags">
            <Flags i={i} /> {!i.untested && !i.flaky && !i.bisected && <span className="text-text-muted">none</span>}
          </MetaRow>
          <MetaRow label="state">
            {i.state}
            {i.opened_at ? ` · opened ${i.opened_at.slice(0, 10)}` : ""}
          </MetaRow>
          {p.itemStatus === "loading" && (
            <MetaRow label="tracker" tone="muted">
              <Loading layout="inline" label="Loading the record" />
            </MetaRow>
          )}
          {p.itemStatus === "unknown" && (
            <MetaRow label="tracker" tone="muted">
              The tracker has no row for this number yet (the sync lags minutes)
            </MetaRow>
          )}
          {p.itemStatus === "tracker_unavailable" && (
            <MetaRow label="tracker" tone="warn">
              the tracker source could not be read; nothing below is a claim
            </MetaRow>
          )}
          {p.itemStatus === "error" && (
            <MetaRow label="tracker" tone="danger">
              the record read failed
            </MetaRow>
          )}
          {q && (
            <>
              <MetaRow label="caused by">
                {q.caused_by?.length ? (
                  <span className="flex flex-col gap-0.5">
                    {q.caused_by.map((n) => (
                      <Link
                        key={n}
                        component="button"
                        type="button"
                        size="sm"
                        className="text-left break-words"
                        onClick={() => p.onPickIssue(n)}
                      >
                        {titled(n)} <span className="text-danger text-xs">named by graph</span>
                      </Link>
                    ))}
                  </span>
                ) : (
                  <span className="text-text-muted">no caused_by edge recorded</span>
                )}
              </MetaRow>
              <MetaRow label="fixed by">
                {q.fixed_by?.length ? (
                  <span className="flex flex-col gap-0.5">
                    {q.fixed_by.map((n) => (
                      <Link
                        key={n}
                        component="button"
                        type="button"
                        size="sm"
                        className="text-left break-words"
                        onClick={() => p.onPickIssue(n)}
                      >
                        {titled(n)}
                      </Link>
                    ))}
                  </span>
                ) : (
                  <span className="text-text-muted">no fixes edge recorded</span>
                )}
              </MetaRow>
              {q.affected_versions?.length ? (
                <MetaRow label="affects" mono>
                  {q.affected_versions.join(", ")}
                </MetaRow>
              ) : null}
              {q.introduced_commit && (
                <MetaRow label="introduced" mono>
                  {q.introduced_commit}
                </MetaRow>
              )}
            </>
          )}
          {p.duplicates.filter((d) => d.rel_type === "duplicate_of").length > 0 && (
            <MetaRow label="duplicates">
              <span className="flex flex-col gap-0.5">
                {p.duplicates
                  .filter((d) => d.rel_type === "duplicate_of")
                  .map((d) => (
                    <button
                      key={d.number}
                      type="button"
                      className="text-left underline decoration-dotted hover:text-text break-words"
                      onClick={() => p.onPickIssue(d.number)}
                    >
                      #{d.number} {shorten(d.title, 48)}{" "}
                      <span className="text-text-muted text-xs">
                        {d.direction === "out" ? "this duplicates it" : "duplicates this"}
                      </span>
                    </button>
                  ))}
              </span>
            </MetaRow>
          )}
          {p.duplicates.filter((d) => d.rel_type === "related_to").length > 0 && (
            <MetaRow label="related">
              <span className="flex flex-col gap-0.5">
                {p.duplicates
                  .filter((d) => d.rel_type === "related_to")
                  .map((d) => (
                    <button
                      key={d.number}
                      type="button"
                      className="text-left underline decoration-dotted hover:text-text break-words"
                      onClick={() => p.onPickIssue(d.number)}
                    >
                      #{d.number} {shorten(d.title, 48)}{" "}
                      <span className="text-text-muted text-xs">related, not a duplicate</span>
                    </button>
                  ))}
              </span>
            </MetaRow>
          )}
          {p.item?.line && (
            <MetaRow label="[tracker]" mono>
              {p.item.line}
            </MetaRow>
          )}
        </MetaList>
        <div className="mt-3 flex items-center gap-0.5 -mx-1 flex-wrap">
          <IconButton label="Show in the tracker graph" onClick={p.onShowGraph} icon="git-merge" size="sm" />
          {p.isEntity(i.number) && (
            <IconButton
              label={`Walk from #${i.number} in the co-mention graph`}
              onClick={() => p.onWalk(String(i.number))}
              icon="arrow-right"
              size="sm"
            />
          )}
          <IconButton label={`Similar to #${i.number}`} onClick={() => p.onSimilar(i.title)} icon="search" size="sm" />
          {i.url && (
            <ExternalLink href={i.url} size="sm" className="min-h-7">
              Open on GitHub
            </ExternalLink>
          )}
          <IconButton
            label="Copy the issue line"
            onClick={() => copy(`#${i.number} ${i.title}${p.item?.line ? ` ${p.item.line}` : ""}`)}
            icon="copy"
            size="sm"
          />
          {copied && <span className="text-xs text-text-muted">copied</span>}
        </div>
        {p.similarFor === i.title && (
          <div className="mt-3">
            <SectionHeading
              label="Similar"
              count={p.similar ? p.similar.length : undefined}
              caption="by title"
              hint="similar_bugs: issues whose title or body carries the tokens of this issue's title, newest first; partial when only some tokens matched."
            />
            {p.similarBusy && <Loading layout="inline" label="Loading similar issues" />}
            {p.similar && p.similar.length === 0 && !p.similarBusy && (
              <p className="text-xs text-text-muted">No other issue carries these words.</p>
            )}
            {p.similar && p.similar.length > 0 && (
              <ul className="space-y-1">
                {p.similar
                  .filter((h) => h.number !== i.number)
                  .map((h) => (
                    <li
                      key={`${h.number}-${h.title}`}
                      className="text-xs rounded-md border border-border bg-surface px-2 py-1.5 flex items-start gap-1.5"
                    >
                      <span className="flex-1 min-w-0 break-words">
                        <span className="font-mono">#{h.number}</span> {h.title}
                        {h.partial ? <span className="text-text-muted"> · partial</span> : ""}
                      </span>
                      <span className={`shrink-0 ${sevTone(h.severity)}`}>{h.severity || "—"}</span>
                      {h.url && <ExternalLink href={h.url} label={`Open #${h.number} on GitHub`} />}
                    </li>
                  ))}
              </ul>
            )}
          </div>
        )}
        {restrictedLine}
      </Panel>
    );
  }

  const cell = p.cell;
  if (!cell) {
    return (
      <Panel
        as="aside"
        aria-label="Inspector"
        tone="surface-2"
        scroll
        className="text-xs text-text-muted"
        header={<SectionHeading label="Reading the board" />}
      >
        <ul className="space-y-1.5">
          <li>
            each row is a component, each column a severity; a cell is the open issues there and, smaller, the share
            with no linked test
          </li>
          <li>
            the row header carries the risk snapshot's gap ratio and its trend (▲ worse, ▼ better) when the snapshot
            names the component
          </li>
          <li>choose a cell to list its issues here; pick an issue for its record and its tracker graph</li>
          <li>
            the tracker graph draws caused-by (left), fixed-by (right) and duplicate (below) edges from the tracker; a
            PR the graph names as the cause is marked
          </li>
          <li>
            incidents, escalations and customers need a restricted tracker namespace and are named as such, never listed
            empty
          </li>
        </ul>
        <p className="mt-3 text-xs text-text-muted">
          source: <span className="font-mono">{p.trackerNamespace}</span>
        </p>
      </Panel>
    );
  }

  const c = p.cellCounts;
  return (
    <Panel
      as="aside"
      aria-label="Inspector"
      tone="surface-2"
      scroll
      header={
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <SectionHeading label="Cell" />
            <div className="text-sm font-semibold break-words">
              {cell.label} <span className={`font-normal ${sevTone(cell.severity)}`}>{cell.severity}</span>
            </div>
          </div>
          <IconButton label="Clear the cell" onClick={p.onClearCell} icon="cross" size="sm" />
        </div>
      }
    >
      {c && (
        <MetaList className="mt-2" aria-label="Cell counts">
          <MetaRow label="open">{c.open}</MetaRow>
          <MetaRow label="untested">
            {c.untested}{" "}
            <span className="text-text-muted">
              ({c.open ? Math.round((c.untested / c.open) * 100) : 0}% — no linked test known)
            </span>
          </MetaRow>
          {c.flaky > 0 && <MetaRow label="flaky">{c.flaky}</MetaRow>}
          {c.bisected > 0 && <MetaRow label="bisected">{c.bisected}</MetaRow>}
        </MetaList>
      )}
      <div className="mt-3">
        <SectionHeading
          label="Issues"
          count={p.issues ? p.issues.length : undefined}
          caption={p.issues && c && p.issues.length !== c.open ? `of ${c.open}` : undefined}
          hint={`area_open_issues reads the open ${cell.severity} issues of ${cell.label} — the cell the board counted, filtered on the server, up to ${p.issuesRead || 300} rows, most severe then newest first; the flag chips narrow the list here. When the ceiling is reached the list says so.`}
        />
        {p.issuesStatus === "loading" && <Loading layout="inline" label="Loading issues" />}
        {p.issuesStatus === "error" && (
          <p className="text-xs text-danger">{p.issuesError || "The issue list did not answer."}</p>
        )}
        {p.issuesStatus === "tracker_unavailable" && (
          <p className="text-xs text-warn">The tracker source could not be read; nothing here is a claim.</p>
        )}
        {p.issuesStatus === "restricted_required" && (
          <div className="text-xs text-text-muted inline-flex items-center gap-1">
            <Icon name="lock" size="xs" aria-hidden="true" /> restricted — not available with this token
          </div>
        )}
        {p.issuesStatus === "empty" && (
          <p className="text-xs text-text-muted">No open issue of this component was served.</p>
        )}
        {p.issuesStatus === "served" && p.issues && p.issues.length === 0 && (
          <p className="text-xs text-text-muted">
            No open {cell.severity} issue of {cell.label} was served
            {c && c.open > 0 ? ` — the board counted ${c.open}; the counts and the list are two reads` : ""}.
          </p>
        )}
        {p.issues && p.issues.length > 0 && (
          <ul className="space-y-1" aria-label="Issues in this cell">
            {p.issues.map((i) => (
              <li key={i.number}>
                <Tip content={i.title}>
                  <button
                    type="button"
                    onClick={() => p.onPickIssue(i.number)}
                    className="w-full text-left text-xs rounded-md border border-border bg-surface px-2 py-1.5 flex items-center gap-1.5 hover:bg-surface-3 min-h-7"
                  >
                    <span className="font-mono shrink-0 text-text-muted">#{i.number}</span>
                    <TextClamp text={i.title} lines={1} max={120} reach="parent" className="flex-1 min-w-0" />
                    <Flags i={i} />
                  </button>
                </Tip>
              </li>
            ))}
          </ul>
        )}
        {p.issues && c && p.issues.length > 0 && p.issuesCapped && p.issues.length < c.open && (
          <p className="mt-1.5 text-xs text-text-muted">
            <span className="tabular-nums">{p.issues.length}</span> of {c.open} shown: the read stops at {p.issuesRead}{" "}
            rows.
          </p>
        )}
      </div>
      {AREA_CLASS.map((k) => (
        <AreaClassList
          key={k.template}
          a={areaClass(p.sections, k.template)}
          label={k.label}
          hint={k.hint}
          trackerNamespace={p.trackerNamespace}
        />
      ))}
      {restrictedLine}
    </Panel>
  );
}
