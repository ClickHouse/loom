import { Select } from "@clickhouse/click-ui/Select";
import type { Inbox, InboxCount } from "../../api/tasks";
import { Tip } from "../../components/ui";

/**
 * The page's scope, the inbox: on a wide screen a rail of every inbox the namespace owns
 * with "N to do · M running" from `task.counts`, "All inboxes" first; on a phone the same
 * choice as one Click UI Select. Registering an inbox is in the page's options menu.
 */
export const ALL_INBOXES = "*";

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

function totalsOf(counts: Map<string, RailCounts> | null): RailCounts | undefined {
  return counts
    ? [...counts.values()].reduce((a, c) => ({ todo: a.todo + c.todo, running: a.running + c.running }), {
        todo: 0,
        running: 0,
      })
    : undefined;
}

/** "3 to do · 2 running", "empty", or "" before the counts land. */
export function countLine(c: RailCounts | undefined): string {
  if (!c) return "";
  const parts: string[] = [];
  if (c.todo) parts.push(`${c.todo} to do`);
  if (c.running) parts.push(`${c.running} running`);
  return parts.length ? parts.join(" · ") : "empty";
}

/** What the rail's folder leaves out: the inbox's description and the agent that owns it. */
function InboxTip({ inbox }: { inbox: Inbox }) {
  return (
    <>
      {inbox.description && <div>{inbox.description}</div>}
      {inbox.owner_agent && <div>Owner agent: {inbox.owner_agent}</div>}
    </>
  );
}

function CountText({ c }: { c: RailCounts | undefined }) {
  if (!c)
    return (
      <span className="cnt" aria-hidden="true">
        …
      </span>
    );
  if (!c.todo && !c.running) return <span className="cnt">empty</span>;
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
}

const countsOf = (counts: Map<string, RailCounts> | null, inbox: string) =>
  counts?.get(inbox) ?? (counts ? { todo: 0, running: 0 } : undefined);

export function InboxRail({ inboxes, counts, picked, onPick }: Props) {
  return (
    <div className="tq-rail">
      <h2 id="tq-rail-inboxes" className="section-label mb-1.5 ml-2">
        Inboxes
      </h2>
      <nav aria-labelledby="tq-rail-inboxes">
        {inboxes.length > 1 && (
          <button
            type="button"
            className="tq-inbox"
            aria-current={picked === ALL_INBOXES}
            onClick={() => onPick(ALL_INBOXES)}
          >
            <span className="nm">All inboxes</span>
            <CountText c={totalsOf(counts)} />
          </button>
        )}
        {inboxes.map((i) => (
          <Tip key={i.inbox} content={<InboxTip inbox={i} />} side="right" disabled={!i.description && !i.owner_agent}>
            <button
              type="button"
              className="tq-inbox"
              aria-current={picked === i.inbox}
              onClick={() => onPick(i.inbox)}
            >
              <span className="nm">{i.inbox}</span>
              <CountText c={countsOf(counts, i.inbox)} />
            </button>
          </Tip>
        ))}
      </nav>
    </div>
  );
}

/** The rail on a phone: one Select, its options carrying the same counts. */
export function InboxSelect({ inboxes, counts, picked, onPick }: Props) {
  const label = (name: string, c: RailCounts | undefined) => {
    const line = countLine(c);
    return line ? `${name} · ${line}` : name;
  };
  return (
    // The options name inboxes with their counts, so the field needs no visible label.
    <Select value={picked} onSelect={onPick} triggerProps={{ "aria-label": "Inbox" }}>
      {inboxes.length > 1 && <Select.Item value={ALL_INBOXES}>{label("All inboxes", totalsOf(counts))}</Select.Item>}
      {inboxes.map((i) => (
        <Select.Item key={i.inbox} value={i.inbox}>
          {label(i.inbox, countsOf(counts, i.inbox))}
        </Select.Item>
      ))}
    </Select>
  );
}
