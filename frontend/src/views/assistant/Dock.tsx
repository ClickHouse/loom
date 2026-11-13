import { Suspense, lazy, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { Title } from "@clickhouse/click-ui/Title";
import { isAuthDisabled, isProxyMode } from "../../api";
import { getOrgModelCalls } from "../../api/guardrails";
import { toast } from "../../components/Toast";
import { ListTime } from "../../components/ui/ListTime";
import { ListRow } from "../../components/ui/ListRow";
import { RowMenu } from "../../components/ui/RowMenu";
import { useStarterPrefs } from "./cards/prefs";
import { chatOff, useDock } from "../../lib/assistantDock";
import { STARTERS_WAIT } from "./startersWait";
import { claimReason } from "../../lib/assistantClaims";
import { SHORT_REASON, setDockReason, useDockMode, type AssistantReason } from "../../lib/assistantLayout";
import { announceAssistant, closeAssistant } from "../../lib/assistantDockRuntime";
import { CONVERSATION_PREFIX, EPOCH_KEY, INDEX_KEY, type Owner } from "../../lib/assistantStorage";
import { canReadNamespace, isCollectionName, isMemoryNamespace } from "../../lib/namespace";
import { hasOrgAdminClaim, hasPlatformAdminClaim, isNamespaceAdmin } from "../../lib/role";
import { useStore } from "../../lib/state";
import { AssistantChat, KEPT_HERE } from "./Chat";
import {
  CITE_SOURCES,
  useChat,
  newConversation,
  openConversation,
  resetChat,
  saveOnHide,
  ask,
  adoptOwner,
  newConversationLine,
  startIn,
  ownerOf,
  sentQuestion,
  storedChanged,
} from "./chatStore";
import { adoptEpoch, deleteAllConversations, deleteConversation, listConversations } from "./conversations";
import { MAX_MESSAGE_CHARS, contextLength, currentAsk, withContext } from "./pageContext";
import { readReadiness } from "./readiness";

// The starters and their cards: a chunk of their own, loaded when the panel shows them.
const Starters = lazy(() => import("./cards/Starters").then((m) => ({ default: m.Starters })));

/** The reason while this deployment's chat is off (lib/assistantDock.ts chatOff). */
const NOT_CONFIGURED: AssistantReason = { kind: "not_configured" };

/** How often the panel reads readiness again while the assistant is setting up or failing. */
export const READINESS_POLL_MS = 10_000;

type ModelCallsState = "loading" | "admin" | "locked" | "just_on" | "unknown";

/** Why nothing can be asked now, first match; null when a question can be asked. Ask and
 *  Try again both go by it. */
function blockedBy(c: {
  reason: AssistantReason | null;
  checking: boolean;
  memory: boolean;
  mismatch: boolean;
}): string | null {
  if (c.reason) return "Ask AI can't answer now: the panel says why.";
  if (c.checking) return "Getting Ask AI ready.";
  if (!c.memory) return "Ask AI answers about a memory namespace.";
  if (c.mismatch) return "Pick how to go on with this conversation first.";
  return null;
}

/** The body of the assistant panel: the chat, History, and why the assistant cannot answer
 *  when it cannot. Loaded the first time the panel opens. */
export function DockBody() {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const mine = useStore((s) => s.myNamespaces);
  const expired = useStore((s) => s.sessionExpired);
  const open = useDock((s) => s.open);
  const mode = useDockMode();
  const namespace = identity?.namespace ?? "";
  const owner = useMemo(() => ownerOf(whoami), [whoami]);
  const ownerKey = owner ? JSON.stringify(owner) : "";
  const convNamespace = useChat((s) => s.namespace);
  const turns = useChat((s) => s.turns);
  const answering = useChat((s) => s.answering);
  const storageOff = useChat((s) => s.storageOff);
  const notice = useChat((s) => s.notice);
  const refused = useChat((s) => s.refused);
  const hint = useChat((s) => s.hint);
  const readiness = useChat((s) => s.readiness);
  const chatId = useChat((s) => s.id);
  const off = chatOff();
  const focusField = useDock((s) => s.focusField);
  // History is the panel's own view, which its header's History button toggles.
  const showHistory = useDock((s) => !!s.history);
  const setShowHistory = (history: boolean) => useDock.setState({ history });
  // An open the person starts asks for the question field (openAssistant's focusField), on
  // an open panel too (the search's Continue): History gives way to the chat, which shows it.
  useEffect(() => {
    if (focusField) setShowHistory(false);
  }, [focusField]);
  // A closed panel reopens on the chat, whose field takes the focus.
  useEffect(() => {
    if (!open) setShowHistory(false);
  }, [open]);
  // The header's New chat: a new conversation, on the chat view, its field focused. While an
  // answer streams the button drops the click, and newConversation keeps the conversation.
  const newChatSeq = useDock((s) => s.newChatSeq ?? 0);
  const handledNewChat = useRef(newChatSeq);
  useEffect(() => {
    if (newChatSeq === handledNewChat.current) return;
    handledNewChat.current = newChatSeq;
    newConversation();
    useDock.setState({ history: false, focusField: true });
  }, [newChatSeq]);
  // "Ask about this" (lib/assistantDockRuntime.ts askAbout): the chat shows, and a
  // conversation about another namespace gives way to a new one in the header's, which the
  // question being typed and the chip move to (they are the panel's); the old one stays in
  // History. While an answer still streams there, none starts: the mismatch prompt shows and
  // the chip keeps the item.
  const askSeq = useDock((s) => s.askSeq);
  const handledAsk = useRef(0);
  useEffect(() => {
    if (askSeq === handledAsk.current) return;
    handledAsk.current = askSeq;
    setShowHistory(false);
    const line = startIn(useStore.getState().identity?.namespace ?? "");
    if (!line) return;
    useChat.setState({ notice: line });
    announceAssistant(line);
  }, [askSeq]);
  // The Memories page's Insights button (lib/assistantDockRuntime.ts openStarters): the chat shows,
  // and a finished conversation gives way to a new one, whose empty state lists the starters.
  // While an answer streams, its conversation stays (the button says so).
  const startersSeq = useDock((s) => s.startersSeq);
  const handledStarters = useRef(startersSeq);
  useEffect(() => {
    if (startersSeq === handledStarters.current) return;
    handledStarters.current = startersSeq;
    setShowHistory(false);
    const c = useChat.getState();
    if (c.answering) {
      // Said on screen and to a screen reader: the starters wait for the answer.
      useDock.setState({ startersAudit: false });
      useChat.setState({ notice: STARTERS_WAIT });
      announceAssistant(STARTERS_WAIT);
    } else if (c.turns.length > 0) newConversation();
  }, [startersSeq]);
  const [checking, setChecking] = useState(true);
  // Bumped to draw History again: a write to the index here or in another tab.
  const [, setIndexTick] = useState(0);

  // The person (or their token's scope) changed: what was shown is not theirs. The starters
  // show once this has run, so nothing a starter adds is reset by it.
  const [ownerSettled, setOwnerSettled] = useState(false);
  useEffect(() => {
    setOwnerSettled(true);
    adoptOwner(owner);
  }, [ownerKey, owner]);

  // Another tab: a bumped epoch (a sign-out, Delete all) clears this view; a write to the
  // conversation shown brings in the turns it finished.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      const s = useChat.getState();
      if (e.key === EPOCH_KEY || e.key === null) {
        resetChat(s.owner);
        adoptEpoch(isProxyMode() ? (s.owner?.subject ?? null) : null);
        return;
      }
      // Deleted there: it stays on screen here, unsaved, until this tab starts another.
      if (s.id && e.key === CONVERSATION_PREFIX + s.id) storedChanged();
      if (e.key === INDEX_KEY) setIndexTick((n) => n + 1);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Saved when the page hides: a reload or a closed tab keeps what is on screen.
  useEffect(() => {
    const onHide = () => saveOnHide();
    const onVisibility = () => {
      if (document.visibilityState === "hidden") saveOnHide();
    };
    window.addEventListener("pagehide", onHide);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", onHide);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  // Readiness: read when the panel opens, and every READINESS_POLL_MS while it reports the
  // assistant setting up or failing and the panel is open.
  const [readTick, setReadTick] = useState(0);
  useEffect(() => {
    // With the chat off there is nothing to get ready.
    if (!open || isAuthDisabled() || off) {
      setChecking(false);
      return;
    }
    const ctl = new AbortController();
    void readReadiness(ctl.signal).then((r) => {
      if (ctl.signal.aborted) return;
      // A route's refusal that said setting up or failing is what readiness now says (as
      // Try again does): readiness reports the setup from here on.
      const h = useChat.getState().hint;
      const stale = h?.kind === "starting" || h?.kind === "failing";
      useChat.setState({ readiness: r, ...(stale ? { hint: null } : {}) });
      setChecking(false);
    });
    return () => ctl.abort();
  }, [open, readTick, off]);
  // The setup is in progress or failing by readiness or by the route's refusal: read again.
  const settingUp = (k?: string) => k === "starting" || k === "failing";
  const polling = open && (settingUp(readiness?.kind) || settingUp(hint?.kind));
  useEffect(() => {
    if (!polling) return;
    const t = setInterval(() => setReadTick((n) => n + 1), READINESS_POLL_MS);
    return () => clearInterval(t);
  }, [polling]);

  // The reason shown, first match: the chat off, the token's claims, the route's refusal,
  // readiness.
  const reason = off ? NOT_CONFIGURED : (claimReason() ?? hint ?? readiness);
  useEffect(() => {
    setDockReason(reason);
  }, [reason]);

  const tryAgain = async () => {
    setChecking(true);
    const r = await readReadiness();
    useChat.setState({ readiness: r, hint: null });
    setChecking(false);
    const reasonNow = off ? NOT_CONFIGURED : (claimReason() ?? r);
    // Try again goes with the reason: the focus goes to the question field.
    if (!reasonNow) useDock.setState({ focusField: true });
    // A question the route refused is asked again, in the conversation's namespace, unless
    // what blocks Ask still applies; with nothing refused, nothing is sent.
    const pending = useChat.getState().refused;
    const c = useChat.getState();
    const st = useStore.getState();
    const header = st.identity?.namespace ?? "";
    const stillBlocked = blockedBy({
      reason: reasonNow,
      checking: false,
      memory: isMemoryNamespace(header, st.myNamespaces),
      mismatch: c.turns.length > 0 && c.namespace !== null && c.namespace !== header,
    });
    // A new question goes with what the chip shows now (a chip removed since sends nothing
    // about the page); a turn asked again with the page line it was asked with.
    // A line longer now than when it was asked (an item added since) can push the message
    // past the route's length: nothing is sent. The question waits in the field, which says
    // why; a field holding other text keeps it, and a line in the panel says why.
    if (!pending || stillBlocked) return;
    const asked = pending.asked ?? currentAsk();
    const draft = useDock.getState().draft;
    if (withContext(sentQuestion(pending.question, pending.withSources), asked.context).length > MAX_MESSAGE_CHARS) {
      if (!draft || draft === pending.question) useDock.setState({ draft: pending.question });
      else {
        const limit =
          MAX_MESSAGE_CHARS - contextLength(asked.context) - (pending.withSources ? CITE_SOURCES.length : 0);
        const line = `Not sent: with this page's line the question is longer than ${limit.toLocaleString("en-US")} characters.`;
        useChat.setState({ notice: line });
        announceAssistant(line);
      }
      return;
    }
    // A turn asked again and refused is answered again in its place, as it was asked again;
    // the field empties only when it holds the question.
    const { question, asked: _asked, ...again } = pending;
    ask(question, c.namespace ?? header, asked, null, { ...again, fromField: draft === question });
  };

  // Chat not set up or its setup failing: the panel is its empty state and the starters,
  // with no question field (ReasonBody's SetupState).
  const setupDown = reason?.kind === "not_configured" || reason?.kind === "failing";
  const memory = isMemoryNamespace(namespace, mine);
  const mismatch = turns.length > 0 && convNamespace !== null && convNamespace !== namespace;
  const blocked = blockedBy({ reason, checking, memory, mismatch });
  // A conversation kept in a collection is not reopened: the header never holds one.
  const readable = (ns: string) =>
    !isCollectionName(ns) && (ns === namespace || canReadNamespace(ns, whoami?.namespaces ?? [], mine));
  // Why a starter cannot ask the assistant now: its card still reads.
  const cannotAnswer = reason
    ? SHORT_REASON[reason.kind]
    : checking
      ? "it is getting ready"
      : !memory
        ? "it answers about a memory namespace"
        : null;
  // What a card's pick goes by (an inventory row asks as a starter does): kept current on
  // every render, not only while the starters show, so a pick never asks while the field is
  // blocked, the conversation's namespace differing from the header's included.
  const pickCannotAnswer = cannotAnswer ?? (mismatch ? "pick how to go on with this conversation first" : null);
  useEffect(() => {
    useStarterPrefs.setState({ cannotAnswer: pickCannotAnswer });
  }, [pickCannotAnswer]);

  // With the chat off the cards are a list of reads: the starters stay under the last one,
  // except under a conversation reopened from History, which is read-only.
  const startersShown = ownerSettled && !expired && !showHistory && (turns.length === 0 || (off && chatId === null));
  // With no question field an open's focus goes to the first starter (Starters takes it), or
  // to the panel where none renders.
  useEffect(() => {
    if (!setupDown || !focusField || !ownerSettled || startersShown) return;
    document.getElementById("assistant-panel")?.focus();
    useDock.setState({ focusField: false });
  }, [setupDown, focusField, ownerSettled, startersShown]);
  if (expired)
    return (
      <p className="px-4 py-3 text-sm text-text-muted" role="status">
        Your session ended. Sign in again to see your conversations.
      </p>
    );

  const starters = startersShown ? (
    <Suspense fallback={null}>
      <Starters namespace={namespace} phone={mode === "sheet"} cannotAnswer={cannotAnswer} noField={setupDown} />
    </Suspense>
  ) : null;

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {showHistory && !owner ? (
        // No owner (auth off, or a session whose whoami names no subject): nothing is saved.
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3">
          <p className="assistant-column text-sm text-text-muted">{NOT_SAVED}</p>
        </div>
      ) : showHistory && owner ? (
        <History
          owner={owner}
          readable={readable}
          current={chatId}
          onDeleted={() => setIndexTick((n) => n + 1)}
          onOpen={async (entry) => {
            if (!(await openConversation(entry.id))) return;
            setShowHistory(false);
            if (identity && entry.namespace !== namespace) {
              useStore.getState().setIdentity({ ...identity, namespace: entry.namespace });
              toast(`Switched to ${entry.namespace} to continue this conversation`, "info");
            }
            useDock.setState({ focusField: true });
          }}
        />
      ) : (
        <>
          {/* It scrolls when the starters fill it; the padding keeps their focus rings whole. */}
          <div className="px-4 pt-3 pb-2 min-h-0 overflow-y-auto">
            <div className="assistant-column space-y-3">
              {reason ? (
                <ReasonBody reason={reason} starters={startersShown} onTryAgain={() => void tryAgain()} />
              ) : checking ? (
                <p className="text-sm text-text-muted" role="status">
                  Getting Ask AI ready…
                </p>
              ) : !memory ? (
                <p className="text-sm text-text-muted">
                  Ask AI answers about a memory namespace. Pick one in the namespace picker.
                </p>
              ) : mismatch && convNamespace ? (
                <Mismatch old={convNamespace} now={namespace} />
              ) : turns.length === 0 ? (
                <p className="text-sm text-text-muted">
                  Ask about what this namespace holds. Answers cite the memories and documents they use.
                </p>
              ) : null}
              {starters}
              {storageOff && (
                <p className="text-xs text-text-muted">
                  This browser refused to save conversations: this one lasts until the tab closes.
                </p>
              )}
              {notice && <p className="text-sm text-text-muted">{notice}</p>}
              {/* A turn asked again and put back, with nothing in the way: Try again asks it. */}
              {refused?.replace && !blocked && !answering && (
                <Button type="secondary" htmlType="button" onClick={() => void tryAgain()}>
                  Try again
                </Button>
              )}
            </div>
          </div>
          <AssistantChat namespace={convNamespace ?? namespace} blocked={blocked} composer={!setupDown} />
        </>
      )}
    </div>
  );
}

/** History with no owner to save chats for. */
export const NOT_SAVED = "Chats aren't saved in this session.";

/** Where the chats are kept and what deleting does, said at the end of History. */
function keptHere(): string {
  const signOut = isProxyMode() ? "" : " Signing out deletes them.";
  return `${KEPT_HERE}${signOut} Deleting a memory does not change answers already saved here.`;
}

function Mismatch({ old, now }: { old: string; now: string }) {
  const identity = useStore((s) => s.identity);
  // One kept in a collection: History does not list it, and there is no way back, since the
  // header never holds a collection.
  const collection = isCollectionName(old);
  return (
    <div className="space-y-2">
      <p className="text-sm text-text">
        {collection ? (
          <>
            This conversation is about a collection; collections are in the{" "}
            <Link
              component="button"
              type="button"
              size="sm"
              onClick={() => {
                closeAssistant("nav");
                useStore.getState().setView("knowledge");
              }}
            >
              Library
            </Link>
            .
          </>
        ) : (
          `This conversation is about ${old}.`
        )}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="secondary"
          htmlType="button"
          onClick={() => {
            newConversation();
            announceAssistant(newConversationLine(now, old));
            useDock.setState({ focusField: true });
          }}
        >
          {`Start a new one in ${now}`}
        </Button>
        {!collection && (
          <Button
            type="secondary"
            htmlType="button"
            onClick={() => {
              if (identity) useStore.getState().setIdentity({ ...identity, namespace: old });
              useDock.setState({ focusField: true });
            }}
          >
            {`Switch back to ${old}`}
          </Button>
        )}
      </div>
    </div>
  );
}

function History({
  owner,
  readable,
  current,
  onDeleted,
  onOpen,
}: {
  owner: Owner;
  readable: (ns: string) => boolean;
  /** The chat the panel shows, marked in the list. */
  current: string | null;
  /** A Delete or Delete all here: DockBody draws the list again, as for another tab's. */
  onDeleted: () => void;
  onOpen: (entry: { id: string; namespace: string }) => Promise<void>;
}) {
  const list = useRef<HTMLUListElement>(null);
  const [confirming, setConfirming] = useState(false);
  // Where the focus goes once a Delete, Delete all or the confirmation's buttons have drawn:
  // a row (by its chat's id), Delete all chats, the confirmation's Cancel, else the panel
  // header's History button, which stays.
  const [focusTo, setFocusTo] = useState<{ row: string } | "delete-all" | "cancel" | "history" | null>(null);
  const deleteAll = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!focusTo) return;
    const target =
      focusTo === "delete-all"
        ? deleteAll.current
        : focusTo === "cancel"
          ? cancel.current
          : typeof focusTo === "object"
            ? list.current?.querySelector<HTMLElement>(`[data-conversation="${focusTo.row}"] [data-row-open]`)
            : null;
    (target ?? historyButton())?.focus();
    setFocusTo(null);
  }, [focusTo]);
  // Read on each render (cheap: one small key): DockBody draws it again on a delete here or
  // a write to the index in another tab.
  const entries = listConversations(owner, readable);
  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3">
      <div className="assistant-column space-y-3">
        {entries.length === 0 ? (
          <p className="text-sm text-text-muted">No chats saved here yet.</p>
        ) : (
          <ul ref={list} className="space-y-1" aria-label="Saved chats">
            {entries.map((e, i) => {
              const title = e.title || "Untitled";
              const shown = e.id === current;
              return (
                <li key={e.id} data-conversation={e.id}>
                  <ListRow
                    active={shown}
                    trailing={
                      <RowMenu
                        name={title}
                        items={[
                          {
                            label: "Delete",
                            icon: "trash",
                            danger: true,
                            onSelect: () => {
                              // The chat shown goes first, an answer in it stopped: nothing
                              // writes it after the delete.
                              if (useChat.getState().id === e.id) resetChat(owner);
                              deleteConversation(e.id);
                              // The row goes: the focus goes to the next row, or History.
                              const next = (entries[i + 1] ?? entries[i - 1])?.id;
                              setFocusTo(next ? { row: next } : "history");
                              onDeleted();
                            },
                          },
                        ]}
                      />
                    }
                  >
                    <button
                      type="button"
                      data-row-open=""
                      className="list-row-open block w-full min-w-0 text-sm text-text truncate"
                      aria-current={shown ? "true" : undefined}
                      onClick={() => void onOpen(e)}
                    >
                      {title}
                    </button>
                    <span className="block text-xs text-text-muted truncate">
                      {e.namespace} · <ListTime time={new Date(e.updated).toISOString()} />
                      {shown && " · Open now"}
                    </span>
                  </ListRow>
                </li>
              );
            })}
          </ul>
        )}
        <div className="space-y-2 border-t border-border pt-3">
          <p className="text-xs text-text-muted">{keptHere()}</p>
          {entries.length > 0 &&
            (confirming ? (
              // Confirmed here, in the panel: a dialog over a dialog would stack two traps.
              // biome-ignore lint/a11y/useSemanticElements: a confirmation's two buttons, not a form's fields
              <div role="group" aria-label="Delete all chats" className="space-y-2">
                <p className="text-sm text-text">Delete every chat saved in this browser?</p>
                <div className="flex flex-wrap justify-end gap-2">
                  <Button
                    type="secondary"
                    htmlType="button"
                    ref={cancel}
                    onClick={() => {
                      setConfirming(false);
                      setFocusTo("delete-all");
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    type="danger"
                    htmlType="button"
                    onClick={() => {
                      deleteAllConversations(owner);
                      resetChat(owner);
                      setConfirming(false);
                      // The list goes: the focus goes to the History button.
                      setFocusTo("history");
                      onDeleted();
                    }}
                  >
                    Delete all
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                type="danger"
                htmlType="button"
                ref={deleteAll}
                onClick={() => {
                  setConfirming(true);
                  setFocusTo("cancel");
                }}
              >
                Delete all chats
              </Button>
            ))}
        </div>
      </div>
    </div>
  );
}

/** The panel header's History button (layout/AssistantPanel.tsx). */
const historyButton = () => document.querySelector<HTMLElement>(".assistant-head button[aria-pressed]");

/** Why the assistant cannot answer, in words that say who fixes it and where (the link
 *  goes only to someone who can open the page it names). */
function ReasonBody({
  reason,
  starters,
  onTryAgain,
}: {
  reason: AssistantReason;
  /** The starters show under it. */
  starters: boolean;
  onTryAgain: () => void;
}) {
  const whoami = useStore((s) => s.whoami);
  const identity = useStore((s) => s.identity);
  const platform = hasPlatformAdminClaim(whoami);
  const orgAdmin = hasOrgAdminClaim(whoami);
  const modelCalls = useModelCalls(reason.kind === "model_calls_off" && (platform || orgAdmin), identity?.org ?? "");
  const guardrails = (
    <Link component="button" type="button" size="sm" onClick={() => useStore.getState().navigate("admin:guardrails")}>
      Admin › Guardrails
    </Link>
  );
  // Chat not set up, or its setup failing: one empty state, Loom's own reads under it.
  if (reason.kind === "not_configured" || reason.kind === "failing")
    return <SetupState failing={reason.kind === "failing"} starters={starters} onTryAgain={onTryAgain} />;
  let text: ReactNode;
  switch (reason.kind) {
    case "auth_off":
      text =
        "Ask AI answers as the signed-in person, so it needs auth on. An operator turns auth on for this deployment.";
      break;
    case "org_wide":
      text = "This token works across every org; Ask AI answers in one. Sign in with a token for one org.";
      break;
    case "narrowed":
      text = `This token can't use Ask AI: it is narrowed to ${reason.scope}. Sign in with a token that reads the whole namespace.`;
      break;
    case "token_expires_soon":
      text = `This token expires ${reason.leftS === undefined ? "" : `in ${reason.leftS} s, `}sooner than an answer can take. Sign in again or use a longer-lived token.`;
      break;
    case "starting":
      text = "Setting up — usually under a minute. This updates on its own.";
      break;
    case "model_calls_off":
      text =
        modelCalls === "just_on" ? (
          "Model calls were just switched on; this takes a few seconds to reach every server."
        ) : modelCalls === "locked" ? (
          "A platform admin locked model calls off for this org; only they can switch them on."
        ) : modelCalls === "admin" ? (
          <>Switch on model calls in {guardrails}.</>
        ) : modelCalls === "loading" ? (
          "Model calls are off for this org."
        ) : (
          "Model calls are off for this org. An org admin can switch them on in Admin › Guardrails, unless a platform admin locked them."
        );
      break;
  }
  return (
    <div className="space-y-2" data-testid="assistant-unavailable" data-reason={reason.kind}>
      <p className="text-sm text-text">{text}</p>
      <Button type="secondary" htmlType="button" onClick={onTryAgain}>
        Try again
      </Button>
    </div>
  );
}

/** Where the docs say how an operator sets up the assistant service. */
export const SETUP_DOCS = { page: "features/integrations/librechat.md", anchor: "the-deployed-assistant" };

/** Chat not set up (`failing`: its setup failing): the icon, a title and a line, then the
 *  docs on setting up the assistant service, or Try again while it fails, with Admin › Health
 *  for someone who can open it (a platform or namespace admin) and who fixes it for anyone
 *  but a platform admin. The line about Loom's own reads shows only while the starters, which
 *  need no model, show under it. */
function SetupState({
  failing,
  starters,
  onTryAgain,
}: {
  failing: boolean;
  starters: boolean;
  onTryAgain: () => void;
}) {
  const openDocsPage = useStore((s) => s.openDocsPage);
  const whoami = useStore((s) => s.whoami);
  const mine = useStore((s) => s.myNamespaces);
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const platform = hasPlatformAdminClaim(whoami);
  const seesHealth = platform || isNamespaceAdmin(whoami, mine, namespace);
  // Only a platform admin fixes the deployment's setup; a namespace admin sees why it fails.
  const why = seesHealth
    ? "Loom retries the setup on its own; Admin › Health says why it fails."
    : "Loom retries the setup on its own.";
  const first = failing
    ? platform
      ? why
      : `${why} An admin of this deployment fixes it.`
    : "An operator sets up the assistant service for this deployment.";
  const reads = starters
    ? failing
      ? " Until it works, Loom can still answer these from its own reads."
      : " Until then, Loom can still answer these from its own reads."
    : "";
  return (
    <div
      className="flex flex-col items-center gap-2 py-4 text-center"
      data-testid="assistant-unavailable"
      data-reason={failing ? "failing" : "not_configured"}
    >
      <div className="grid h-10 w-10 place-items-center rounded-sm bg-surface-2 text-text-muted">
        <Icon name={failing ? "warning" : "sparkle"} size="md" aria-hidden="true" />
      </div>
      <Title type="h3" size="sm">
        {failing ? "Chat's setup is failing" : "Chat isn't set up"}
      </Title>
      <p className="max-w-sm text-sm text-text-muted">
        {first}
        {reads}
      </p>
      {failing ? (
        <div className="flex flex-wrap items-center justify-center gap-3">
          <Button type="secondary" htmlType="button" onClick={onTryAgain}>
            Try again
          </Button>
          {seesHealth && (
            <Link
              component="button"
              type="button"
              size="sm"
              onClick={() => useStore.getState().navigate("admin:health", { arriveFocus: "health-assistant" })}
            >
              See Health
            </Link>
          )}
        </div>
      ) : (
        <Link
          component="button"
          type="button"
          size="sm"
          onClick={() => openDocsPage(SETUP_DOCS.page, SETUP_DOCS.anchor)}
        >
          How to set up the assistant service
        </Link>
      )}
    </div>
  );
}

/** The org's model-call switch, read only for an org or platform admin after a refusal
 *  said model calls are off (org.model_calls is ORG_ADMIN). */
function useModelCalls(wanted: boolean, org: string): ModelCallsState {
  const whoami = useStore((s) => s.whoami);
  const [state, setState] = useState<ModelCallsState>("loading");
  useEffect(() => {
    if (!wanted || !org) return;
    const ctl = new AbortController();
    setState("loading");
    getOrgModelCalls(org, { signal: ctl.signal })
      .then((r) => {
        if (ctl.signal.aborted) return;
        if (r.enabled) setState("just_on");
        else if (r.locked && !hasPlatformAdminClaim(whoami)) setState("locked");
        else setState("admin");
      })
      .catch(() => {
        if (!ctl.signal.aborted) setState("unknown");
      });
    return () => ctl.abort();
  }, [wanted, org, whoami]);
  return wanted ? state : "unknown";
}
