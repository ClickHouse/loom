import { Suspense, lazy, useEffect, useRef, type KeyboardEvent, type PointerEvent } from "react";
import { FocusScope } from "@radix-ui/react-focus-scope";
import { IconButton } from "../components/IconButton";
import { SkeletonCard } from "../components/Skeleton";
import { LoadingSkeleton, PageHeader } from "../components/ui";
import { SectionHeading } from "../components/ui/SectionHeading";
import { useDock } from "../lib/assistantDock";
import { PANEL_MIN, SPLIT_STEP, isModal, layoutOf } from "../lib/assistantLayout";
import { closeAssistant, collapseAssistant, expandAssistant, setPanelWidth } from "../lib/assistantDockRuntime";

// The chat, its stored conversations and the readiness read: a chunk of their own, loaded
// the first time the panel opens.
const DockBody = lazy(() => import("../views/assistant/Dock").then((m) => ({ default: m.DockBody })));

const prevent = (e: Event) => e.preventDefault();

/**
 * The assistant panel: one element in every mode (lib/assistantDock.ts). Modal at overlay
 * and phone widths (role="dialog", aria-modal, the focus trapped and Tab wrapping, the page
 * inert); beside the page in push mode, where Tab flows in and out. The FocusScope never
 * moves the focus itself: the shell places it (the question field on an open the person
 * starts, the launcher on a close they start, the arrival's target after a navigation).
 * Esc closes the panel from inside it, unless a layer above it (a popover, a menu) took it.
 */
export function AssistantPanel() {
  const mounted = useDock((s) => s.mounted);
  const open = useDock((s) => s.open);
  const modal = useDock(isModal);
  const mode = useDock((s) => layoutOf(s).mode);
  const width = useDock((s) => layoutOf(s).width);
  const max = useDock((s) => layoutOf(s).max);
  const dragging = useRef(false);
  const element = useRef<HTMLDivElement | null>(null);
  // Esc from anywhere inside closes the panel, as a dialog's does; a layer above it (a
  // popover, a menu) that took the key has prevented it first.
  useEffect(() => {
    const el = element.current;
    if (!el) return;
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      // On /assistant the panel is the page: Esc closes nothing.
      if (e.key !== "Escape" || e.defaultPrevented || useDock.getState().expanded) return;
      if (e.target instanceof Element && e.target.closest("[data-radix-popper-content-wrapper]")) return;
      e.preventDefault();
      closeAssistant("person");
    };
    el.addEventListener("keydown", onKeyDown);
    return () => el.removeEventListener("keydown", onKeyDown);
  }, [mounted]);
  if (!mounted) return null;
  const expanded = mode === "expanded";
  const shown = open || expanded;

  // The panel is at the right edge: left makes it wider.
  const resizeBy = (e: KeyboardEvent<HTMLDivElement>) => {
    const to: Record<string, number> = {
      ArrowLeft: width + SPLIT_STEP,
      ArrowRight: width - SPLIT_STEP,
      Home: PANEL_MIN,
      End: max,
    };
    if (!(e.key in to)) return;
    e.preventDefault();
    setPanelWidth(Math.min(max, Math.max(PANEL_MIN, to[e.key])));
  };
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    setPanelWidth(Math.min(max, Math.max(PANEL_MIN, window.innerWidth - e.clientX)));
  };
  const onPointerUp = () => {
    dragging.current = false;
  };

  return (
    <>
      {open && mode === "overlay" && !!modal && (
        // A pointer on the dimmed page closes the panel, as a click outside closes a dialog.
        <div className="assistant-scrim" aria-hidden="true" onClick={() => closeAssistant("person")} />
      )}
      <FocusScope asChild trapped={modal} loop={modal} onMountAutoFocus={prevent} onUnmountAutoFocus={prevent}>
        {/* A region beside the page, a dialog over it. */}
        <div
          ref={element}
          id="assistant-panel"
          data-assistant-panel=""
          data-mode={mode}
          data-open={shown ? "" : undefined}
          hidden={!shown}
          {...(modal
            ? { role: "dialog", "aria-modal": "true", "aria-labelledby": "assistant-panel-title" }
            : expanded
              ? { role: "region", "aria-label": "Assistant" }
              : { role: "region", "aria-labelledby": "assistant-panel-title" })}
          tabIndex={-1}
          className="assistant-panel"
          style={mode === "sheet" || expanded ? undefined : { width }}
        >
          {mode !== "sheet" && !expanded && (
            // The WAI-ARIA window splitter: a focusable separator with its value range.
            // biome-ignore lint/a11y/useSemanticElements: an <hr> takes no focus, keys or value range
            <div
              role="separator"
              tabIndex={0}
              aria-orientation="vertical"
              aria-label="Resize the assistant panel"
              aria-controls="assistant-panel"
              aria-valuemin={PANEL_MIN}
              aria-valuemax={max}
              aria-valuenow={width}
              className="assistant-splitter"
              onKeyDown={resizeBy}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerUp}
            />
          )}
          {/* SectionHeading keeps 6 px under itself: the bottom padding makes up the rest. */}
          <div className="px-4 pt-2 pb-0.5 border-b border-border shrink-0">
            {expanded ? (
              // The page's title row, as every page's: it also titles the document.
              <div className="pt-1">
                <PageHeader
                  title="Assistant"
                  actions={<IconButton icon="popin" label="Collapse the assistant" onClick={collapseAssistant} />}
                />
              </div>
            ) : (
              <SectionHeading
                label="Assistant"
                level={2}
                id="assistant-panel-title"
                actions={
                  <>
                    {mode !== "sheet" && (
                      <IconButton icon="popout" label="Expand the assistant" onClick={expandAssistant} />
                    )}
                    <IconButton icon="cross" label="Close the assistant" onClick={() => closeAssistant("person")} />
                  </>
                }
              />
            )}
          </div>
          <Suspense
            fallback={
              <LoadingSkeleton label="Loading the assistant" className="p-4">
                <SkeletonCard />
              </LoadingSkeleton>
            }
          >
            <DockBody />
          </Suspense>
        </div>
      </FocusScope>
    </>
  );
}
