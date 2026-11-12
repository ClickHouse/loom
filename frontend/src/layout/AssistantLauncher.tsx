import { useLayoutEffect, useRef } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Tip } from "../components/ui/Tooltip";
import { reducedMotion, useMedia } from "../lib/useMedia";
import { toggleAssistantSoon, useDock } from "../lib/assistantDock";
import { assistantShortcut } from "../lib/platform";

/** Tailwind's `md`: from here the assistant edge is the launcher, below it the bottom nav's
 *  item. One launcher element per width. */
export const EDGE_QUERY = "(min-width: 768px)";

// The launcher that held the focus unmounted (the window crossed md): the one that mounts
// takes it.
let handOff = false;

/**
 * What both launchers show (the assistant edge and the bottom nav's item), never disabled.
 * Its marks, each a shape of its own and in its name: answering (a spinner, a static ring
 * under reduced motion), an answer ready (a dot on the icon), unavailable (a crossed
 * circle; a click opens the panel to the reason). A deployment whose chat is not set up
 * keeps the plain sparkle: its starters still read.
 */
export function useLauncher() {
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
  const ref = useRef<HTMLButtonElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (handOff) {
      handOff = false;
      el?.focus();
    }
    return () => {
      if (el && document.activeElement === el) handOff = true;
    };
  }, []);
  const mark = (
    <span className="relative inline-flex">
      <Icon name={state === "working" && reducedMotion() ? "loading" : MARKS[state][0]} size="md" aria-hidden="true" />
      {state === "unread" && <span className="assistant-launcher-dot" aria-hidden="true" />}
    </span>
  );
  return {
    ref,
    mark,
    reasonShort,
    props: {
      type: "button" as const,
      "aria-label": `Assistant${MARKS[state][1]}`,
      "aria-expanded": open,
      "aria-controls": mounted ? "assistant-panel" : undefined,
      "data-focus-key": "assistant-launcher",
      "data-launcher-state": state,
    },
  };
}

// Each state's icon (under reduced motion the answering spinner is a static ring) and the
// words its name adds.
const MARKS = {
  unavailable: ["line-in-circle", ", unavailable"],
  working: ["loading-animated", ", answering"],
  unread: ["sparkle", ", answer ready"],
  normal: ["sparkle", ""],
} as const satisfies Record<string, readonly [IconName, string]>;

/**
 * The assistant edge's button, at the window's right from md: it opens the panel and, beside
 * the page, closes it (over the page the panel covers it). Its word stands vertically under
 * the icon; the tooltip says what the word does not (the shortcut, a reason).
 */
export function AssistantEdge() {
  return useMedia(EDGE_QUERY) ? <EdgeColumn /> : null;
}

function EdgeColumn() {
  const { ref, mark, reasonShort, props } = useLauncher();
  const keys = assistantShortcut();
  return (
    <div className="assistant-edge">
      <Tip content={reasonShort ? `${reasonShort} (${keys})` : keys} side="left">
        <button ref={ref} {...props} className="assistant-edge-button" onClick={toggleAssistantSoon}>
          {mark}
          <span>Assistant</span>
        </button>
      </Tip>
    </div>
  );
}
