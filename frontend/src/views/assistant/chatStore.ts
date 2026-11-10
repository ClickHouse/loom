// The conversation the assistant panel shows, and its answer while it streams. It lives
// outside the panel's components: closing the panel, a resize that changes its mode or a
// navigation never stops an answer. Saved to this browser at turn boundaries and when the
// page hides (./conversations.ts).
import { create } from "zustand";
import { type ChatMessage, streamAssistantChat } from "../../api/assistantChat";
import { ApiError } from "../../lib/errors";
import { humanizeError } from "../../lib/errorWords";
import type { Owner } from "../../lib/assistantStorage";
import { useDock } from "../../lib/assistantDock";
import { SHORT_REASON, type AssistantReason } from "../../lib/assistantLayout";
import { announceAssistant } from "../../lib/assistantDockRuntime";
import { supportLabel } from "./Sources";
import {
  ConversationDeleted,
  StorageRefused,
  deleteConversation,
  mergeTurns,
  randomId,
  readConversation,
  saveConversation,
  type Conversation,
  type Turn,
} from "./conversations";

export type { Turn } from "./conversations";

/** The reason a refusal before the stream names, when it is one the panel words as the
 *  assistant being unavailable (the route's `detail.reason`, server/assistant/app). */
export function refusalReason(e: unknown): AssistantReason | null {
  if (!(e instanceof ApiError) || e.status === 0) return null;
  const reason = typeof e.detail.reason === "string" ? e.detail.reason : "";
  if (e.code === "MODEL_CALLS_DISABLED") return { kind: "model_calls_off" };
  if (e.code === "ASSISTANT_UNAVAILABLE") {
    if (reason === "auth_off" || reason === "starting" || reason === "failing") return { kind: reason };
    // A refusal that names no reason (an older server) reads as not set up: the route says
    // the assistant cannot answer, and an admin is who fixes it.
    return { kind: "not_configured" };
  }
  if (e.code === "AUTH_INSUFFICIENT") {
    if (reason === "org_wide") return { kind: "org_wide" };
    if (reason === "narrowed") return { kind: "narrowed", scope: "less than the namespace" };
    if (reason === "token_expires_soon")
      return typeof e.detail.left_s === "number"
        ? { kind: "token_expires_soon", leftS: Math.max(0, Math.round(e.detail.left_s)) }
        : { kind: "token_expires_soon" };
  }
  return null;
}

const isAbort = (e: unknown) => e instanceof DOMException && e.name === "AbortError";

// What one request may carry (server/assistant/api/models.py): the earlier messages, newest
// kept, and each message's length.
const MAX_HISTORY = 50;
export const MAX_MESSAGE_CHARS = 20_000;

/** The messages a new question goes with: the turns shown, but a question refused before
 *  any answer (it was never answered, and the person asks again) and no empty answer; the
 *  newest `MAX_HISTORY`, each cut to `MAX_MESSAGE_CHARS`. */
export function conversation(turns: Turn[], question: string): ChatMessage[] {
  const history: ChatMessage[] = [];
  for (const t of turns) {
    if (t.state === "refused") continue;
    history.push({ role: "user", content: t.question.slice(0, MAX_MESSAGE_CHARS) });
    if (t.answer.trim()) history.push({ role: "assistant", content: t.answer.slice(0, MAX_MESSAGE_CHARS) });
  }
  return [...history.slice(-MAX_HISTORY), { role: "user", content: question }];
}

interface ChatState {
  /** Whose conversations these are; null until the panel knows (or with auth off). */
  owner: Owner | null;
  /** The conversation shown; null for a new one nothing has been asked in. */
  id: string | null;
  namespace: string | null;
  turns: Turn[];
  trimmed: boolean;
  answering: boolean;
  /** A line about the conversation shown: another tab answers in it, or it was deleted. */
  notice: string | null;
  /** This browser refused to store conversations: this one lasts as long as the tab. */
  storageOff: boolean;
  /** A question the route refused for a reason the panel shows (Try again asks it again). */
  refused: string | null;
  /** A reason the route's refusal named: a tab-lifetime hint, cleared by an answer or by
   *  Try again. */
  hint: AssistantReason | null;
  /** The reason the readiness read gave, null when it reported none or failed. */
  readiness: AssistantReason | null;
}

/** Said about one Ask refused because another tab held the conversation's lock: it goes with
 *  the next Ask, an edit of the question, or once no stored turn is still answering. */
export const OTHER_TAB = "Another tab was answering in this conversation. Ask again once it ends.";
export const DELETED = "This conversation was deleted: it is no longer saved.";

const blank = { id: null, namespace: null, turns: [] as Turn[], trimmed: false, notice: null };

export const useChat = create<ChatState>(() => ({
  owner: null,
  ...blank,
  answering: false,
  storageOff: false,
  refused: null,
  hint: null,
  readiness: null,
}));

/** The answer running in this tab; a callback of any other answer changes nothing shared. */
let controller: AbortController | null = null;

export const lockName = (id: string) => `loom_assistant_c_${id}`;

// The Web Locks API: a conversation's lock is held while one tab answers in it, and let go
// when the answer ends or the tab dies. Without it (an insecure context) each tab answers
// on its own, and every save merges the stored turns first.
const webLocks = () => (typeof navigator !== "undefined" ? navigator.locks : undefined);

function current(turns: Turn[]): Conversation | null {
  const s = useChat.getState();
  if (!s.id || !s.owner || !s.namespace) return null;
  const first = turns[0]?.question ?? "";
  return {
    id: s.id,
    owner: s.owner,
    namespace: s.namespace,
    title: first.replace(/\s+/g, " ").trim().slice(0, 80),
    turns,
    trimmed: s.trimmed,
    updated: Date.now(),
  };
}

/** Merge the stored copy's finished turns into the ones shown (another tab may have saved
 *  one since); `stopped`: this tab holds the lock, so a stored turn still answering was cut
 *  off and joins as stopped. Answers whether the stored copy has a turn still answering. */
function mergeStored(answering: "omit" | "stopped" = "omit"): boolean {
  const s = useChat.getState();
  if (!s.id || !s.owner) return false;
  const stored = readConversation(s.id, s.owner, { answering: "kept" });
  if (!stored) return false;
  const theirs = stored.turns.flatMap((t) =>
    t.state !== "answering" ? [t] : answering === "stopped" ? [{ ...t, state: "stopped" as const }] : [],
  );
  useChat.setState({ turns: mergeTurns(s.turns, theirs) });
  return stored.turns.some((t) => t.state === "answering");
}

/** Save the conversation shown, merged with the stored copy first, unless storage is
 *  refused here. A stored turn still answering that this tab does not hold is written back
 *  as it is (another tab may be answering it). `held`: the lock holder's save, where such a
 *  turn was cut off and is kept as stopped; `drop`: the holder's turn taken back. */
export function save({ held = false, drop }: { held?: boolean; drop?: string } = {}): void {
  const s = useChat.getState();
  if (!s.id || !s.owner || s.storageOff) return;
  const stored = readConversation(s.id, s.owner, { answering: held ? "stopped" : "kept" });
  const theirs = (stored?.turns ?? []).filter((t) => t.id !== drop);
  // Shown: the finished turns; written: those and the stored turns still answering.
  const shown = mergeTurns(
    s.turns,
    theirs.filter((t) => t.state !== "answering"),
  );
  useChat.setState({ turns: shown });
  const c = current(mergeTurns(shown, theirs));
  if (!c) return;
  try {
    saveConversation(c);
  } catch (e) {
    if (e instanceof StorageRefused) useChat.setState({ storageOff: true });
    if (e instanceof ConversationDeleted) useChat.setState({ notice: DELETED });
  }
}

/** Whether a stored turn still answering was cut off: no tab holds its conversation's lock
 *  (a reload, a closed tab). Without the Locks API there is no holder to ask: it is not
 *  taken as cut off here (one older than ANSWER_MAX_MS reads as stopped in any case,
 *  conversations.ts). */
async function cutOff(id: string): Promise<boolean> {
  const locks = webLocks();
  if (!locks?.query) return false;
  try {
    const state = await locks.query();
    return !state.held?.some((l) => l.name === lockName(id));
  } catch {
    return true;
  }
}

/** Show the stored conversation `id`. A turn another tab is still answering is left out
 *  until it ends; one no tab answers any more, or older than ANSWER_MAX_MS, shows as
 *  stopped. */
export async function openConversation(id: string): Promise<boolean> {
  const owner = useChat.getState().owner;
  if (useChat.getState().answering || !owner) return false;
  const stopped = await cutOff(id);
  const s = useChat.getState();
  if (s.answering || s.owner !== owner) return false;
  const c = readConversation(id, owner, { answering: stopped ? "stopped" : "omit" });
  if (!c) return false;
  useChat.setState({ ...blank, id: c.id, namespace: c.namespace, turns: c.turns, trimmed: c.trimmed });
  return true;
}

/** Another tab wrote the conversation shown: its finished turns join the ones here, and
 *  once none is still answering, the note that another tab answers goes. While this tab
 *  answers, nothing changes until its own save. */
export function storedChanged(): void {
  if (useChat.getState().answering) return;
  const answeringThere = mergeStored();
  if (!answeringThere && useChat.getState().notice === OTHER_TAB) useChat.setState({ notice: null });
}

/** Start a new conversation (the one shown stays in History). */
export function newConversation(): void {
  if (useChat.getState().answering) return;
  useChat.setState({ ...blank });
}

/** Forget what is shown: the person changed, the session ended, or a conversation was
 *  deleted. An answer streaming stops, and nothing it does later reaches this state. */
export function resetChat(owner: Owner | null): void {
  controller?.abort();
  controller = null;
  // A refusal's reason was about the owner's token: it goes with them.
  useChat.setState({ owner, ...blank, answering: false, refused: null, hint: null });
  useDock.setState({ working: false });
}

/** Ask `question`: in the conversation's namespace, or in `namespace` when a new one starts.
 *  The conversation's lock is taken first, only if no other tab holds it; when one does,
 *  nothing is sent and the question stays in the field. */
export function ask(question: string, namespace: string): void {
  const s = useChat.getState();
  if (s.answering) return;
  const id = s.id ?? randomId();
  const ns = s.namespace ?? namespace;
  const mine = new AbortController();
  controller = mine;
  // Answering from here on, so a second Ask waits; the turn shows once the lock is held.
  useChat.setState({ answering: true, notice: null });
  useDock.setState({ working: true });
  const run = (held: boolean) => answer(id, ns, question, mine, held);
  const locks = webLocks();
  if (!locks?.request) {
    void run(false);
    return;
  }
  let granted = false;
  locks
    .request(lockName(id), { ifAvailable: true }, (lock) => {
      if (!lock) return otherTab(mine);
      granted = true;
      // The lock is held until the answer ends.
      return run(true);
    })
    .catch(() => {
      // The Locks API refused the request itself: the answer goes ahead without a lock.
      if (!granted) void run(false);
    });
}

/** The end of an answer's hold on the shared state, when it is still this tab's answer. */
function settle(mine: AbortController): boolean {
  if (controller !== mine) return false;
  controller = null;
  useChat.setState({ answering: false });
  useDock.setState({ working: false });
  return true;
}

/** Another tab holds the conversation's lock: nothing is asked. */
function otherTab(mine: AbortController): void {
  if (!settle(mine)) return;
  useChat.setState({ notice: OTHER_TAB });
  announceAssistant(OTHER_TAB);
  // That tab's final save may have landed while the lock was asked for, its storage event
  // heard while this tab was answering: merge it now. The note stays until the next Ask,
  // an edit of the question or a storage event, so the refusal is said on screen.
  mergeStored();
}

/** The question was edited: a note about the last Ask refused no longer applies. */
export function draftEdited(): void {
  if (useChat.getState().notice === OTHER_TAB) useChat.setState({ notice: null });
}

/** One answer, from its turn's start to its end; it never rejects. `held`: this tab holds
 *  the conversation's lock, so no other tab answers in it. */
async function answer(
  id: string,
  namespace: string,
  question: string,
  mine: AbortController,
  held: boolean,
): Promise<void> {
  // Stopped, or the chat reset, before the lock came: nothing is asked.
  if (mine.signal.aborted || controller !== mine) {
    settle(mine);
    return;
  }
  const turnId = randomId();
  // What another tab finished in this conversation goes with the question.
  useChat.setState({ id, namespace, refused: null });
  mergeStored(held ? "stopped" : "omit");
  const before = useChat.getState().turns;
  const messages = conversation(before, question);
  useChat.setState({
    turns: [...before, { id: turnId, question, answer: "", state: "answering", at: Date.now() }],
  });
  useDock.setState({ draft: "" });
  announceAssistant("The assistant is answering.");
  // Saved at its start: a reload mid-answer shows it as stopped. Other tabs leave a turn
  // still answering out until it ends.
  save({ held });
  const update = (change: (t: Turn) => Partial<Turn>) => {
    if (controller !== mine) return;
    useChat.setState((st) => ({
      turns: st.turns.map((t) => (t.id === turnId ? { ...t, ...change(t), at: Date.now() } : t)),
    }));
  };
  let streamed = false;
  // The label, said with the end of the answer.
  let labelled = "";
  const end = (change: Partial<Turn>, said: string) => {
    update(() => change);
    if (!settle(mine)) return;
    save({ held });
    const unread = !useDock.getState().open && change.state === "done";
    if (unread) {
      useDock.setState({ unread: true });
      announceAssistant("Assistant answer ready");
    } else if (said) announceAssistant(said);
  };
  try {
    await streamAssistantChat(
      namespace,
      messages,
      {
        onText: (text) => {
          streamed = true;
          update((t) => ({ answer: t.answer + text }));
        },
        onCitations: (citations, support, unknownNames) => {
          labelled = supportLabel(support, citations, unknownNames)?.text ?? "";
          update(() => ({ citations, support, unknownNames }));
        },
      },
      mine.signal,
    );
    // An answer came: whatever the route refused before no longer holds.
    if (controller === mine) useChat.setState({ hint: null });
    end({ state: "done" }, `The answer is complete.${labelled ? ` ${labelled}.` : ""}`);
  } catch (e: unknown) {
    if (isAbort(e)) return end({ state: "stopped" }, "The answer was stopped.");
    // A refusal before the stream started is an HTTP error (status set); one inside the
    // stream is an error event (status 0) after part of the answer.
    const refusedBefore = e instanceof ApiError && e.status !== 0 && !streamed;
    const reason = refusedBefore ? refusalReason(e) : null;
    if (reason) return takeBack(id, turnId, question, reason, mine, held);
    // A refused question was never answered: it waits in the field to be asked again.
    if (refusedBefore && controller === mine && !useDock.getState().draft) useDock.setState({ draft: question });
    end({ state: refusedBefore ? "refused" : "failed", error: humanizeError(e, { read: true }) }, "");
  }
}

/** The route refused before the answer for a reason the panel shows: the turn is taken
 *  back (never saved as finished, so no tab keeps it), the question waits in the field, and
 *  Try again asks it. A conversation whose only question this was is deleted, and the next
 *  question starts a new one in the header's namespace. */
function takeBack(
  id: string,
  turnId: string,
  question: string,
  reason: AssistantReason,
  mine: AbortController,
  held: boolean,
): void {
  if (controller !== mine) return;
  const turns = useChat.getState().turns.filter((t) => t.id !== turnId);
  useChat.setState({ turns, hint: reason, refused: question });
  if (!useDock.getState().draft) useDock.setState({ draft: question });
  const owner = useChat.getState().owner;
  const stored = owner ? readConversation(id, owner, { answering: "kept" }) : null;
  // Nothing else in it, here or stored (a turn another tab answers without a lock counts).
  if (turns.length === 0 && !stored?.turns.some((t) => t.id !== turnId)) {
    deleteConversation(id);
    useChat.setState({ id: null, namespace: null, trimmed: false });
  } else save({ held, drop: turnId });
  settle(mine);
  announceAssistant(`The assistant can't answer: ${SHORT_REASON[reason.kind]}.`);
}

export function stop(): void {
  controller?.abort();
}

/** The page hides (a reload, a closed tab, a switch away on a phone): what is on screen is
 *  saved, an answer mid-stream as it stands. */
export function saveOnHide(): void {
  if (useChat.getState().turns.length > 0) save();
}
