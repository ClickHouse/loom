// What the header bell computes from `task.counts`. Its own module so the shell (index
// chunk) carries these two functions and not the board's whole vocabulary in
// `lib/tasks.ts`.
import type { InboxCount } from "../api/tasks";

function waitsOnPerson(c: InboxCount): boolean {
  return c.status === "triage" || (c.status === "blocked" && c.blocked_on === "human");
}

/** The bell's number: review items in triage plus tasks blocked on a
 *  person, over the namespace's inboxes. A task blocked on its sender or
 *  an external event waits on someone else and is not counted. */
export function bellCount(counts: readonly InboxCount[]): number {
  return counts.reduce((n, c) => n + (waitsOnPerson(c) ? c.n : 0), 0);
}

export interface InboxAttention {
  inbox: string;
  triage: number;
  /** Blocked on a person — in the badge. */
  blockedHuman: number;
  /** Blocked on the sender or an external event — listed, not counted. */
  blockedOther: number;
  oldest: string | null;
}

/** Per inbox, what the bell lists — inboxes with nothing waiting on a
 *  person are left out, even when they hold tasks blocked on a sender. */
export function attentionByInbox(counts: readonly InboxCount[]): InboxAttention[] {
  const by = new Map<string, InboxAttention>();
  for (const c of counts) {
    if (c.status !== "triage" && c.status !== "blocked") continue;
    let a = by.get(c.inbox);
    if (!a) {
      a = { inbox: c.inbox, triage: 0, blockedHuman: 0, blockedOther: 0, oldest: null };
      by.set(c.inbox, a);
    }
    if (c.status === "triage") a.triage += c.n;
    else if (c.blocked_on === "human") a.blockedHuman += c.n;
    else a.blockedOther += c.n;
    if (waitsOnPerson(c) && c.oldest && (!a.oldest || c.oldest < a.oldest)) a.oldest = c.oldest;
  }
  return [...by.values()].filter((a) => a.triage + a.blockedHuman > 0).sort((x, y) => x.inbox.localeCompare(y.inbox));
}
