import { Button } from "@clickhouse/click-ui/Button";
import type { IconName } from "@clickhouse/click-ui/Icon";
import { Tip } from "../components/ui/Tooltip";
import { reducedMotion, useMedia } from "../lib/useMedia";
import { toggleAssistantSoon, useDock } from "../lib/assistantDock";

/** Tailwind's `xl`, from which the button shows its word beside its icon: narrower, the
 *  header's search and actions need the room (e2e/namespace-picker.spec.ts). */
const WORD_QUERY = "(min-width: 1280px)";

/**
 * The header's Ask AI button, right after the search field at every width: it opens the Ask
 * AI panel, and closes it while it sits beside the page (over the page the header is inert:
 * Close and Esc close it there), and is never disabled. From xl a secondary button with its
 * word; below it the icon alone, frameless like the header's other icon buttons (32 px, 44
 * under a coarse pointer). One element at every width, so the focus stays across a resize.
 * Its marks, each a shape of its own and in its name: answering (a spinner, a static ring
 * under reduced motion), an answer ready (a dot on the icon), unavailable (a crossed
 * circle; a click opens the panel to the reason). A deployment whose chat is not set up
 * keeps the plain sparkle: its starters still read. Open, it takes Click UI's pressed look
 * (`aria-expanded`).
 */
export function AssistantLauncher() {
  const open = useDock((s) => s.open);
  const mounted = useDock((s) => s.mounted);
  const reasonShort = useDock((s) => s.reasonShort);
  const state = useDock((s) =>
    s.reason && s.reason.kind !== "not_configured"
      ? "unavailable"
      : s.working
        ? "working"
        : s.unread
          ? "unread"
          : "normal",
  );
  const worded = useMedia(WORD_QUERY);
  const reason = reasonShort || null;
  return (
    <span className="relative inline-flex shrink-0">
      {/* The word names it from xl, and the tooltip says what it does, with a reason it
          can't answer; below xl the icon is all there is, and the tooltip names it. */}
      <Tip content={reason ? `Ask AI: ${reason}` : TIP} side="bottom">
        <Button
          type={worded ? "secondary" : "empty"}
          htmlType="button"
          iconLeft={state === "working" && reducedMotion() ? "loading" : MARKS[state][0]}
          aria-label={`Ask AI${MARKS[state][1]}`}
          aria-expanded={open}
          aria-controls={mounted ? "assistant-panel" : undefined}
          data-focus-key="assistant-launcher"
          data-launcher-state={state}
          className="assistant-launcher"
          onClick={toggleAssistantSoon}
        >
          {worded && <span aria-hidden="true">Ask AI</span>}
        </Button>
      </Tip>
      {state === "unread" && <span className="assistant-launcher-dot" aria-hidden="true" />}
    </span>
  );
}

/** What the button does, said by its tooltip. */
const TIP = "Ask AI about this namespace";

// Each state's icon (under reduced motion the answering spinner is a static ring) and the
// words its name adds.
const MARKS = {
  unavailable: ["line-in-circle", ", unavailable"],
  working: ["loading-animated", ", answering"],
  unread: ["sparkle", ", answer ready"],
  normal: ["sparkle", ""],
} as const satisfies Record<string, readonly [IconName, string]>;
