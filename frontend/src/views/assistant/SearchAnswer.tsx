// The header search's answer card (components/HeaderSearch.tsx): a question asked from the
// search box goes through the panel's chat store, into the conversation the panel shows, and
// the card shows that turn's answer as it streams. A chunk of its own, loaded once the search
// shows the card.
import { useEffect, useId, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Link } from "@clickhouse/click-ui/Link";
import { Alert } from "../../components/ui/Alert";
import { Kbd } from "../../components/ui/Kbd";
import { useDock } from "../../lib/assistantDock";
import { SHORT_REASON } from "../../lib/assistantLayout";
import { isApplePlatform } from "../../lib/platform";
import { useStore } from "../../lib/state";
import { citationKey, inline, parseBlocks, type CitationRenderer } from "./AnswerText";
import { Sources } from "./Sources";
import { announceAssistant } from "../../lib/assistantDockRuntime";
import { OTHER_TAB, adoptOwner, ask, latestStep, ownerOf, startIn, useChat } from "./chatStore";
import { pageContext } from "./pageContext";

/**
 * Ask `question` as the panel's Ask would: in the conversation shown when it is about the
 * header's namespace, else in a new one (the other stays in History, and the panel says
 * so). It carries the page line (none when the person removed the panel's page chip), not
 * an item "Ask about this" set for the panel's own field, and leaves what is typed in the
 * panel's field alone, unless that is this question, given back there earlier. False when
 * an answer is still coming in, which nothing interrupts: the question then waits in the
 * panel's empty field.
 */
export function askFromSearch(question: string, onTurn: (turnId: string) => void): boolean {
  const st = useStore.getState();
  const header = st.identity?.namespace ?? "";
  if (useChat.getState().answering) {
    // Not asked: the question waits in the panel's field, unless something is typed there,
    // the panel's chip as it is.
    if (!useDock.getState().draft) useDock.setState({ draft: question });
    return false;
  }
  // The panel's body adopts the same owner when it mounts, and so keeps this answer.
  adoptOwner(ownerOf(st.whoami));
  const line = startIn(header);
  // A question given back to the panel's field before (busy, another tab) is asked now: it
  // leaves the field, so the panel does not ask it again.
  if (useDock.getState().draft === question) useDock.setState({ draft: "" });
  const context = pageContext(st.view, null, useDock.getState().pageOff);
  ask(question, header, { context, item: null }, null, { fromField: false, keepChip: true, shown: onTurn });
  // Said as the panel's Ask about this says it (ask() clears the notice as it starts).
  if (line) {
    useChat.setState({ notice: line });
    announceAssistant(line);
  }
  return true;
}

/** Each distinct marker the preview `text` draws, numbered by where it first appears: the
 *  same inline pass the preview renders, so a marker inside a code span (drawn as code) gets
 *  none. */
function numbering(text: string): Map<string, number> {
  const numbers = new Map<string, number>();
  inline(text, undefined, (marker) => {
    if (!numbers.has(marker)) numbers.set(marker, numbers.size + 1);
    return null;
  });
  return numbers;
}

/** Draws a marker as its number: a link to its row under the card once the rows show
 *  (`rows`), else the number alone. `numbers` come from the same preview text, so every
 *  marker it draws has one. The link is no tab stop: the rows below are the keyboard's way
 *  to the sources, and their 44 px rows the finger's (the clamped preview would cut a
 *  touch area round the number). */
function numberedCitations(
  numbers: ReadonlyMap<string, number>,
  rows: ReadonlySet<string>,
  prefix: string,
): CitationRenderer {
  return (marker) => {
    const n = numbers.get(marker);
    if (n === undefined) return marker;
    if (!rows.has(marker)) return <sup className="text-text-muted">{n}</sup>;
    return (
      <sup>
        {/* A link to the row in this card, not a button: a button takes a 44 px hit area
            under a finger, which would widen the line. */}
        <Link
          href={`#${prefix}-${n}`}
          size="sm"
          tabIndex={-1}
          aria-label={`Source ${n}`}
          onClick={(e) => {
            e.preventDefault();
            const row = document.getElementById(`${prefix}-${n}`);
            row?.scrollIntoView?.({ block: "nearest" });
            row?.querySelector<HTMLElement>("button, [tabindex]")?.focus();
          }}
        >
          {n}
        </Link>
      </sup>
    );
  };
}

/** The answer's text as one run of inline Markdown, for a preview clamped to a few lines:
 *  code and tables are left out. */
function preview(answer: string): string {
  return parseBlocks(answer)
    .flatMap((b) => (b.kind === "paragraph" ? [b.text] : b.kind === "list" ? b.items : []))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * The card under its "Ask AI: <question>" option once asked: the answer's first lines as
 * they stream, the working line, then its label and sources as the panel shows them, and
 * Continue in the assistant, which opens the panel on the conversation. `turnId`: the turn
 * asked, null until it shows; `busy`: nothing was asked, another answer is still coming in.
 * While it shows, the answer's end is seen here: it is said as complete through the
 * assistant's live region, never marked unread.
 */
export function SearchAnswer({
  turnId,
  busy,
  onStarted,
  onGone,
  onContinue,
}: {
  turnId: string | null;
  busy: boolean;
  /** Told once the answer has started (text, or an end) or a refusal's reason shows, when
   *  Enter continues. */
  onStarted: () => void;
  /** Told when nothing answers the card's question any more and no turn of it shows: the
   *  turn left the conversation shown (a new one, another from History), or it was never
   *  asked (another answer was coming in, or another tab held the conversation). The card
   *  goes back to asking. */
  onGone: () => void;
  onContinue: () => void;
}) {
  const turns = useChat((s) => s.turns);
  const answering = useChat((s) => s.answering);
  const checking = useChat((s) => s.checkingSources);
  const hint = useChat((s) => s.hint);
  const notice = useChat((s) => s.notice);
  // The card's source rows, which its numbered markers link to.
  const rowPrefix = useId();
  const at = turnId ? turns.findIndex((t) => t.id === turnId) : -1;
  const turn = at >= 0 ? turns[at] : null;
  const started = !!turn && (!!turn.answer || turn.state !== "answering");

  // Only the card's own turn is seen here: an answer the panel streams when the card found
  // it busy ends unread, with its dot.
  const own = turn?.id;
  useEffect(() => {
    if (!own) return;
    useDock.setState({ answerShown: own });
    return () => {
      if (useDock.getState().answerShown === own) useDock.setState({ answerShown: undefined });
    };
  }, [own]);
  // A question not asked for a reason the card says (the route refused it, or another tab
  // answers in the conversation) keeps the card; with none, nothing answering and no turn,
  // the card asks again.
  const refused = (!!hint || notice === OTHER_TAB) && !answering;
  const gone = !turn && !answering && !refused;
  useEffect(() => {
    if (gone) onGone();
  }, [gone, onGone]);
  // Enter continues once the answer has started or the question ended unasked with a reason
  // (the panel says it at more length).
  const ended = started || (!turn && refused);
  useEffect(() => {
    if (ended) onStarted();
  }, [ended, onStarted]);

  let body: ReactNode;
  // Busy only while that other answer runs: once it ends, what the card says is the !turn
  // case's (a reason, or back to asking).
  if (busy && answering) body = <p className="text-sm text-text-muted">An answer is still coming in.</p>;
  else if (!turn) {
    // Not asked after all, once nothing answers: the route refused before the answer, or
    // another tab answers in the conversation. The panel says why at more length.
    const why = hint ? `The assistant can't answer: ${SHORT_REASON[hint.kind]}.` : notice;
    body = <p className="text-sm text-text-muted">{!answering && why ? why : "Answering…"}</p>;
  } else {
    const text = preview(turn.answer);
    const numbers = numbering(text);
    // The label and rows show once the answer is done and its check has ended (a server a
    // release behind sends its citations before the end, with no text_done); the numbers
    // link to rows only then. A citation's text matches its marker with its whitespace
    // collapsed.
    const listed = turn.state === "done" && checking !== turn.id;
    const rows = new Set(listed ? (turn.citations ?? []).map((c) => citationKey(c.text)) : []);
    const step = latestStep(turn);
    body = (
      <>
        {text && (
          <p className="line-clamp-4 text-sm text-text break-words" data-testid="search-answer-text">
            {inline(text, undefined, numberedCitations(numbers, rows, rowPrefix))}
          </p>
        )}
        {/* A finished answer that is only a table or code: the preview has no words for it. */}
        {!text && turn.state === "done" && turn.answer.trim() && (
          <p className="text-sm text-text-muted">
            The answer holds a table or code: continue in the assistant to read it.
          </p>
        )}
        {/* The working line: the latest step while the answer reads, never blank. */}
        {turn.state === "answering" && (!text || step) && (
          <p className={text ? "text-xs text-text-muted" : "text-sm text-text-muted"}>{step ?? "Answering…"}</p>
        )}
        {turn.state === "done" && checking === turn.id && <p className="text-xs text-text-muted">Checking sources…</p>}
        {listed && (
          <Sources
            citations={turn.citations ?? []}
            support={turn.support}
            checkLost={turn.checkLost}
            unknownNames={turn.unknownNames ?? []}
            answer={at + 1}
            numbers={numbers}
            idPrefix={rowPrefix}
          />
        )}
        {turn.state === "done" && (turn.steps?.length ?? 0) > 0 && (
          <p className="text-xs text-text-muted">{`What it read (${turn.steps?.length}${turn.stepsCut ? "+" : ""})`}</p>
        )}
        {turn.state === "stopped" && <p className="text-xs text-text-muted">Stopped.</p>}
        {turn.error && <Alert role="alert" state="danger" size="small" text={turn.error} />}
      </>
    );
  }
  return (
    <div className="space-y-1.5 pb-2" data-testid="search-answer" aria-busy={turn?.state === "answering" || undefined}>
      {body}
      <div className="flex items-center gap-2 pt-1">
        <Button type="secondary" htmlType="button" iconRight="arrow-right" onClick={onContinue}>
          Continue in the assistant
        </Button>
        <span className="hidden sm:inline-flex items-center gap-1 text-xs text-text-muted" aria-hidden="true">
          {isApplePlatform() ? (
            <Kbd>⌘Enter</Kbd>
          ) : (
            <>
              <Kbd>Ctrl</Kbd>+<Kbd>Enter</Kbd>
            </>
          )}
        </span>
      </div>
    </div>
  );
}
