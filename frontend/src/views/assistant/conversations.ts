// The assistant's conversations, kept in this browser (lib/assistantStorage.ts has the
// layout). Loom's server stores none of them. The rules, each enforced here:
//   - an entry lists only for its owner (subject, org, token read scope and read user);
//   - 30 days from last use, then deleted; at most 25 turns a conversation (the 50
//     messages one request sends), 256 KB a conversation and 2 MB in all, oldest first;
//   - written at turn boundaries and when the page hides, never while tokens stream;
//   - every write first checks the shared epoch is this tab's, the token is still the
//     page's person's, and the id has no tombstone, so a write that lands after a
//     sign-out, another person's sign-in, a Delete all or a Delete cannot bring one back.
import { isAuthDisabled, isProxyMode, ownsToken, storedToken } from "../../api";
import {
  CONVERSATION_PREFIX,
  INDEX_KEY,
  bumpEpoch,
  readEpoch,
  type IndexEntry,
  type Owner,
  type StoredIndex,
} from "../../lib/assistantStorage";
import { readIndex } from "./storedIndex";
import type { Citation, Support } from "../../api/assistantChat";

export type TurnState = "answering" | "done" | "stopped" | "failed" | "refused";

export interface Turn {
  id: string;
  question: string;
  answer: string;
  state: TurnState;
  error?: string;
  /** The answer's citations as the server checked them; absent until the answer ends, or
   *  from a server that sends none. */
  citations?: Citation[];
  /** How far the answer rests on Loom; absent until the answer ends. */
  support?: Support;
  /** Op and setting names the answer wrote that the server neither serves nor documents. */
  unknownNames?: string[];
  /** When the turn last changed (ms): two tabs' copies of one turn merge to the newer. */
  at: number;
}

export interface Conversation {
  id: string;
  owner: Owner;
  namespace: string;
  title: string;
  turns: Turn[];
  /** Older turns were dropped from the stored copy. */
  trimmed: boolean;
  updated: number;
}

export const KEPT_DAYS = 30;
const KEPT_MS = KEPT_DAYS * 24 * 3600 * 1000;
export const KEPT_TURNS = 25;
const MAX_CONVERSATION = 256 * 1024;
const MAX_TOTAL = 2 * 1024 * 1024;

/** 16 random bytes as hex: getRandomValues, which works outside secure contexts too. */
export function randomId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export const sameOwner = (a: Owner, b: Owner) =>
  a.subject === b.subject && a.org === b.org && a.scope === b.scope && a.user === b.user;

/** The epoch this tab writes under: taken when it first reads, and again when it reloads
 *  its view after another tab bumped it. */
let tabEpoch = readEpoch();
/** The proxy's subject this tab writes as (behind a proxy there is no token to check). */
let tabSubject: string | null = null;

export function adoptEpoch(subject: string | null): void {
  tabEpoch = readEpoch();
  tabSubject = subject;
}

/** Whether this tab may write now. */
function mayWrite(owner: Owner): boolean {
  if (isAuthDisabled()) return false;
  if (readEpoch() !== tabEpoch) return false;
  if (isProxyMode()) return tabSubject === owner.subject;
  const t = storedToken();
  return !!t && ownsToken(t);
}

/** Storage refused a write that freeing space did not fix: the chat stays in memory. */
export class StorageRefused extends Error {}

function writeIndex(index: StoredIndex): void {
  localStorage.setItem(INDEX_KEY, JSON.stringify(index));
}

/** The index without what has expired, deleting the expired conversations' keys too. */
function liveIndex(now = Date.now()): StoredIndex {
  const index = readIndex();
  const dead = index.entries.filter((e) => now - e.updated > KEPT_MS);
  const tombstones = Object.fromEntries(Object.entries(index.tombstones).filter(([, at]) => now - at <= KEPT_MS));
  if (dead.length === 0 && Object.keys(tombstones).length === Object.keys(index.tombstones).length) return index;
  const live = { entries: index.entries.filter((e) => !dead.includes(e)), tombstones };
  try {
    for (const e of dead) localStorage.removeItem(CONVERSATION_PREFIX + e.id);
    writeIndex(live);
  } catch {
    /* refused: read as live, deleted on a later write */
  }
  return live;
}

/** The conversations `owner` may see, newest first: only theirs, and of those only the
 *  namespaces `readable` says the token reads. */
export function listConversations(owner: Owner, readable: (namespace: string) => boolean): IndexEntry[] {
  return liveIndex()
    .entries.filter((e) => sameOwner(e.owner, owner) && readable(e.namespace))
    .sort((a, b) => b.updated - a.updated);
}

/** A stored turn still answering after this long was cut off, whatever a lock says or
 *  where there are none: longer than any answer streams. */
export const ANSWER_MAX_MS = 60 * 60 * 1000;

/** What a read does with a turn saved while it was answering: `omit` it (another tab may
 *  still answer it; it shows once it ends), read it as `stopped` (no tab answers in the
 *  conversation any more: a reload or a closed tab cut it off), or keep it as stored (a save
 *  writes it back untouched). One older than ANSWER_MAX_MS reads as stopped in every mode. */
export type AnsweringTurns = "omit" | "stopped" | "kept";

/** The stored conversation `id`, when it is `owner`'s. */
export function readConversation(
  id: string,
  owner: Owner,
  { answering = "omit" }: { answering?: AnsweringTurns } = {},
): Conversation | null {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(CONVERSATION_PREFIX + id) || "null");
    if (!raw || typeof raw !== "object") return null;
    const c = raw as Conversation;
    if (c.id !== id || !c.owner || !sameOwner(c.owner, owner) || !Array.isArray(c.turns)) return null;
    return {
      ...c,
      trimmed: c.trimmed === true,
      turns: c.turns
        .map((t) =>
          t.state === "answering" && (answering === "stopped" || Date.now() - t.at > ANSWER_MAX_MS)
            ? { ...t, state: "stopped" as const }
            : t,
        )
        .filter((t) => answering !== "omit" || t.state !== "answering"),
    };
  } catch {
    return null;
  }
}

/** The stored copy: the newest KEPT_TURNS turns, then older ones dropped until it fits. */
function storedCopy(c: Conversation): { text: string; copy: Conversation } {
  let turns = c.turns.slice(-KEPT_TURNS);
  let trimmed = c.trimmed || turns.length < c.turns.length;
  for (;;) {
    const copy = { ...c, turns, trimmed };
    const text = JSON.stringify(copy);
    if (text.length <= MAX_CONVERSATION || turns.length <= 1) return { text, copy };
    turns = turns.slice(1);
    trimmed = true;
  }
}

/** The conversation was deleted (here or in another tab): it is never written again. */
export class ConversationDeleted extends Error {}

/** Save `c`, under the rules above. Answers false when the write was refused because the
 *  person changed, throws ConversationDeleted for a deleted id, and StorageRefused when the
 *  browser refused it even after the oldest conversations made room. */
export function saveConversation(c: Conversation): boolean {
  if (c.turns.length === 0 || !mayWrite(c.owner)) return false;
  const index = liveIndex();
  if (index.tombstones[c.id] !== undefined) throw new ConversationDeleted();
  const { text } = storedCopy(c);
  const entry: IndexEntry = {
    id: c.id,
    owner: c.owner,
    namespace: c.namespace,
    title: c.title,
    updated: c.updated,
    bytes: text.length,
  };
  let entries = [entry, ...index.entries.filter((e) => e.id !== c.id)];
  // The newest first; the oldest go while the whole is over its cap.
  entries.sort((a, b) => b.updated - a.updated);
  const dropped: IndexEntry[] = [];
  while (entries.length > 1 && entries.reduce((n, e) => n + e.bytes, 0) > MAX_TOTAL) {
    const last = entries[entries.length - 1];
    if (last.id === c.id) break;
    dropped.push(last);
    entries = entries.slice(0, -1);
  }
  const attempt = () => {
    for (const e of dropped) localStorage.removeItem(CONVERSATION_PREFIX + e.id);
    localStorage.setItem(CONVERSATION_PREFIX + c.id, text);
    writeIndex({ entries, tombstones: index.tombstones });
  };
  try {
    attempt();
    return true;
  } catch {
    // The browser's own quota: the oldest other conversation makes room, once.
    const oldest = entries.filter((e) => e.id !== c.id).pop();
    if (!oldest) throw new StorageRefused();
    dropped.push(oldest);
    entries = entries.filter((e) => e !== oldest);
    try {
      attempt();
      return true;
    } catch {
      throw new StorageRefused();
    }
  }
}

/** Delete one conversation: its key goes and its id is tombstoned, so a tab still holding
 *  it can never write it back. */
export function deleteConversation(id: string): void {
  try {
    const index = liveIndex();
    localStorage.removeItem(CONVERSATION_PREFIX + id);
    writeIndex({
      entries: index.entries.filter((e) => e.id !== id),
      tombstones: { ...index.tombstones, [id]: Date.now() },
    });
  } catch {
    /* refused: nothing stored */
  }
}

/** Delete every conversation `owner` has here, and bump the epoch: other tabs stop
 *  writing and reload their view. */
export function deleteAllConversations(owner: Owner): void {
  bumpEpoch();
  try {
    const index = readIndex();
    const mine = index.entries.filter((e) => sameOwner(e.owner, owner));
    for (const e of mine) localStorage.removeItem(CONVERSATION_PREFIX + e.id);
    writeIndex({ entries: index.entries.filter((e) => !mine.includes(e)), tombstones: {} });
  } catch {
    /* refused */
  }
  tabEpoch = readEpoch();
}

/** Two copies of one conversation, merged turn by turn (the newer copy of each turn wins,
 *  in the order the turns were asked). */
export function mergeTurns(mine: Turn[], theirs: Turn[]): Turn[] {
  const byId = new Map<string, Turn>();
  const order: string[] = [];
  for (const t of [...theirs, ...mine]) {
    const had = byId.get(t.id);
    if (!had) order.push(t.id);
    if (!had || t.at >= had.at) byId.set(t.id, t);
  }
  return order.map((id) => byId.get(id)).filter((t): t is Turn => !!t);
}
