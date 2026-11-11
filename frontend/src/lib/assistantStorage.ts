// The assistant's conversations in this browser's storage: the keys and the few reads and
// clears the shell itself makes (a sign-out, another person signing in). The conversations
// themselves are read and written by the panel's chunk (views/assistant/conversations.ts).
// The panel's own preferences (open, width) have a key here too.
//
// Layout: one key per conversation (`loom_assistant_c_<id>`), an index of them
// (`loom_assistant_index`: each entry's id, owner, namespace, title, time and size, plus
// the tombstones of deleted ids) and a shared epoch (`loom_assistant_epoch`),
// bumped by a sign-out, another person signing in and Delete all: a tab whose epoch is
// older writes nothing.

export const INDEX_KEY = "loom_assistant_index";
/** The panel's own preferences, kept per browser: whether it was open and its width. */
export const PANEL_KEY = "loom_assistant_panel";
/** Whether this browser has shown the panel's one-time line about the page chip. */
export const PAGE_HINT_KEY = "loom_assistant_page_hint";
export const EPOCH_KEY = "loom_assistant_epoch";
export const CONVERSATION_PREFIX = "loom_assistant_c_";

/** Whose conversation it is: the person, their org, and the read scope and read user of the
 *  token it was asked with ("-" for none), so a narrower token never lists a wider one's. */
export interface Owner {
  subject: string;
  org: string;
  scope: string;
  user: string;
}

export interface IndexEntry {
  id: string;
  owner: Owner;
  namespace: string;
  title: string;
  /** Last use, ms since the epoch. */
  updated: number;
  /** The stored conversation's length in characters. */
  bytes: number;
}

export interface StoredIndex {
  entries: IndexEntry[];
  /** Deleted ids and when: a write for one of them is refused, here and in every tab. */
  tombstones: Record<string, number>;
}

export function readEpoch(): number {
  try {
    const n = Number(localStorage.getItem(EPOCH_KEY) || "0");
    return Number.isFinite(n) ? n : 0;
  } catch {
    return 0;
  }
}

/** Bump the shared epoch: every tab's later write is refused until it reloads its view.
 *  Whoever bumps it writes the index without tombstones (the ids they name are gone). */
export function bumpEpoch(): number {
  const next = readEpoch() + 1;
  try {
    localStorage.setItem(EPOCH_KEY, String(next));
  } catch {
    /* refused: nothing stored to protect */
  }
  return next;
}

function storedKeys(): string[] {
  const keys: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k?.startsWith(CONVERSATION_PREFIX)) keys.push(k);
    }
  } catch {
    /* refused: nothing stored */
  }
  return keys;
}

/** Delete every stored conversation (a sign-out): the epoch first, so a tab still answering
 *  cannot write one back. */
export function forgetAssistantConversations(): void {
  bumpEpoch();
  try {
    for (const k of storedKeys()) localStorage.removeItem(k);
    localStorage.removeItem(INDEX_KEY);
  } catch {
    /* refused: nothing stored */
  }
}
