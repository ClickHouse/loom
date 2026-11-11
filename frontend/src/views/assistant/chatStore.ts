// The conversation the assistant panel shows, and its answer while it streams. It lives
// outside the panel's components: closing the panel, a resize that changes its mode or a
// navigation never stops an answer. Saved to this browser at turn boundaries and when the
// page hides (./conversations.ts).
import { create } from "zustand";
import { type ChatMessage, type Support, streamAssistantChat } from "../../api/assistantChat";
import { ApiError } from "../../lib/errors";
import { humanizeError } from "../../lib/errorWords";
import type { Owner } from "../../lib/assistantStorage";
import { useDock } from "../../lib/assistantDock";
import { SHORT_REASON, type AssistantReason } from "../../lib/assistantLayout";
import { announceAssistant } from "../../lib/assistantDockRuntime";
import { NOT_CHECKED, supportLabel } from "./Sources";
import { withContext, type AskContext, type Asked } from "./pageContext";
import type { TurnCard } from "./cards/types";
import {
  ConversationDeleted,
  StorageRefused,
  deleteConversation,
  cutOffTurn,
  mergeTurns,
  randomId,
  readConversation,
  saveConversation,
  type Conversation,
  type Turn,
} from "./conversations";

export type { Turn } from "./conversations";

/** A question refused for a reason the panel shows, and how Try again asks it. */
type Refused = {
  question: string;
  /** The page line a turn asked again was asked with: Try again sends it, not the chip's. */
  asked?: Asked;
} & Pick<AskOptions, "replace" | "withSources">;

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

/** Whether the same question sent again may be answered: the server marks the error
 *  retryable, the stream lost its connection, or the request never reached Loom (fetch's
 *  own TypeError). */
function retryableError(e: unknown): boolean {
  if (e instanceof ApiError) return e.code === "STREAM_ENDED" || e.detail.retryable === true;
  return e instanceof TypeError;
}

/** What "Ask again with sources" adds to the question. */
export const CITE_SOURCES = " Cite your sources.";

/** The question as it is sent: with CITE_SOURCES when it was asked with sources. */
export const sentQuestion = (question: string, withSources = false) =>
  withSources ? `${question}${CITE_SOURCES}` : question;

// What one request may carry (server/assistant/api/models.py): the earlier messages, newest
// kept, and each message's length.
const MAX_HISTORY = 50;
export const MAX_MESSAGE_CHARS = 20_000;

/** The messages a new question goes with: the turns shown, but a question refused before
 *  any answer (it was never answered, and the person asks again) and no empty answer; the
 *  newest `MAX_HISTORY`, each cut to `MAX_MESSAGE_CHARS`. Each question is sent as it was
 *  asked: with the context line it carried. A starter chosen without asking the assistant
 *  (its card alone) was never asked. */
export function conversation(turns: Turn[], question: string, context: AskContext | null = null): ChatMessage[] {
  const history: ChatMessage[] = [];
  for (const t of turns) {
    if (t.state === "refused" || t.state === "card") continue;
    const sent = sentQuestion(t.question, t.withSources);
    history.push({ role: "user", content: withContext(sent, t.context).slice(0, MAX_MESSAGE_CHARS) });
    if (t.answer.trim()) history.push({ role: "assistant", content: t.answer.slice(0, MAX_MESSAGE_CHARS) });
  }
  return [...history.slice(-MAX_HISTORY), { role: "user", content: withContext(question, context) }];
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
  /** A question the route refused for a reason the panel shows: Try again asks it again,
   *  answering `replace` again in place when it was a turn asked again, with CITE_SOURCES
   *  when it was asked with them. */
  refused: Refused | null;
  /** A reason the route's refusal named: a tab-lifetime hint, cleared by an answer or by
   *  Try again. */
  hint: AssistantReason | null;
  /** The reason the readiness read gave, null when it reported none or failed. */
  readiness: AssistantReason | null;
  /** The last question is in the field to be edited: Ask answers that turn again;
   *  `withSources`: Ask adds CITE_SOURCES. An edit starts only in an empty field. */
  editing: { id: string; withSources?: boolean } | null;
  /** The turn whose text is complete while this tab checks its sources (`loom.text_done`
   *  came, `loom.citations` not yet): never stored. */
  checkingSources: string | null;
}

/** Said about one Ask refused because another tab held the conversation's lock: it goes with
 *  the next Ask, an edit of the question, or once no stored turn is still answering. */
export const OTHER_TAB = "Another tab was answering in this conversation. Ask again once it ends.";
export const DELETED = "This conversation was deleted: it is no longer saved.";

// A refusal belongs to its conversation: Try again never asks it in another.
const blank = {
  id: null,
  namespace: null,
  turns: [] as Turn[],
  trimmed: false,
  notice: null,
  editing: null,
  refused: null,
};

export const useChat = create<ChatState>(() => ({
  owner: null,
  ...blank,
  answering: false,
  storageOff: false,
  hint: null,
  readiness: null,
  checkingSources: null,
}));

/** The answer running in this tab; a callback of any other answer changes nothing shared. */
let controller: AbortController | null = null;

/** A starter card's read in flight, by turn (cards/cardStore.ts): kept here so a turn put
 *  back can stop its read without this module loading the cards. */
export const cardReads = new Map<string, AbortController>();

/** The turn asked again, as it was, while its new answer has not begun: saves write it in
 *  place of the empty turn shown, so a reload or a closed tab keeps the answer before. Other
 *  tabs see that turn finished until the new text comes; where the Locks API is, the
 *  conversation's lock still keeps them from answering in it meanwhile. */
let keptBack: Turn | null = null;

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
    t.state !== "answering" ? [t] : answering === "stopped" ? [cutOffTurn(t)] : [],
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
  // A turn whose sources this tab still checks is stored as answering, so no other tab
  // shows it or asks it again before the check ends; cut off, it reads as done.
  const written = mergeTurns(shown, theirs).map((t) =>
    t.id === keptBack?.id
      ? keptBack
      : t.id === s.checkingSources
        ? { ...t, state: "answering" as const, textDone: true as const }
        : t,
  );
  const c = current(written);
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
  keptBack = null;
  // A refusal's reason was about the owner's token: it goes with them.
  useChat.setState({ owner, ...blank, answering: false, checkingSources: null, hint: null });
  useDock.setState({ working: false });
}

/** What a starter adds to the turn it asks: its card, what to do once the turn shows (read
 *  the card), and how to add the card alone when no turn is asked (`alone`, with why). */
export interface WithCard {
  card: TurnCard;
  shown: (turnId: string) => void;
  alone: (why: string) => void;
}

/** How a question is asked. `replace`: the id of the last turn, answered again in place
 *  (Retry, an edit, "Ask again with sources"); it keeps its id, so every tab's copy of the
 *  conversation takes the new answer. `withSources`: asked with CITE_SOURCES. `fromField`:
 *  the question came from the field, which empties once it is asked (by default, a
 *  question with no card). */
export interface AskOptions {
  replace?: string;
  withSources?: boolean;
  fromField?: boolean;
}

/** Ask `question`, with what the panel's chip says about the page (`asked`): in the
 *  conversation's namespace, or in `namespace` when a new one starts. The conversation's
 *  lock is taken first, only if no other tab holds it; when one does, nothing is sent and
 *  the question stays in the field. A starter's turn carries its card (`withCard`). */
export function ask(
  question: string,
  namespace: string,
  asked: Asked = { context: null, item: null },
  withCard: WithCard | null = null,
  opts: AskOptions = {},
): void {
  const s = useChat.getState();
  if (s.answering) return;
  const id = s.id ?? randomId();
  const ns = s.namespace ?? namespace;
  const mine = new AbortController();
  controller = mine;
  // Answering from here on, so a second Ask waits; the turn shows once the lock is held.
  useChat.setState({ answering: true, notice: null });
  useDock.setState({ working: true });
  const run = (held: boolean) => answer(id, ns, question, asked, mine, held, withCard, opts);
  const locks = webLocks();
  if (!locks?.request) {
    void run(false);
    return;
  }
  let granted = false;
  locks
    .request(lockName(id), { ifAvailable: true }, (lock) => {
      if (!lock) {
        // A starter's card still reads: it is the person's own read, not the answer.
        if (otherTab(mine)) withCard?.alone("another tab is answering in this conversation");
        return;
      }
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
  keptBack = null;
  useChat.setState({ answering: false, checkingSources: null });
  useDock.setState({ working: false });
  return true;
}

/** Another tab holds the conversation's lock: nothing is asked. Answers whether this was
 *  still this tab's ask (a reset since, a sign-out say, leaves nothing to do). */
function otherTab(mine: AbortController): boolean {
  if (!settle(mine)) return false;
  useChat.setState({ notice: OTHER_TAB });
  announceAssistant(OTHER_TAB);
  // That tab's final save may have landed while the lock was asked for, its storage event
  // heard while this tab was answering: merge it now. The note stays until the next Ask,
  // an edit of the question or a storage event, so the refusal is said on screen.
  mergeStored();
  return true;
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
  asked: Asked,
  mine: AbortController,
  held: boolean,
  withCard: WithCard | null,
  opts: AskOptions,
): Promise<void> {
  const context = asked.context;
  // Stopped, or the chat reset, before the lock came: nothing is asked.
  if (mine.signal.aborted || controller !== mine) {
    settle(mine);
    return;
  }
  // What another tab finished in this conversation goes with the question.
  useChat.setState({ id, namespace, refused: null });
  mergeStored(held ? "stopped" : "omit");
  const before = useChat.getState().turns;
  // The turn answered again, when it is still the last one; otherwise the question is a
  // new turn and nothing is lost.
  const last = before[before.length - 1];
  const previous = opts.replace && last?.id === opts.replace ? last : null;
  const turnId = previous?.id ?? randomId();
  const earlier = previous ? before.slice(0, -1) : before;
  const messages = conversation(earlier, sentQuestion(question, opts.withSources), context);
  // A starter's turn answered again keeps its card, marked to be read again once it shows.
  const card = withCard?.card ?? (previous?.card ? { ...previous.card, again: true } : undefined);
  const fromField = opts.fromField ?? !withCard;
  useChat.setState({
    turns: [
      ...earlier,
      {
        id: turnId,
        question,
        ...(context ? { context } : {}),
        ...(opts.withSources ? { withSources: true } : {}),
        ...(card ? { card } : {}),
        answer: "",
        state: "answering",
        at: Date.now(),
      },
    ],
    editing: null,
  });
  withCard?.shown(turnId);
  // The next question starts again from the page alone, its chip on. A starter's question,
  // or one asked again from the transcript, never came from the field: what is typed there,
  // and its chip, stay.
  if (fromField) useDock.setState({ draft: "", item: null, pageOff: false });
  announceAssistant("The assistant is answering.");
  // Saved at its start: a reload mid-answer shows it as stopped. Other tabs leave a turn
  // still answering out until it ends. A turn asked again is stored as it was until its new
  // text begins.
  keptBack = previous;
  save({ held });
  const update = (change: (t: Turn) => Partial<Turn>) => {
    if (controller !== mine) return;
    useChat.setState((st) => ({
      turns: st.turns.map((t) => (t.id === turnId ? { ...t, ...change(t), at: Date.now() } : t)),
    }));
  };
  let streamed = false;
  // The label, said with the end of the answer, and the support it is for.
  let labelled = "";
  let support: Support | null = null;
  // Every source the answer cites was found: its label needs no saying once the end was.
  let allFound = false;
  // `loom.text_done` came: the text is complete and its sources are being checked.
  let textDone = false;
  // The end was said at text_done (the panel was open then): it is not said again, nor
  // marked unread, when the check lands.
  let saidDone = false;
  const endSaid = (unchecked = "") =>
    endAnnouncement({ label: labelled || (support === null ? unchecked : ""), allFound, saidDone });
  const end = (change: Partial<Turn>, said: string) => {
    update(() => change);
    if (!settle(mine)) return;
    save({ held });
    const unread = !useDock.getState().open && change.state === "done" && !saidDone;
    if (unread) {
      useDock.setState({ unread: true });
      announceAssistant("Assistant answer ready");
    } else if (said) announceAssistant(said);
  };
  // What ended the answer: null when its stream ended.
  let failure: unknown = null;
  try {
    await streamAssistantChat(
      namespace,
      messages,
      {
        onText: (text) => {
          streamed = true;
          if (controller === mine) keptBack = null;
          update((t) => ({ answer: t.answer + text }));
        },
        onCitations: (citations, landed, unknownNames, read) => {
          support = landed;
          allFound = landed === "supported" && citations.every((c) => c.verified);
          labelled = supportLabel(landed, citations, unknownNames)?.text ?? "";
          // The server's one read of the run replaces the live list (an empty one when the
          // run made no call); when that read failed or ran past its bound the event lists
          // none, and the steps already shown stay.
          update(() => ({
            citations,
            support: landed,
            unknownNames,
            ...(read.listed ? { steps: read.steps, stepsCut: read.cut } : {}),
          }));
        },
        // Display only: the steps are not said (the live region keeps card lines and the end).
        onSteps: (read) => update(() => ({ steps: read.steps, stepsCut: read.cut })),
        // The turn is done and Stop goes; Ask still waits for the checks, which hold the
        // namespace's answer slot.
        onTextDone: () => {
          // A turn asked again with no new text keeps answering until it goes back as it was.
          if (controller !== mine || (previous && !streamed)) return;
          textDone = true;
          update(() => ({ state: "done" }));
          useChat.setState({ checkingSources: turnId });
          saidDone = useDock.getState().open;
          if (saidDone) announceAssistant("The answer is complete.");
        },
      },
      mine.signal,
    );
  } catch (e: unknown) {
    failure = e;
  }
  // A turn asked again replaces the answer before it only once new text came: however it
  // ended (the stream's end, a refusal, an error, Stop), with none it goes back as it was.
  if (previous && !streamed) return putBack(previous, question, asked, opts, failure, mine, held);
  if (failure === null) {
    // An answer came: whatever the route refused before no longer holds.
    if (controller === mine) useChat.setState({ hint: null });
    end({ state: "done" }, endSaid());
  } else {
    const e = failure;
    // The text was complete: whatever cut the checks short, the answer stands; with no
    // label yet, its check did not finish.
    if (textDone)
      return end({ state: "done", ...(support === null ? { checkLost: true } : {}) }, endSaid(NOT_CHECKED.text));
    if (isAbort(e)) return end({ state: "stopped" }, "The answer was stopped.");
    // A refusal before the stream started is an HTTP error (status set); one inside the
    // stream is an error event (status 0) after part of the answer.
    const refusedBefore = e instanceof ApiError && e.status !== 0 && !streamed;
    const reason = refusedBefore ? refusalReason(e) : null;
    // A starter's card works while the assistant cannot answer: it stays, as the card alone.
    if (refusedBefore && withCard) return cardAlone(turnId, reason, e, mine, held);
    if (reason) return takeBack(id, turnId, question, asked, reason, mine, held);
    // A refused question was never answered: it waits in the field to be asked again, with
    // the chip it was asked with.
    if (refusedBefore && controller === mine) giveBack(question, asked);
    end(
      {
        state: refusedBefore ? "refused" : "failed",
        error: humanizeError(e, { read: true }),
        ...(retryableError(e) ? { retryable: true } : {}),
      },
      "",
    );
  }
}

/** What is said when an answer ends. `label`: its label ("" for none); `allFound`: every
 *  source it cites was found; `saidDone`: "The answer is complete." was said when its text
 *  ended, so only a label not fully confirmed is said now. */
export function endAnnouncement(end: { label: string; allFound: boolean; saidDone: boolean }): string {
  if (!end.saidDone) return end.label ? `The answer is complete. ${end.label}.` : "The answer is complete.";
  return end.label && !end.allFound ? `${end.label}.` : "";
}

/** A turn asked again ended with no new text (`e` null: its stream ended empty): it goes
 *  back as it was, and Try again or an Ask from the field answers it again in place. */
function putBack(
  previous: Turn,
  question: string,
  asked: Asked,
  opts: AskOptions,
  e: unknown,
  mine: AbortController,
  held: boolean,
): void {
  if (controller !== mine) return;
  // A card read the attempt started would land on the turn put back.
  cardReads.get(previous.id)?.abort();
  cardReads.delete(previous.id);
  const reason = e instanceof ApiError && e.status !== 0 ? refusalReason(e) : null;
  const said = isAbort(e)
    ? "Stopped before the answer began: the answer before it stays."
    : e === null
      ? "No new answer came: the answer before it stays."
      : reason
        ? `The assistant can't answer: ${SHORT_REASON[reason.kind]}. The answer before it stays.`
        : `Not asked again: ${humanizeError(e, { read: true, bare: true })}`;
  // Try again asks it again: a refusal the panel shows, and an edit, whose words are
  // nowhere else once the field holds other text.
  const again = reason || question !== previous.question;
  useChat.setState((st) => ({
    turns: st.turns.map((t) =>
      t.id === previous.id ? { ...previous, card: keptCard(previous, t), at: Date.now() } : t,
    ),
    // Said in the notice too: it stays once readiness clears a reason of the setup.
    notice: said,
    ...(reason ? { hint: reason } : {}),
    ...(again
      ? { refused: { question, asked, replace: previous.id, ...(opts.withSources ? { withSources: true } : {}) } }
      : {}),
  }));
  // Stopped, a Retry needs nothing in the field; an edit's words go back to it.
  if ((!isAbort(e) || question !== previous.question) && !useDock.getState().draft) {
    giveBack(question, asked);
    useChat.setState({
      editing: { id: previous.id, ...(opts.withSources ? { withSources: true } : {}) },
    });
  }
  settle(mine);
  save({ held });
  announceAssistant(said);
}

/** The card of a turn put back: the one its attempt's read landed in, or the turn's own
 *  when no read landed (none started, or one stopped busy); never marked again or busy, as
 *  its reads were stopped. */
function keptCard(previous: Turn, shown: Turn): Turn["card"] {
  const card = !shown.card || shown.card.busy ? previous.card : shown.card;
  if (!card) return card;
  const { again: _again, busy: _busy, ...kept } = card;
  return kept;
}

/** The last turn, when it can be asked again or edited: nothing answering, and not a card
 *  alone. A selector, so a component re-renders when it changes. */
export function lastAskedOf(s: Pick<ChatState, "turns" | "answering">): Turn | null {
  const t = s.turns[s.turns.length - 1];
  return !s.answering && t && t.state !== "card" && t.state !== "answering" ? t : null;
}

const lastAsked = (): Turn | null => lastAskedOf(useChat.getState());

/** Ask the last turn's question again in its place, with the context line it was asked
 *  with (Retry), or with CITE_SOURCES (`withSources`, "Ask again with sources"). A
 *  starter's turn reads its card again. */
export function retry(turnId: string, namespace: string, { withSources }: { withSources?: boolean } = {}): void {
  const t = lastAsked();
  if (!t || t.id !== turnId) return;
  // A refused question was given back to the field: asked again, it leaves it.
  const fromField = useDock.getState().draft === t.question;
  ask(t.question, namespace, { context: t.context ?? null, item: null }, null, {
    replace: t.id,
    withSources: withSources ?? t.withSources === true,
    fromField,
  });
}

/** Put the last question in an empty field to be edited; Ask then answers that turn again,
 *  with sources when it was asked with them. */
export function editLast(): boolean {
  const t = lastAsked();
  if (!t || useDock.getState().draft) return false;
  useChat.setState({ editing: { id: t.id, ...(t.withSources ? { withSources: true } : {}) }, notice: null });
  useDock.setState({ draft: t.question });
  return true;
}

/** Leave the edit: the field empties, and an edit put back is not asked again either. */
export function cancelEdit(): void {
  const { editing, refused } = useChat.getState();
  if (!editing) return;
  useChat.setState({ editing: null, ...(refused?.replace === editing.id ? { refused: null } : {}) });
  useDock.setState({ draft: "" });
}

/** The route refused before the answer for a reason the panel shows: the turn is taken
 *  back (never saved as finished, so no tab keeps it), the question waits in the field, and
 *  Try again asks it. A conversation whose only question this was is deleted, and the next
 *  question starts a new one in the header's namespace. */
function takeBack(
  id: string,
  turnId: string,
  question: string,
  asked: Asked,
  reason: AssistantReason,
  mine: AbortController,
  held: boolean,
): void {
  if (controller !== mine) return;
  const turns = useChat.getState().turns.filter((t) => t.id !== turnId);
  useChat.setState({ turns, hint: reason, refused: { question } });
  giveBack(question, asked);
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

/** The route refused a starter's question before the answer: the turn becomes the card
 *  alone, in place (its read, already started, lands in it), saying why; the question is not
 *  put in the field (the starter asks it again). A reason the panel shows is shown there too. */
function cardAlone(
  turnId: string,
  reason: AssistantReason | null,
  e: unknown,
  mine: AbortController,
  held: boolean,
): void {
  if (controller !== mine) return;
  // A reason the panel shows in its short words; anything else (busy, over the cost cap, a
  // failure) in the route's own words, so a wait and try again is said.
  const why = reason ? SHORT_REASON[reason.kind] : humanizeError(e, { read: true, bare: true }).replace(/\.$/, "");
  useChat.setState((st) => ({
    turns: st.turns.map((t) =>
      t.id === turnId && t.card
        ? { ...t, state: "card" as const, answer: "", card: { ...t.card, unanswered: why }, at: Date.now() }
        : t,
    ),
    ...(reason ? { hint: reason } : {}),
  }));
  settle(mine);
  save({ held });
  announceAssistant(`The assistant can't answer: ${why}.`);
}

/** A question that was never answered goes back to the field with the chip it was asked
 *  with (the item, or the chip removed), unless something else is typed there now. */
function giveBack(question: string, asked: Asked): void {
  if (useDock.getState().draft) return;
  useDock.setState({ draft: question, item: asked.item, pageOff: asked.context === null });
}

export function stop(): void {
  controller?.abort();
}

/** The page hides (a reload, a closed tab, a switch away on a phone): what is on screen is
 *  saved, an answer mid-stream as it stands. */
export function saveOnHide(): void {
  if (useChat.getState().turns.length > 0) save();
}
