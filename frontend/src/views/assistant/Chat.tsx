import { Suspense, lazy, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { IconButton } from "../../components/IconButton";
import { Alert, Chip, Unavailable } from "../../components/ui";
import { GatedIconButton } from "../../components/ui/GatedIconButton";
import { fieldError } from "../../components/ui/fieldError";
import { chatOff, useDock } from "../../lib/assistantDock";
import { announceAssistant } from "../../lib/assistantDockRuntime";
import { useStore } from "../../lib/state";
import { AnswerText, citationKey, citationNumbers, numberedCitations } from "./AnswerText";
import {
  CITE_SOURCES,
  ask,
  cancelEdit,
  draftEdited,
  editLast,
  lastAskedOf,
  latestStep,
  retry,
  sentQuestion,
  stop,
  useChat,
  type Turn,
} from "./chatStore";
import { Sources } from "./Sources";
import { MAX_MESSAGE_CHARS, contextLength, pageContext, withContext } from "./pageContext";

/** Where the chats are kept, said once under an empty chat; History says more. */
export const KEPT_HERE = "Chats are kept in this browser for 30 days.";

// A starter's card and its reads: a chunk of their own, loaded when a conversation shows one.
const Card = lazy(() => import("./cards/Card").then((m) => ({ default: m.Card })));

/** Whether Retry is offered on a turn that is the last one: an answer that ended, was
 *  stopped, or failed in a way the same question sent again may pass. */
const retryOffered = (t: Turn) =>
  t.state === "done" || t.state === "stopped" || ((t.state === "failed" || t.state === "refused") && !!t.retryable);

/** "What it read (n)"'s list: the answer's tool calls, a disclosure under it once it ends.
 *  A call that failed or was refused is muted, not an error: the answer may still stand. */
function StepsList({ turn, open, id }: { turn: Turn; open: boolean; id: string }) {
  const steps = turn.steps ?? [];
  return (
    <ol id={id} hidden={!open} className="mt-1 space-y-0.5 text-xs" data-testid="assistant-steps">
      {steps.map((step, i) => (
        // A step's words repeat when the answer made one call twice: keyed by place.
        <li key={i} className={step.ok ? "text-text" : "text-text-muted"} data-ok={step.ok ? "" : undefined}>
          {step.text}
        </li>
      ))}
      {turn.stepsCut && <li className="text-text-muted">and more</li>}
    </ol>
  );
}

/** Copy, as a small icon button: it copies the stored answer, Markdown and markers as
 *  written, and says "Copied" (or that the browser refused) for two seconds. */
function CopyAnswer({ text, answer }: { text: string; answer: number }) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = setTimeout(() => setState("idle"), 2000);
    return () => clearTimeout(t);
  }, [state]);
  return (
    <IconButton
      icon={state === "done" ? "check" : "copy"}
      label={`Copy answer ${answer}`}
      tooltip={state === "done" ? "Copied" : state === "failed" ? "Copy failed: select the text" : "Copy"}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setState("done");
          announceAssistant("Copied");
        } catch {
          setState("failed");
          announceAssistant("Copy failed: select the text");
        }
      }}
    />
  );
}

/** One finished answer's row under it: "What it read" at the start, Copy and Ask again at
 *  the end, then the steps the disclosure shows. */
function AnswerActions({
  turn,
  answer,
  canCopy,
  askAgain,
  blocked,
}: {
  turn: Turn;
  answer: number;
  canCopy: boolean;
  /** Ask again, offered on the last turn; absent elsewhere. */
  askAgain?: () => void;
  blocked: string | null;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const steps = turn.state === "done" && (turn.steps?.length ?? 0) > 0;
  if (!steps && !canCopy && !askAgain) return null;
  return (
    <div className="mt-1">
      <div className="flex flex-wrap items-center gap-1">
        {steps && (
          <Button
            type="empty"
            htmlType="button"
            iconRight={open ? "chevron-up" : "chevron-down"}
            aria-expanded={open}
            aria-controls={id}
            onClick={() => setOpen((v) => !v)}
          >
            {`What it read (${turn.steps?.length}${turn.stepsCut ? "+" : ""})`}
          </Button>
        )}
        <span className="ml-auto inline-flex items-center gap-1">
          {canCopy && <CopyAnswer text={turn.answer} answer={answer} />}
          {askAgain && (
            <GatedIconButton
              icon="refresh"
              label={`Ask again, answer ${answer}`}
              tooltip="Ask again"
              reason={blocked}
              onClick={askAgain}
            />
          )}
        </span>
      </div>
      {steps && <StepsList turn={turn} open={open} id={id} />}
    </div>
  );
}

/**
 * The assistant's chat: the conversation the panel shows and the question field. The
 * conversation and its answer live in the chat store (./chatStore.ts), so closing the
 * panel, a change of its mode or a navigation never stops an answer; the question being
 * typed is the panel's (lib/assistantDock.ts). `blocked` says why nothing can be asked now.
 */
export function AssistantChat({
  namespace,
  blocked,
  composer = true,
}: {
  namespace: string;
  blocked: string | null;
  /** False where nothing can be asked (the chat off, not set up or failing): no field. */
  composer?: boolean;
}) {
  const turns = useChat((s) => s.turns);
  const trimmed = useChat((s) => s.trimmed);
  const answering = useChat((s) => s.answering);
  const checkingSources = useChat((s) => s.checkingSources);
  const editing = useChat((s) => s.editing);
  // Kept only for an owner, in a browser that lets it save.
  const saved = useChat((s) => !!s.owner && !s.storageOff);
  const draft = useDock((s) => s.draft);
  const focusField = useDock((s) => s.focusField);
  const view = useStore((s) => s.view);
  const item = useDock((s) => s.item);
  const pageOff = useDock((s) => s.pageOff);
  const context = pageContext(view, item, pageOff);
  const field = useRef<HTMLTextAreaElement>(null);
  // The last turn, when it can be asked again or edited (nothing answering).
  const last = useChat(lastAskedOf);
  // A turn put back waits for Try again: no Ask again, Edit or "Ask again with sources"
  // replaces its words meanwhile. While nothing can be asked, Edit is off too (Ask again
  // shows, unavailable, with the reason).
  const waiting = useChat((s) => !!s.refused?.replace);
  const editable = !blocked && !waiting;
  // An edit holds while its turn is the last one: a turn another tab finished since ends it,
  // and Ask then asks a new question.
  const edited = editing && last?.id === editing.id ? last : null;
  // The same trimmed question `ask` sends, after its context line (and the sources' line an
  // edit asks with); past the route's length it says so on the field.
  const limit = MAX_MESSAGE_CHARS - contextLength(context) - (edited && editing?.withSources ? CITE_SOURCES.length : 0);
  const tooLong =
    draft.trim().length > limit ? `A question is at most ${limit.toLocaleString("en-US")} characters.` : null;
  // The button pressed goes once the answer starts: the focus goes to the question field.
  const askAgain = (t: Turn, withSources?: boolean) => {
    if (blocked) return;
    retry(t.id, namespace, { withSources });
    field.current?.focus();
  };
  const edit = () => {
    if (editLast()) field.current?.focus();
  };

  // The shell asks for the field's focus on an open the person started.
  useEffect(() => {
    if (!focusField || !field.current) return;
    field.current.focus();
    useDock.setState({ focusField: false });
  }, [focusField]);
  // The Stop button unmounts once the text is complete: a focus on it goes back to the
  // question field.
  const stoppable = answering && !checkingSources;
  const wasStoppable = useRef(stoppable);
  useEffect(() => {
    if (wasStoppable.current && !stoppable && document.activeElement === document.body) field.current?.focus();
    wasStoppable.current = stoppable;
  }, [stoppable]);

  const send = () => {
    const question = draft.trim();
    if (!question || question.length > limit || answering || blocked) return;
    const asked = { context, item: context ? item : null };
    // An edit answers its turn again.
    if (edited)
      ask(question, namespace, asked, null, {
        replace: edited.id,
        fromField: true,
        ...(editing?.withSources ? { withSources: true } : {}),
      });
    else ask(question, namespace, asked);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter asks, Shift+Enter starts a new line; an input method's Enter commits its text.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const sendBlocked =
    blocked ??
    (checkingSources
      ? "Loom is checking the answer's sources."
      : answering
        ? "An answer is still coming in."
        : !draft.trim()
          ? "Type a question."
          : tooLong);
  const error = fieldError("assistant-question-error", tooLong);
  return (
    // No min-h-0: the question form keeps its height inside the panel, the starters above
    // giving way first; the transcript's size containment leaves its turns out of that floor.
    <div className="flex flex-col flex-1">
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 [contain:size]" data-testid="assistant-transcript">
        <div className="assistant-column space-y-4">
          {trimmed && <p className="text-xs text-text-muted">Earlier turns were not kept.</p>}
          {turns.length > 0 && (
            <ol className="space-y-4" aria-label="Conversation">
              {turns.map((t, i) => {
                // What the turn offers: Copy on a finished answer; Ask again (Retry inside
                // a failure's alert) and "Ask again with sources" on the last one, outside
                // an edit. With the chat off a stored conversation is read-only: nothing
                // asks again.
                const again = !chatOff() && last?.id === t.id && !edited && !waiting;
                const canCopy = t.state === "done" && !!t.answer.trim();
                const canRetry = again && retryOffered(t);
                const canAskWithSources =
                  again &&
                  !t.withSources &&
                  withContext(sentQuestion(t.question, true), t.context).length <= MAX_MESSAGE_CHARS;
                // The citations are numbered as the search's card numbers them; a number
                // links to its Sources row once the answer is done and its check has ended.
                const listed = t.state === "done" && checkingSources !== t.id;
                const numbers = citationNumbers(t.answer);
                const rows = new Set(listed ? (t.citations ?? []).map((c) => citationKey(c.text)) : []);
                const rowPrefix = `assistant-${t.id}-source`;
                return (
                  <li key={t.id} className="space-y-2">
                    <div className="rounded-lg bg-surface-2 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <p className="section-label">You</p>
                        {last?.id === t.id && edited?.id !== t.id && !draft && editable && (
                          <IconButton icon="pencil" size="sm" label="Edit question" onClick={edit} />
                        )}
                      </div>
                      <p className="text-sm text-text whitespace-pre-wrap break-words">{t.question}</p>
                      {(t.context || t.withSources) && (
                        <div className="mt-2 flex flex-wrap gap-2">
                          {t.context && <Chip label={`Includes: ${t.context.chip}`} />}
                          {t.withSources && <Chip label="With sources" />}
                        </div>
                      )}
                    </div>
                    {t.card && (
                      <Suspense fallback={null}>
                        <Card turn={t} />
                      </Suspense>
                    )}
                    {t.state !== "card" && (
                      <div className="px-3" aria-busy={t.state === "answering" || undefined}>
                        <p className="section-label">Ask AI</p>
                        {t.answer && <AnswerText text={t.answer} cite={numberedCitations(numbers, rows, rowPrefix)} />}
                        {/* The working line: the latest step while the answer reads, never blank. */}
                        {t.state === "answering" && (!t.answer || latestStep(t)) && (
                          <p className={t.answer ? "text-xs text-text-muted" : "text-sm text-text-muted"}>
                            {latestStep(t) ?? "Answering…"}
                          </p>
                        )}
                        {/* The label's slot: one line while the answer's sources are checked. */}
                        {t.state === "done" && checkingSources === t.id && (
                          <p className="mt-2 text-xs text-text-muted">Checking sources…</p>
                        )}
                        {listed && (
                          <Sources
                            citations={t.citations ?? []}
                            support={t.support}
                            checkLost={t.checkLost}
                            unknownNames={t.unknownNames ?? []}
                            answer={i + 1}
                            blocked={blocked}
                            askAgain={canAskWithSources ? () => askAgain(t, true) : undefined}
                            numbers={numbers}
                            idPrefix={rowPrefix}
                          />
                        )}
                        {t.state === "stopped" && <p className="text-xs text-text-muted mt-1">Stopped.</p>}
                        {t.error && (
                          <div className="mt-2">
                            <Alert
                              role="alert"
                              state="danger"
                              size="small"
                              text={
                                <span className="flex flex-col items-start gap-2">
                                  <span>{t.error}</span>
                                  {canRetry && (
                                    <Unavailable reason={blocked}>
                                      <Button
                                        type="secondary"
                                        htmlType="button"
                                        iconLeft="refresh"
                                        onClick={() => askAgain(t)}
                                      >
                                        Retry
                                      </Button>
                                    </Unavailable>
                                  )}
                                </span>
                              }
                            />
                          </div>
                        )}
                        <AnswerActions
                          turn={t}
                          answer={i + 1}
                          canCopy={canCopy}
                          askAgain={canRetry && !t.error ? () => askAgain(t) : undefined}
                          blocked={blocked}
                        />
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      </div>
      {/* With the chat off, not set up or failing nothing can be asked: the panel is its
          starters. */}
      {composer && !chatOff() && (
        <form
          className="px-4 py-3 border-t border-border shrink-0"
          aria-label="Ask AI"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <div className="assistant-column space-y-2">
            {context && (
              <div className="flex min-w-0 flex-wrap items-center gap-2" data-testid="assistant-page-chip">
                <Chip
                  label={`Includes: ${context.chip}`}
                  tip="Your question names this page; × leaves it out"
                  removeLabel="Don't include this page"
                  onRemove={() => {
                    useDock.setState({ pageOff: true, item: null });
                    field.current?.focus();
                  }}
                />
              </div>
            )}
            {edited && (
              <div className="flex flex-wrap items-center gap-2" data-testid="assistant-editing">
                <p className="text-xs text-text-muted">
                  {editing?.withSources
                    ? "Editing your last question: Ask replaces its answer, asking with sources."
                    : "Editing your last question: Ask replaces its answer."}
                </p>
                <Button
                  type="secondary"
                  htmlType="button"
                  onClick={() => {
                    cancelEdit();
                    field.current?.focus();
                  }}
                >
                  Cancel edit
                </Button>
              </div>
            )}
            <TextAreaField
              ref={field}
              id="assistant-question"
              label="Question"
              placeholder={turns.length === 0 ? "Ask about this namespace" : "Ask a follow-up"}
              rows={3}
              value={draft}
              onChange={(v) => {
                useDock.setState({ draft: v });
                draftEdited();
              }}
              onKeyDown={onKeyDown}
              {...error}
            />
            <div className="flex flex-wrap items-center justify-end gap-2">
              {stoppable && (
                <Button type="secondary" htmlType="button" onClick={stop}>
                  Stop
                </Button>
              )}
              <Unavailable reason={sendBlocked}>
                <Button type="primary" htmlType="submit">
                  Ask
                </Button>
              </Unavailable>
            </div>
            {turns.length === 0 && saved && <p className="text-xs text-text-muted">{KEPT_HERE}</p>}
          </div>
        </form>
      )}
    </div>
  );
}
