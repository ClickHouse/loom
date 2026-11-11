import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { Alert, Chip, Unavailable } from "../../components/ui";
import { fieldError } from "../../components/ui/fieldError";
import { useDock } from "../../lib/assistantDock";
import { useStore } from "../../lib/state";
import { AnswerText } from "./AnswerText";
import { MAX_MESSAGE_CHARS, ask, draftEdited, stop, useChat } from "./chatStore";
import { Sources } from "./Sources";
import { markPageHintSeen, pageContext, pageHintSeen } from "./pageContext";

/**
 * The assistant's chat: the conversation the panel shows and the question field. The
 * conversation and its answer live in the chat store (./chatStore.ts), so closing the
 * panel, a change of its mode or a navigation never stops an answer; the question being
 * typed is the panel's (lib/assistantDock.ts). `blocked` says why nothing can be asked now.
 */
export function AssistantChat({ namespace, blocked }: { namespace: string; blocked: string | null }) {
  const turns = useChat((s) => s.turns);
  const trimmed = useChat((s) => s.trimmed);
  const answering = useChat((s) => s.answering);
  const draft = useDock((s) => s.draft);
  const focusField = useDock((s) => s.focusField);
  const view = useStore((s) => s.view);
  const item = useDock((s) => s.item);
  const pageOff = useDock((s) => s.pageOff);
  const context = pageContext(view, item, pageOff);
  // The same trimmed question `ask` sends, after its context line; past the route's length
  // it says so on the field.
  const limit = MAX_MESSAGE_CHARS - (context ? context.line.length + 2 : 0);
  const tooLong =
    draft.trim().length > limit ? `A question is at most ${limit.toLocaleString("en-US")} characters.` : null;
  // Shown until the chip is removed once or a first question is asked.
  const [hint, setHint] = useState(() => !pageHintSeen());
  const hintSeen = () => {
    markPageHintSeen();
    setHint(false);
  };
  const field = useRef<HTMLTextAreaElement>(null);

  // The shell asks for the field's focus on an open the person started.
  useEffect(() => {
    if (!focusField || !field.current) return;
    field.current.focus();
    useDock.setState({ focusField: false });
  }, [focusField]);
  // The Stop button unmounts with the answer: a focus on it goes back to the question field.
  const wasAnswering = useRef(answering);
  useEffect(() => {
    if (wasAnswering.current && !answering && document.activeElement === document.body) field.current?.focus();
    wasAnswering.current = answering;
  }, [answering]);

  const send = () => {
    const question = draft.trim();
    if (!question || question.length > limit || answering || blocked) return;
    if (hint) hintSeen();
    ask(question, namespace, { context, item: context ? item : null });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter asks, Shift+Enter starts a new line; an input method's Enter commits its text.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const sendBlocked =
    blocked ?? (answering ? "An answer is still coming in." : !draft.trim() ? "Type a question." : tooLong);
  const error = fieldError("assistant-question-error", tooLong);
  return (
    <div className="flex flex-col min-h-0 flex-1">
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-4" data-testid="assistant-transcript">
        {trimmed && <p className="text-xs text-text-muted">Earlier turns were not kept.</p>}
        {turns.length > 0 && (
          <ol className="space-y-4" aria-label="Conversation">
            {turns.map((t, i) => (
              <li key={t.id} className="space-y-2">
                <div className="rounded-lg bg-surface-2 p-3">
                  <p className="section-label">You</p>
                  <p className="text-sm text-text whitespace-pre-wrap break-words">{t.question}</p>
                  {t.context && (
                    <div className="mt-2">
                      <Chip label={`Includes: ${t.context.chip}`} />
                    </div>
                  )}
                </div>
                <div className="px-3" aria-busy={t.state === "answering" || undefined}>
                  <p className="section-label">Assistant</p>
                  {t.answer && <AnswerText text={t.answer} />}
                  {t.state === "answering" && !t.answer && <p className="text-sm text-text-muted">Answering…</p>}
                  {t.state === "done" && t.support && (
                    <Sources
                      citations={t.citations ?? []}
                      support={t.support}
                      unknownNames={t.unknownNames ?? []}
                      answer={i + 1}
                    />
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
      </div>
      <form
        className="space-y-2 px-4 py-3 border-t border-border shrink-0"
        aria-label="Ask the assistant"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        {context && (
          <div className="flex min-w-0 flex-wrap items-center gap-2" data-testid="assistant-page-chip">
            <Chip
              label={`Includes: ${context.chip}`}
              removeLabel="Don't include this page"
              onRemove={() => {
                useDock.setState({ pageOff: true, item: null });
                if (hint) hintSeen();
                field.current?.focus();
              }}
            />
          </div>
        )}
        {context && hint && <p className="text-xs text-text-muted">Your question names this page. Remove it here.</p>}
        <TextAreaField
          ref={field}
          id="assistant-question"
          label="Question"
          placeholder="What does this namespace know about invoices?"
          rows={3}
          value={draft}
          onChange={(v) => {
            useDock.setState({ draft: v });
            draftEdited();
          }}
          onKeyDown={onKeyDown}
          {...error}
          aria-describedby={["assistant-question-hint", error["aria-describedby"]].filter(Boolean).join(" ")}
        />
        <p id="assistant-question-hint" className="text-xs text-text-muted">
          Enter asks, Shift+Enter adds a line. Each answer's citations are checked against Loom as you when it ends.
        </p>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {answering && (
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
      </form>
    </div>
  );
}
