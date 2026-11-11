import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Link } from "@clickhouse/click-ui/Link";
import { isAuthDisabled, isProxyMode, storedToken } from "../../api";
import { getOrgModelCalls } from "../../api/guardrails";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { ListTime } from "../../components/ui/ListTime";
import { SectionHeading } from "../../components/ui/SectionHeading";
import { useDock } from "../../lib/assistantDock";
import {
  claimReason,
  setDockReason,
  useDockMode,
  type AssistantReason,
  type DockMode,
} from "../../lib/assistantLayout";
import { announceAssistant, closeAssistant } from "../../lib/assistantDockRuntime";
import { CONVERSATION_PREFIX, EPOCH_KEY, INDEX_KEY, type Owner } from "../../lib/assistantStorage";
import { canReadNamespace, isMemoryNamespace } from "../../lib/namespace";
import { hasOrgAdminClaim, hasPlatformAdminClaim, isNamespaceAdmin } from "../../lib/role";
import { useStore } from "../../lib/state";
import { AssistantChat } from "./Chat";
import {
  MAX_MESSAGE_CHARS,
  useChat,
  newConversation,
  openConversation,
  resetChat,
  saveOnHide,
  ask,
  storedChanged,
} from "./chatStore";
import { adoptEpoch, deleteAllConversations, deleteConversation, listConversations, sameOwner } from "./conversations";
import { currentAsk, withContext } from "./pageContext";
import { readReadiness } from "./readiness";
import { tokenReadScope } from "./tokenScope";

/** How often the panel reads readiness again while the assistant is setting up or failing. */
export const READINESS_POLL_MS = 10_000;

/** The owner the conversations are keyed by: the person, the org, the token's read scope
 *  and read user. Behind a proxy, whoami's subject (no token). With auth off, none: nothing
 *  is stored. */
function ownerOf(whoami: ReturnType<typeof useStore.getState>["whoami"]): Owner | null {
  if (isAuthDisabled() || !whoami?.subject) return null;
  if (isProxyMode()) return { subject: whoami.subject, org: whoami.org ?? "", scope: "-", user: "-" };
  const scope = tokenReadScope(storedToken());
  return {
    subject: whoami.subject,
    org: whoami.org ?? "",
    scope: scope?.readScope ?? "-",
    user: scope?.readUserId ?? "-",
  };
}

type ModelCallsState = "loading" | "admin" | "locked" | "just_on" | "unknown";

/** Why nothing can be asked now, first match; null when a question can be asked. Ask and
 *  Try again both go by it. */
function blockedBy(c: {
  reason: AssistantReason | null;
  checking: boolean;
  memory: boolean;
  mismatch: boolean;
}): string | null {
  if (c.reason) return "The assistant can't answer now: the panel says why.";
  if (c.checking) return "Getting the assistant ready.";
  if (!c.memory) return "The assistant answers about a memory namespace.";
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
  const hint = useChat((s) => s.hint);
  const readiness = useChat((s) => s.readiness);
  const [showHistory, setShowHistory] = useState(false);
  // A closed panel reopens on the chat, whose field takes the focus.
  useEffect(() => {
    if (!open) setShowHistory(false);
  }, [open]);
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
    const c = useChat.getState();
    const header = useStore.getState().identity?.namespace ?? "";
    if (c.answering || c.turns.length === 0 || !c.namespace || c.namespace === header) return;
    const old = c.namespace;
    newConversation();
    const line = `New conversation in ${header}; the one about ${old} is in History.`;
    useChat.setState({ notice: line });
    announceAssistant(line);
  }, [askSeq]);
  const [checking, setChecking] = useState(true);
  // Bumped to draw History again: a write to the index here or in another tab.
  const [, setIndexTick] = useState(0);

  // The person (or their token's scope) changed: what was shown is not theirs.
  useEffect(() => {
    const s = useChat.getState();
    if (s.owner && owner && sameOwner(s.owner, owner)) return;
    resetChat(owner);
    adoptEpoch(isProxyMode() ? (owner?.subject ?? null) : null);
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
    if (!open || isAuthDisabled()) {
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
  }, [open, readTick]);
  // The setup is in progress or failing by readiness or by the route's refusal: read again.
  const settingUp = (k?: string) => k === "starting" || k === "failing";
  const polling = open && (settingUp(readiness?.kind) || settingUp(hint?.kind));
  useEffect(() => {
    if (!polling) return;
    const t = setInterval(() => setReadTick((n) => n + 1), READINESS_POLL_MS);
    return () => clearInterval(t);
  }, [polling]);

  // The reason shown, first match: the token's claims, the route's refusal, readiness.
  const claim = claimReason();
  const reason = claim ?? hint ?? readiness;
  useEffect(() => {
    setDockReason(reason);
  }, [reason]);

  const tryAgain = async () => {
    setChecking(true);
    const r = await readReadiness();
    useChat.setState({ readiness: r, hint: null });
    setChecking(false);
    const reasonNow = claimReason() ?? r;
    // Try again goes with the reason: the focus goes to the question field.
    if (!reasonNow) useDock.setState({ focusField: true });
    // A question the route refused is asked again, in the conversation's namespace, unless
    // what blocks Ask still applies; with nothing refused, nothing is sent.
    const refused = useChat.getState().refused;
    const c = useChat.getState();
    const st = useStore.getState();
    const header = st.identity?.namespace ?? "";
    const stillBlocked = blockedBy({
      reason: reasonNow,
      checking: false,
      memory: isMemoryNamespace(header, st.myNamespaces),
      mismatch: c.turns.length > 0 && c.namespace !== null && c.namespace !== header,
    });
    // It goes with what the chip shows now: a chip removed since sends nothing about the page.
    // A line longer now than when it was asked (an item added since) can push the message
    // past the route's length: nothing is sent. The question waits in an empty field, which
    // says why; a field holding other text keeps it, and a line in the panel says why.
    if (!refused || stillBlocked) return;
    const asked = currentAsk();
    if (withContext(refused, asked.context).length > MAX_MESSAGE_CHARS) {
      if (!useDock.getState().draft) useDock.setState({ draft: refused });
      else {
        const limit = MAX_MESSAGE_CHARS - (asked.context ? asked.context.line.length + 2 : 0);
        const line = `Not sent: with this page's line the question is longer than ${limit.toLocaleString("en-US")} characters.`;
        useChat.setState({ notice: line });
        announceAssistant(line);
      }
      return;
    }
    ask(refused, c.namespace ?? header, asked);
  };

  if (expired)
    return (
      <p className="px-4 py-3 text-sm text-text-muted" role="status">
        Your session ended. Sign in again to see your conversations.
      </p>
    );

  const memory = isMemoryNamespace(namespace, mine);
  const mismatch = turns.length > 0 && convNamespace !== null && convNamespace !== namespace;
  const blocked = blockedBy({ reason, checking, memory, mismatch });
  const readable = (ns: string) => ns === namespace || canReadNamespace(ns, whoami?.namespaces ?? [], mine);

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <Toolbar
        mode={mode}
        namespace={namespace}
        history={showHistory}
        onHistory={() => setShowHistory((v) => !v)}
        canStartNew={!answering && turns.length > 0}
      />
      {showHistory && owner ? (
        <History
          owner={owner}
          readable={readable}
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
          <div className="px-4 pt-3 space-y-2 shrink-0">
            {reason ? (
              <ReasonBody reason={reason} onTryAgain={() => void tryAgain()} />
            ) : checking ? (
              <p className="text-sm text-text-muted" role="status">
                Getting the assistant ready…
              </p>
            ) : !memory ? (
              <p className="text-sm text-text-muted">
                The assistant answers about a memory namespace. Pick one in the namespace menu.
              </p>
            ) : mismatch && convNamespace ? (
              <Mismatch old={convNamespace} now={namespace} />
            ) : turns.length === 0 ? (
              <EmptyState />
            ) : null}
            {storageOff && (
              <p className="text-xs text-text-muted">
                This browser refused to save conversations: this one lasts until the tab closes.
              </p>
            )}
            {notice && <p className="text-sm text-text-muted">{notice}</p>}
          </div>
          <AssistantChat namespace={convNamespace ?? namespace} blocked={blocked} />
        </>
      )}
    </div>
  );
}

function Toolbar({
  mode,
  namespace,
  history,
  onHistory,
  canStartNew,
}: {
  mode: DockMode;
  namespace: string;
  history: boolean;
  onHistory: () => void;
  canStartNew: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 px-4 py-2 border-b border-border shrink-0">
      {mode === "push" ? (
        <span className="text-xs text-text-muted truncate">ns {namespace}</span>
      ) : (
        // The header's picker is covered at these widths: this one closes the panel and
        // opens it.
        <Link
          component="button"
          type="button"
          size="sm"
          onClick={() => {
            closeAssistant("nav");
            useStore.getState().openNamespacePicker(true);
          }}
        >
          {`ns ${namespace}`}
        </Link>
      )}
      <div className="flex-1" />
      <Button type="secondary" htmlType="button" aria-pressed={history} onClick={onHistory}>
        History
      </Button>
      {!history && canStartNew && (
        <Button
          type="secondary"
          htmlType="button"
          onClick={() => {
            newConversation();
            useDock.setState({ focusField: true });
          }}
        >
          New conversation
        </Button>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="text-sm text-text-muted space-y-1">
      <p>Ask about what this namespace holds.</p>
      <p className="text-xs">Each answer calls a model, which counts against this namespace's model budget.</p>
      <p className="text-xs">{savedHere()}</p>
    </div>
  );
}

/** Where the conversations are kept, said where they are listed. */
function savedHere(): string {
  return isProxyMode()
    ? "Saved in this browser only, for 30 days."
    : "Saved in this browser only, for 30 days. Signing out deletes them.";
}

function Mismatch({ old, now }: { old: string; now: string }) {
  const identity = useStore((s) => s.identity);
  return (
    <div className="space-y-2">
      <p className="text-sm text-text">This conversation is about {old}.</p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="secondary"
          htmlType="button"
          onClick={() => {
            newConversation();
            announceAssistant(`New conversation in ${now}; the one about ${old} is in History.`);
            useDock.setState({ focusField: true });
          }}
        >
          {`Start a new one in ${now}`}
        </Button>
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
      </div>
    </div>
  );
}

function History({
  owner,
  readable,
  onDeleted,
  onOpen,
}: {
  owner: Owner;
  readable: (ns: string) => boolean;
  /** A Delete or Delete all here: DockBody draws the list again, as for another tab's. */
  onDeleted: () => void;
  onOpen: (entry: { id: string; namespace: string }) => Promise<void>;
}) {
  const headingId = useId();
  const list = useRef<HTMLUListElement>(null);
  // Where the focus goes once a Delete or Delete all has drawn: a row's id, or the heading.
  const [focusTo, setFocusTo] = useState<string | null>(null);
  useEffect(() => {
    if (!focusTo) return;
    const row = list.current?.querySelector<HTMLElement>(`[data-conversation="${focusTo}"] button`);
    (row ?? document.getElementById(headingId))?.focus();
    setFocusTo(null);
  }, [focusTo, headingId]);
  // Read on each render (cheap: one small key): DockBody draws it again on a delete here or
  // a write to the index in another tab.
  const entries = listConversations(owner, readable);
  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-3">
      <SectionHeading label="History" level={3} id={headingId} focusable />
      {entries.length === 0 ? (
        <p className="text-sm text-text-muted">No conversations saved here yet.</p>
      ) : (
        <ul ref={list} className="space-y-1" aria-label="Saved conversations">
          {entries.map((e, i) => (
            <li key={e.id} data-conversation={e.id} className="flex items-center gap-2">
              <Link component="button" type="button" size="sm" onClick={() => void onOpen(e)}>
                {e.title || "Untitled"}
              </Link>
              <span className="text-xs text-text-muted truncate">
                {e.namespace} · <ListTime time={new Date(e.updated).toISOString()} />
              </span>
              <div className="flex-1" />
              <Button
                type="secondary"
                htmlType="button"
                aria-label={`Delete ${e.title || "this conversation"}`}
                onClick={() => {
                  // The conversation shown goes first, an answer in it stopped: nothing
                  // writes it after the delete.
                  if (useChat.getState().id === e.id) resetChat(owner);
                  deleteConversation(e.id);
                  // The button goes with its row: the focus goes to the next row, or the heading.
                  setFocusTo((entries[i + 1] ?? entries[i - 1])?.id ?? "heading");
                  onDeleted();
                }}
              >
                Delete
              </Button>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-text-muted">{savedHere()}</p>
      {entries.length > 0 && (
        <div className="space-y-1">
          <Button
            type="danger"
            htmlType="button"
            onClick={() =>
              void confirmAction({
                title: "Delete every saved conversation?",
                message: "They are deleted from this browser.",
                confirmLabel: "Delete all",
                danger: true,
              }).then((ok) => {
                if (!ok) return;
                deleteAllConversations(owner);
                resetChat(owner);
                // The button goes with the list: the focus goes to the heading.
                setFocusTo("heading");
                onDeleted();
              })
            }
          >
            Delete all
          </Button>
          <p className="text-xs text-text-muted">Deleting a memory doesn't change answers already saved here.</p>
        </div>
      )}
    </div>
  );
}

/** Why the assistant cannot answer, in words that say who fixes it and where (the link
 *  goes only to someone who can open the page it names). */
function ReasonBody({ reason, onTryAgain }: { reason: AssistantReason; onTryAgain: () => void }) {
  const whoami = useStore((s) => s.whoami);
  const mine = useStore((s) => s.myNamespaces);
  const identity = useStore((s) => s.identity);
  const platform = hasPlatformAdminClaim(whoami);
  const orgAdmin = hasOrgAdminClaim(whoami);
  const seesHealth = isNamespaceAdmin(whoami, mine, identity?.namespace ?? "");
  const modelCalls = useModelCalls(reason.kind === "model_calls_off" && (platform || orgAdmin), identity?.org ?? "");
  const health = (label: string) => (
    <Link
      component="button"
      type="button"
      size="sm"
      onClick={() => useStore.getState().navigate("admin:health", { arriveFocus: "health-assistant" })}
    >
      {label}
    </Link>
  );
  const guardrails = (
    <Link component="button" type="button" size="sm" onClick={() => useStore.getState().navigate("admin:guardrails")}>
      Admin › Guardrails
    </Link>
  );
  const retries = "Loom retries the setup on its own; this updates when it works.";
  let text: ReactNode;
  switch (reason.kind) {
    case "auth_off":
      text =
        "The assistant answers as the signed-in person, so it needs auth on. An operator turns auth on for this deployment.";
      break;
    case "org_wide":
      text = "This token works across every org; the assistant answers in one. Sign in with a token for one org.";
      break;
    case "narrowed":
      text = `This token can't use the assistant: it is narrowed to ${reason.scope}. Sign in with a token that reads the whole namespace.`;
      break;
    case "token_expires_soon":
      text = `This token expires ${reason.leftS === undefined ? "" : `in ${reason.leftS} s, `}sooner than an answer can take. Sign in again or use a longer-lived token.`;
      break;
    case "starting":
      text = "Setting up — usually under a minute. This updates on its own.";
      break;
    case "not_configured":
      text = platform ? (
        <>Set up the assistant: {health("see Health")}</>
      ) : (
        "An admin of this deployment sets up the assistant."
      );
      break;
    case "failing":
      text = platform ? (
        <>
          The assistant's setup failed: {health("see Health")}. {retries}
        </>
      ) : seesHealth ? (
        <>
          An admin of this deployment fixes the assistant's setup. {retries} Details: {health("see Health")}
        </>
      ) : (
        `An admin of this deployment fixes the assistant's setup. ${retries}`
      );
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
