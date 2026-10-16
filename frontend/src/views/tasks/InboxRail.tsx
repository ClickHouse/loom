import { Plus } from "lucide-react";
import type { Inbox, InboxCount } from "../../api/tasks";
import { initials, type Filters, type PosterFilter } from "../../lib/tasks";
import { Avatar } from "./TaskBits";

/**
 * The inbox rail: every inbox the namespace owns as a folder with "N to do · M running"
 * from `task.counts`, "All inboxes" first, "Register inbox…" last for an admin. Under it
 * the filters: kind chips from the loaded rows, "Posted by" (this agent / other agents),
 * and "Show closed". Under 820 px the rail is a horizontal strip and the filters hide
 * (the tiles still filter).
 */
export const ALL_INBOXES = "*";

const POSTER_OPTIONS: ReadonlyArray<readonly [PosterFilter, string]> = [
  ["me", "this agent"],
  ["others", "other agents"],
];

export interface RailCounts {
  todo: number;
  running: number;
}

export function railCounts(counts: readonly InboxCount[]): Map<string, RailCounts> {
  const by = new Map<string, RailCounts>();
  for (const c of counts) {
    let e = by.get(c.inbox);
    if (!e) {
      e = { todo: 0, running: 0 };
      by.set(c.inbox, e);
    }
    if (c.status === "todo") e.todo += c.n;
    else if (c.status === "in_progress") e.running += c.n;
  }
  return by;
}

function CountText({ c }: { c: RailCounts | undefined }) {
  if (!c)
    return (
      <span className="cnt" aria-hidden="true">
        …
      </span>
    );
  const parts: string[] = [];
  if (c.todo) parts.push(`${c.todo} to do`);
  if (c.running) parts.push(`${c.running} running`);
  if (parts.length === 0) return <span className="cnt">empty</span>;
  return (
    <span className="cnt tabular">
      {c.todo > 0 && (
        <>
          <b>{c.todo}</b> to do
        </>
      )}
      {c.todo > 0 && c.running > 0 && " · "}
      {c.running > 0 && (
        <>
          <b>{c.running}</b> running
        </>
      )}
    </span>
  );
}

interface Props {
  inboxes: Inbox[];
  counts: Map<string, RailCounts> | null;
  picked: string;
  onPick: (inbox: string) => void;
  kinds: string[];
  filters: Filters;
  onFilters: (f: Filters) => void;
  /** An admin's "Register inbox…" — absent for everyone else. */
  onRegister?: () => void;
}

export function InboxRail({ inboxes, counts, picked, onPick, kinds, filters, onFilters, onRegister }: Props) {
  const totals = counts
    ? [...counts.values()].reduce((a, c) => ({ todo: a.todo + c.todo, running: a.running + c.running }), {
        todo: 0,
        running: 0,
      })
    : undefined;
  const setPoster = (p: PosterFilter) => onFilters({ ...filters, poster: filters.poster === p ? null : p });
  return (
    <aside className="tq-rail" aria-label="Inboxes and filters">
      <section aria-labelledby="tq-rail-inboxes">
        <h2 id="tq-rail-inboxes" className="section-label mb-1.5 ml-2">
          Inboxes
        </h2>
        <nav aria-label="Inboxes">
          {inboxes.length > 1 && (
            <button
              type="button"
              className="tq-inbox"
              aria-current={picked === ALL_INBOXES}
              onClick={() => onPick(ALL_INBOXES)}
            >
              <span className="nm">
                <span className="tq-av" aria-hidden="true">
                  ∗
                </span>
                <span>All inboxes</span>
              </span>
              <CountText c={totals} />
            </button>
          )}
          {inboxes.map((i) => (
            <button
              key={i.inbox}
              type="button"
              className="tq-inbox"
              aria-current={picked === i.inbox}
              onClick={() => onPick(i.inbox)}
              title={i.description || i.inbox}
            >
              <span className="nm">
                <Avatar name={i.owner_agent || initials(i.inbox)} kind="bot" title={i.owner_agent} />
                <span>{i.inbox}</span>
              </span>
              <CountText c={counts?.get(i.inbox) ?? (counts ? { todo: 0, running: 0 } : undefined)} />
            </button>
          ))}
          {onRegister && (
            <button type="button" className="tq-inbox text-xs" onClick={onRegister}>
              <span className="nm">
                <span className="tq-av" aria-hidden="true">
                  <Plus size={11} />
                </span>
                <span>Register inbox…</span>
              </span>
            </button>
          )}
        </nav>
      </section>
      {kinds.length > 0 && (
        <section className="tq-filters" aria-labelledby="tq-rail-kind">
          <h2 id="tq-rail-kind" className="section-label mb-1.5 ml-2">
            Kind
          </h2>
          {/* biome-ignore lint/a11y/useSemanticElements: a labelled cluster of toggle chips, not form fields */}
          <div className="flex flex-wrap gap-1.5 px-2" id="tq-kind-chips" role="group" aria-label="Kind">
            {kinds.map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={filters.kind === k}
                onClick={() => onFilters({ ...filters, kind: filters.kind === k ? null : k })}
                className={`font-mono text-xs px-2 py-0.5 rounded-full border ${filters.kind === k ? "bg-[var(--color-text)] text-[var(--color-bg)] border-transparent" : "border-[var(--color-border-strong)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"}`}
              >
                {k}
              </button>
            ))}
          </div>
        </section>
      )}
      <section className="tq-filters" aria-labelledby="tq-rail-poster">
        <h2 id="tq-rail-poster" className="section-label mb-1.5 ml-2">
          Posted by
        </h2>
        {/* biome-ignore lint/a11y/useSemanticElements: a labelled cluster of toggle chips, not form fields */}
        <div className="flex flex-wrap gap-1.5 px-2" role="group" aria-label="Posted by">
          {POSTER_OPTIONS.map(([k, l]) => (
            <button
              key={k}
              type="button"
              aria-pressed={filters.poster === k}
              onClick={() => setPoster(k)}
              className={`text-xs px-2 py-0.5 rounded-full border ${filters.poster === k ? "bg-[var(--color-text)] text-[var(--color-bg)] border-transparent" : "border-[var(--color-border-strong)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"}`}
            >
              {l}
            </button>
          ))}
        </div>
      </section>
      <section className="tq-filters">
        <label className="flex items-center gap-2 text-sm text-[var(--color-text-muted)] px-2 py-1.5">
          <input
            type="checkbox"
            checked={filters.showClosed}
            onChange={(e) => onFilters({ ...filters, showClosed: e.target.checked })}
          />{" "}
          Show closed
        </label>
      </section>
    </aside>
  );
}
