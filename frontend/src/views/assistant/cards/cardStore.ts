// What a starter does to the conversation: it adds a turn holding its card (asking the
// assistant too, or not), and reads the card with the person's own token. A card's read
// belongs to its turn: a read that lands after the conversation was left, reset or deleted
// changes nothing. Nothing here writes to Loom.
import {
  auditNamespace,
  inspectKnowledge,
  inspectMemories,
  mentorBrief,
  namespaceInventory,
  storedToken,
  tuneNamespace,
  type MentorBriefResult,
  type NamespaceAuditResult,
  type NamespaceInventory,
  type NamespaceTune,
} from "../../../api";
import { announceAssistant } from "../../../lib/assistantDockRuntime";
import { useDock } from "../../../lib/assistantDock";
import { canCall } from "../../../lib/canCall";
import { humanizeError } from "../../../lib/errorWords";
import { needsRole, useNamespaceRoleRead } from "../../../lib/namespaceRole";
import { useStore } from "../../../lib/state";
import { ask, cardReads, save, useChat } from "../chatStore";
import { randomId } from "../conversations";
import { tokenReadScope } from "../tokenScope";
import { starterOf, starterQuestion } from "./starters";
import type { CardArgs, CardOp, TurnCard } from "./types";

/** The audit's samples per finding. */
const SAMPLE_LIMIT = 5;

/** The cards read in this tab, by turn: a card restored from a stored conversation was not,
 *  so what it shows is from its own time, and Apply waits for a read again. */
const fresh = new Set<string>();

export const isFresh = (turnId: string) => fresh.has(turnId);

/** The newest inventory card's data in the conversation shown, for the subject starter's
 *  suggestions and the scope it says the token reads. */
export function lastInventory(turns = useChat.getState().turns): NamespaceInventory | null {
  for (let i = turns.length - 1; i >= 0; i--) {
    const c = turns[i].card;
    if (c?.op === "namespace.inventory" && c.data) return c.data as NamespaceInventory;
  }
  return null;
}

export const ROLE_LOADING = "Your role on this namespace is still loading.";

/** Why a card's read cannot run for this person, or null. Each read is gated on its own op:
 *  all are viewer reads, so a refusal says why the role is missing (still loading, a request
 *  waiting, list_mine unread, or no access); the server
 *  refuses memory.inspect and namespace.tune to a token pinned to one user or session, and a
 *  scoped read that needs its user or session in the request, which a card does not send. */
export function useCardGate(namespace: string): (op: CardOp) => string | null {
  const whoami = useStore((s) => s.whoami);
  const { role, loading, rows } = useNamespaceRoleRead(namespace);
  const turns = useChat((s) => s.turns);
  const tokenScope = tokenReadScope(storedToken());
  const inventory = lastInventory(turns);
  const narrowed = tokenScope?.readScope != null || (inventory !== null && inventory.scope.read_scope !== "namespace");
  const incomplete =
    tokenScope?.readScope === "own_session" || (tokenScope?.readScope === "own_user" && !tokenScope.readUserId);
  return (op) => {
    if (op === "graph") return null;
    const scoped = op === "namespace.inventory" || op === "knowledge.inspect";
    if (loading) return ROLE_LOADING;
    if (!canCall(op, role, whoami)) {
      if (rows?.some((m) => m.namespace === namespace && m.status === "pending"))
        return `Your request to join ${namespace} is waiting for an admin.`;
      // Signed in and not loading, with no rows: list_mine could not be read.
      if (whoami && rows === null) return `Couldn't check your access to ${namespace}. Try again.`;
      return needsRole("viewer", namespace);
    }
    if (incomplete && scoped)
      return "Your token needs each request to name its user or session, which a card does not send.";
    if (narrowed && (op === "memory.inspect" || op === "namespace.tune"))
      return "Your token is limited to one user or session; inspecting memories and drafting need a token for the whole namespace.";
    return null;
  };
}

function read(card: TurnCard, scope: { org: string; namespace: string }, signal: AbortSignal): Promise<unknown> {
  const a = card.args;
  switch (card.op) {
    case "namespace.inventory":
      return namespaceInventory(scope, { signal });
    case "namespace.audit":
      return auditNamespace(scope, { windowSeconds: a.windowSeconds, sampleLimit: SAMPLE_LIMIT, signal });
    case "mentor.brief":
      return mentorBrief(scope, { agent: a.agent || undefined, windowSeconds: a.windowSeconds, signal });
    case "memory.inspect":
      return inspectMemories(scope, a.subject ?? "user", a.id ?? "", { signal });
    case "knowledge.inspect":
      return inspectKnowledge(scope, { signal });
    case "namespace.tune":
      return tuneNamespace(scope, { signal });
    case "graph":
      return Promise.resolve(null);
  }
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The one line the assistant's live region says when a card lands. */
export function cardLine(op: CardOp, data: unknown): string {
  try {
    return lineOf(op, data);
  } catch {
    return `${starterOf(op).title}: read.`;
  }
}

function lineOf(op: CardOp, data: unknown): string {
  const title = starterOf(op).title;
  switch (op) {
    case "namespace.audit": {
      const r = data as NamespaceAuditResult;
      return r.findings.length === 0
        ? "Audit: nothing to flag."
        : `Audit: ${r.criticals} critical, ${plural(r.warnings, "warning")}, ${r.infos} info.`;
    }
    case "mentor.brief":
      return `Mentor: ${plural((data as MentorBriefResult).hints.length, "hint")}.`;
    case "namespace.tune":
      return `Improvements: ${plural((data as NamespaceTune).proposals.length, "draft")}.`;
    case "graph":
      return "Relationships: open the graph from the card.";
    default:
      return `${title}: read.`;
  }
}

function patchCard(turnId: string, change: (c: TurnCard) => TurnCard): boolean {
  let found = false;
  useChat.setState((st) => ({
    turns: st.turns.map((t) => {
      if (t.id !== turnId || !t.card) return t;
      found = true;
      return { ...t, card: change(t.card), at: Date.now() };
    }),
  }));
  return found;
}

/** Read the card of turn `turnId` (again), with `args` changed when given; `gate` says why
 *  it cannot run, which the card shows in its place. */
export function readCard(turnId: string, gate: string | null, args?: CardArgs): void {
  const st = useChat.getState();
  const turn = st.turns.find((t) => t.id === turnId);
  const org = useStore.getState().identity?.org ?? "";
  const namespace = st.namespace ?? "";
  if (!turn?.card || !org || !namespace) return;
  cardReads.get(turnId)?.abort();
  const { again: _again, ...was } = turn.card;
  const card = { ...was, ...(args ? { args: { ...was.args, ...args } } : {}) };
  if (gate) {
    patchCard(turnId, () => ({ ...card, error: gate, failed: false, busy: false }));
    // Kept with the turn: an answer still streaming saves it when it ends.
    if (!useChat.getState().answering) save();
    return;
  }
  const mine = new AbortController();
  cardReads.set(turnId, mine);
  patchCard(turnId, () => ({ ...card, busy: true, error: undefined, failed: false }));
  const conversation = st.id;
  // `landed`: the read's data is the card's now (a read of this visit, which may apply).
  const settle = (change: (c: TurnCard) => TurnCard, said: string, landed = false) => {
    if (cardReads.get(turnId) !== mine) return;
    cardReads.delete(turnId);
    // The conversation was left or reset: the read is not its own any more.
    if (useChat.getState().id !== conversation) return;
    if (landed) fresh.add(turnId);
    if (!patchCard(turnId, change)) return;
    announceAssistant(said);
    // An answer still streaming saves its turn when it ends; otherwise the card is saved now.
    if (!useChat.getState().answering) save();
  };
  read(card, { org, namespace }, mine.signal).then(
    (data) =>
      settle((c) => ({ ...c, data, readAt: Date.now(), busy: false, error: undefined }), cardLine(card.op, data), true),
    (e: unknown) => {
      if (mine.signal.aborted) return;
      const text = `Couldn't read the ${starterOf(card.op).title.toLowerCase()}: ${humanizeError(e, { bare: true, read: true })}`;
      settle(
        (c) => ({ ...c, busy: false, error: text, failed: true }),
        `${starterOf(card.op).title}: couldn't read it.`,
      );
    },
  );
}

/** A starter was chosen: its turn joins the conversation (a new one in `namespace` when none
 *  is open) and its card is read. With `alsoAsk` the assistant is asked its question as the
 *  person's turn, else the turn is the card alone and no model is called. */
export function startStarter(
  op: CardOp,
  args: CardArgs,
  {
    alsoAsk,
    gate,
    namespace,
    unanswered,
  }: { alsoAsk: boolean; gate: string | null; namespace: string; unanswered?: string | null },
): void {
  const st = useChat.getState();
  if (st.answering) return;
  const card: TurnCard = { op, args, ...(unanswered ? { unanswered } : {}) };
  const question = starterQuestion(op, args);
  // The turn as the card alone: no model is called. `why`: the assistant was asked and did
  // not answer (another tab answers in the conversation, or the route refused), said on the
  // card.
  const alone = (why: string | null) => {
    const now = useChat.getState();
    const turnId = randomId();
    const own = why ? { ...card, unanswered: why } : card;
    useChat.setState({
      id: now.id ?? randomId(),
      namespace: now.namespace ?? namespace,
      turns: [...now.turns, { id: turnId, question, answer: "", state: "card", card: own, at: Date.now() }],
    });
    save();
    readCard(turnId, gate);
  };
  if (alsoAsk) {
    ask(question, namespace, { context: null, item: null }, { card, shown: (id) => readCard(id, gate), alone });
    return;
  }
  useChat.setState({ notice: null });
  alone(null);
  useDock.setState({ focusField: false });
}

/** Forget the reads made in this tab, for a test to start clean (a read that lands after
 *  its conversation was left or reset is dropped by readCard in any case). */
export function forgetCards(): void {
  for (const c of cardReads.values()) c.abort();
  cardReads.clear();
  fresh.clear();
}
