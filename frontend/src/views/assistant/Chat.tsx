import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { type ChatMessage, type Citation, type Support, streamAssistantChat } from "../../api/assistantChat";
import { Alert, Unavailable } from "../../components/ui";
import { fieldError } from "../../components/ui/fieldError";
import { ApiError } from "../../lib/errors";
import { humanizeError } from "../../lib/errorWords";
import { AnswerText } from "./AnswerText";
import { Sources, supportLabel } from "./Sources";

/**
 * The assistant's chat. The conversation lives in the tab's memory, kept under
 * `conversationKey` (the signed-in subject, the namespace and the token scope) so it is
 * still there after opening a cited document and coming back: Loom stores none of it,
 * so each question goes with the earlier questions and answers. A change of key starts
 * a new conversation (and an answer still streaming is stopped).
 */

type TurnState = "answering" | "done" | "stopped" | "failed" | "refused";

interface Turn {
  id: number;
  question: string;
  answer: string;
  state: TurnState;
  error?: string;
  /** The answer's citations as the server checked them; absent until the answer ends, or
   *  from a server that sends none. */
  citations?: Citation[];
  /** How far the answer rests on Loom; absent until the answer ends. */
  support?: Support;
}

// A refusal before the stream or an error event inside it, worded as every error is
// (lib/errorWords.ts): an answer only reads, so nothing is left to check before asking again.
function refusalText(e: unknown): string {
  return humanizeError(e, { read: true });
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

// Each conversation of this tab by its key; an answer cut off by leaving the page shows as stopped.
const kept = new Map<string, Turn[]>();
const keptTurns = (key: string): Turn[] =>
  (kept.get(key) ?? []).map((t) => (t.state === "answering" ? { ...t, state: "stopped" } : t));

export function AssistantChat({
  namespace,
  blocked,
  conversationKey,
}: {
  namespace: string;
  blocked: string | null;
  conversationKey: string;
}) {
  const [turns, setTurns] = useState<Turn[]>(() => keptTurns(conversationKey));
  const [draft, setDraft] = useState("");
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const controller = useRef<AbortController | null>(null);
  const nextId = useRef(Math.max(0, ...turns.map((t) => t.id)) + 1);
  const field = useRef<HTMLTextAreaElement>(null);
  const stopButton = useRef<HTMLButtonElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  const answering = turns.some((t) => t.state === "answering");

  useEffect(() => {
    kept.set(conversationKey, turns);
  }, [conversationKey, turns]);
  // The form unmounts for the unavailable state, and back: the focus goes with it.
  const wasUnavailable = useRef(false);
  useEffect(() => {
    if (unavailable !== null) retryButton.current?.focus();
    else if (wasUnavailable.current) field.current?.focus();
    wasUnavailable.current = unavailable !== null;
  }, [unavailable]);

  // Leaving the page (or the namespace, which remounts this) closes the answer's request.
  useEffect(() => () => controller.current?.abort(), []);

  const update = (id: number, change: (t: Turn) => Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...change(t) } : t)));

  const finish = (id: number, change: Partial<Turn>, said: string) => {
    // The Stop button unmounts with the answer: a focus on it goes back to the question field.
    if (document.activeElement === stopButton.current) field.current?.focus();
    update(id, () => change);
    setAnnounce(said);
  };

  const ask = () => {
    const question = draft.trim();
    if (!question || question.length > MAX_MESSAGE_CHARS || answering || blocked) return;
    const id = nextId.current++;
    const messages = conversation(turns, question);
    const mine = new AbortController();
    controller.current = mine;
    setTurns((ts) => [...ts, { id, question, answer: "", state: "answering" }]);
    setDraft("");
    setAnnounce("The assistant is answering.");
    let streamed = false;
    // The label, said with the end of the answer.
    let labelled = "";
    streamAssistantChat(
      namespace,
      messages,
      {
        onText: (text) => {
          streamed = true;
          update(id, (t) => ({ answer: t.answer + text }));
        },
        onCitations: (citations, support) => {
          labelled = supportLabel(support, citations)?.text ?? "";
          update(id, () => ({ citations, support }));
        },
      },
      mine.signal,
    )
      .then(() => finish(id, { state: "done" }, `The answer is complete.${labelled ? ` ${labelled}.` : ""}`))
      .catch((e: unknown) => {
        if (isAbort(e)) {
          finish(id, { state: "stopped" }, "The answer was stopped.");
          return;
        }
        // A refusal before the stream started is an HTTP error (status set); one inside
        // the stream is an error event (status 0) after part of the answer.
        const before = e instanceof ApiError && e.status !== 0 && !streamed;
        if (before && e.code === "ASSISTANT_UNAVAILABLE") {
          // Nothing to ask until the deployment sets the assistant up: the turn goes, and
          // the question waits in the field for a later try.
          setTurns((ts) => ts.filter((t) => t.id !== id));
          setDraft((d) => d || question);
          setUnavailable(e.serverMessage || "");
          setAnnounce("");
          return;
        }
        // A refused question was never answered: it waits in the field to be asked again.
        if (before) setDraft((d) => d || question);
        finish(id, { state: before ? "refused" : "failed", error: refusalText(e) }, "");
      })
      .finally(() => {
        if (controller.current === mine) controller.current = null;
      });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter asks, Shift+Enter starts a new line; an input method's Enter commits its text.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      ask();
    }
  };

  if (unavailable !== null)
    return (
      <div className="space-y-3" data-testid="assistant-chat-unavailable">
        <Alert
          role="status"
          state="neutral"
          size="medium"
          text={
            <>
              <strong>The assistant is not available on this deployment.</strong>{" "}
              {unavailable && <span className="break-words">{unavailable}</span>}
            </>
          }
        />
        <Button ref={retryButton} type="secondary" htmlType="button" onClick={() => setUnavailable(null)}>
          Try again
        </Button>
      </div>
    );

  // The same trimmed question `ask` sends; past the route's length it says so on the field.
  const tooLong =
    draft.trim().length > MAX_MESSAGE_CHARS
      ? `A question is at most ${MAX_MESSAGE_CHARS.toLocaleString("en-US")} characters.`
      : null;
  const sendBlocked =
    blocked ?? (answering ? "An answer is still coming in." : !draft.trim() ? "Type a question." : tooLong);
  const error = fieldError("assistant-question-error", tooLong);
  return (
    <div className="space-y-4">
      {turns.length > 0 && (
        <ol className="space-y-4" aria-label="Conversation">
          {turns.map((t, i) => (
            <li key={t.id} className="space-y-2">
              <div className="rounded-lg bg-surface-2 p-3">
                <p className="section-label">You</p>
                <p className="text-sm text-text whitespace-pre-wrap break-words">{t.question}</p>
              </div>
              <div className="px-3" aria-busy={t.state === "answering" || undefined}>
                <p className="section-label">Assistant</p>
                {t.answer && <AnswerText text={t.answer} />}
                {t.state === "answering" && !t.answer && <p className="text-sm text-text-muted">Answering…</p>}
                {t.state === "done" && t.support && (
                  <Sources citations={t.citations ?? []} support={t.support} answer={i + 1} />
                )}
                {t.state === "stopped" && <p className="text-xs text-text-muted mt-1">Stopped.</p>}
                {t.error && (
                  <div className="mt-2">
                    <Alert role="alert" state="danger" size="small" text={t.error} />
                  </div>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
      <form
        className="space-y-2"
        aria-label="Ask the assistant"
        onSubmit={(e) => {
          e.preventDefault();
          ask();
        }}
      >
        <TextAreaField
          ref={field}
          id="assistant-question"
          label="Question"
          placeholder="What does this namespace know about invoices?"
          rows={3}
          value={draft}
          onChange={(v) => setDraft(v)}
          onKeyDown={onKeyDown}
          {...error}
          aria-describedby={["assistant-question-hint", error["aria-describedby"]].filter(Boolean).join(" ")}
        />
        <p id="assistant-question-hint" className="text-xs text-text-muted">
          Enter asks, Shift+Enter adds a line. Each answer's citations are checked against Loom as you when it ends.
        </p>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {turns.length > 0 && !answering && (
            <Button
              type="secondary"
              htmlType="button"
              onClick={() => {
                // The button goes with the conversation: the focus goes to the question field.
                setTurns([]);
                field.current?.focus();
              }}
            >
              Clear the conversation
            </Button>
          )}
          {answering && (
            <Button ref={stopButton} type="secondary" htmlType="button" onClick={() => controller.current?.abort()}>
              Stop
            </Button>
          )}
          <Unavailable reason={sendBlocked}>
            <Button type="primary" htmlType="submit">
              Ask
            </Button>
          </Unavailable>
        </div>
      </form>
      <p className="sr-only" role="status">
        {announce}
      </p>
    </div>
  );
}
