import { Button } from "@clickhouse/click-ui/Button";
import { Tip } from "../components/ui/Tooltip";
import { useMedia } from "../lib/useMedia";
import { toggleAssistantSoon, useDock } from "../lib/assistantDock";
import { assistantShortcut } from "../lib/platform";

const reducedMotion = () =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * The header's Assistant button: it opens and closes the assistant panel, and is never
 * disabled. Its marks, each a shape of its own and in its name: answering (a spinner, a
 * static ring under reduced motion), an answer ready (a dot), unavailable (a crossed circle;
 * a click opens the panel to the reason). One button at every width: its icon alone below
 * xl, where the header's search needs the room (32 px, 44 under a coarse pointer), its
 * tooltip naming it with its shortcut; from xl the icon and the word.
 */
export function AssistantLauncher() {
  const open = useDock((s) => s.open);
  const working = useDock((s) => s.working);
  const unread = useDock((s) => s.unread);
  const reason = useDock((s) => s.reason);
  const reasonShort = useDock((s) => s.reasonShort);
  const mounted = useDock((s) => s.mounted);
  const keys = assistantShortcut();
  const worded = useMedia(WORD_QUERY);
  const state = reason ? "unavailable" : working ? "working" : unread ? "unread" : "normal";
  const name =
    state === "unavailable"
      ? "Assistant, unavailable"
      : state === "working"
        ? "Assistant, answering"
        : state === "unread"
          ? "Assistant, answer ready"
          : "Assistant";
  const tip = reason ? `Assistant: ${reasonShort} (${keys})` : `Assistant (${keys})`;
  const icon =
    state === "unavailable"
      ? "line-in-circle"
      : state === "working"
        ? reducedMotion()
          ? "loading"
          : "loading-animated"
        : "sparkle";
  return (
    <span className="relative inline-flex">
      {/* The word shown from xl, the tooltip says only what it does not (the shortcut, a
          reason); below xl it names the icon too. */}
      <Tip content={worded ? (reason ? `${reasonShort} (${keys})` : keys) : tip} side="bottom">
        <Button
          // Below xl an icon like the header's bells and help, frameless; from xl a button
          // with its word. One element either way, so the focus stays across the width.
          type={worded ? "secondary" : "empty"}
          htmlType="button"
          iconLeft={icon}
          aria-label={name}
          aria-expanded={open}
          aria-controls={mounted ? "assistant-panel" : undefined}
          data-focus-key="assistant-launcher"
          data-launcher-state={state}
          className={`assistant-launcher ${state === "unavailable" ? "assistant-launcher-muted" : ""}`}
          onClick={toggleAssistantSoon}
        >
          <span className="hidden xl:inline" aria-hidden="true">
            Assistant
          </span>
        </Button>
      </Tip>
      {state === "unread" && <span className="assistant-launcher-dot" aria-hidden="true" />}
    </span>
  );
}

/** Tailwind's `xl`, from which the launcher shows its word beside its icon. */
const WORD_QUERY = "(min-width: 1280px)";
