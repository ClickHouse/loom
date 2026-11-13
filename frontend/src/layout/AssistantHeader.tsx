import { Title } from "@clickhouse/click-ui/Title";
import { IconButton } from "../components/IconButton";
import { GatedIconButton } from "../components/ui/GatedIconButton";
import { useDock } from "../lib/assistantDock";
import { EXPAND_MIN } from "../lib/assistantLayout";
import { closeAssistant, collapseAssistant, expandAssistant } from "../lib/assistantDockRuntime";
import { useStore } from "../lib/state";
import { useMedia } from "../lib/useMedia";

/**
 * The Ask AI panel's one header row (./AssistantPanel.tsx): the title and the namespace in
 * muted text, then New chat, History (pressed while History shows), Expand (from EXPAND_MIN
 * px) and Close. Once the session has ended only Close stays: the body asks to sign in
 * again and holds no chat to start or list. In History the title says so, after a Back
 * button that returns to the chat. New chat and History are said to the panel's body through the dock
 * state (views/assistant/Dock.tsx), which loads after this row.
 */
export function AssistantHeader({ expanded }: { expanded: boolean }) {
  const expired = useDock((s) => s.expired);
  // History shows only while the session holds conversations to list.
  const history = useDock((s) => !!s.history) && !expired;
  const working = useDock((s) => s.working);
  const wide = useMedia(`(min-width: ${EXPAND_MIN}px)`);
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  return (
    <div className="assistant-head px-4 py-1.5 border-b border-border shrink-0">
      <div className="flex items-center gap-1 min-w-0">
        {history && (
          <IconButton
            icon="arrow-left"
            label="Back to the chat"
            onClick={() => useDock.setState({ history: false, focusField: true })}
          />
        )}
        <Title type="h2" size="sm" id="assistant-panel-title" className="whitespace-nowrap">
          {history ? "History" : "Ask AI"}
        </Title>
        {!history && namespace && (
          <span className="ml-1 min-w-0 truncate text-xs text-text-muted" data-testid="assistant-namespace">
            {namespace}
          </span>
        )}
        <span className="ml-auto inline-flex shrink-0 items-center gap-1">
          {!expired && (
            <>
              <GatedIconButton
                icon="plus"
                label="New chat"
                reason={working ? "An answer is still coming in." : null}
                onClick={() => useDock.setState((s) => ({ newChatSeq: (s.newChatSeq ?? 0) + 1 }))}
              />
              <IconButton
                icon="clock"
                label="History"
                aria-pressed={history}
                onClick={() => useDock.setState((s) => ({ history: !s.history }))}
              />
            </>
          )}
          {/* One button whose label swaps, so the focus stays on it. */}
          {wide && !expired && (
            <span className="contents" data-expand-toggle="">
              <IconButton
                icon={expanded ? "popin" : "popout"}
                label={expanded ? "Collapse" : "Expand"}
                onClick={expanded ? collapseAssistant : expandAssistant}
              />
            </span>
          )}
          <IconButton icon="cross" label="Close" onClick={() => closeAssistant("person")} />
        </span>
      </div>
    </div>
  );
}
