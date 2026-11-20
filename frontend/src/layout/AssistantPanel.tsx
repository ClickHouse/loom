import { Suspense, lazy, useEffect, useRef, type KeyboardEvent, type PointerEvent } from "react";
import { FocusScope } from "@radix-ui/react-focus-scope";
import { SkeletonCard } from "../components/Skeleton";
import { LoadingSkeleton } from "../components/ui";
import { useDock } from "../lib/assistantDock";
import { PANEL_MIN, SPLIT_STEP, dockLayout, isModal, layoutOf } from "../lib/assistantLayout";
import { closeAssistant, collapseAssistant, setPanelWidth } from "../lib/assistantDockRuntime";
import { AssistantHeader } from "./AssistantHeader";

// The chat, its stored conversations and the readiness read: a chunk of their own, loaded
// the first time the panel opens.
const DockBody = lazy(() => import("../views/assistant/Dock").then((m) => ({ default: m.DockBody })));

const prevent = (e: Event) => e.preventDefault();

/**
 * The assistant panel: one element in every mode (lib/assistantDock.ts). Modal over the page
 * (the overlay, the phone sheet and the expanded window: role="dialog", aria-modal, the
 * focus trapped and Tab wrapping, the page inert); beside the page in push mode, where Tab
 * flows in and out. The FocusScope never moves the focus itself: the shell places it (the
 * question field on an open the person starts, the launcher on a close they start, the
 * arrival's target after a navigation). Esc from inside closes the panel, or collapses the
 * window, unless a layer above it (a popover, a menu) took it.
 */
export function AssistantPanel() {
  const mounted = useDock((s) => s.mounted);
  const open = useDock((s) => s.open);
  const modal = useDock(isModal);
  const mode = useDock((s) => layoutOf(s).mode);
  const width = useDock((s) => layoutOf(s).width);
  const max = useDock((s) => layoutOf(s).max);
  const entering = useDock((s) => !!s.entering);
  // Under the window, the docked panel's place while it was beside the page and still would
  // be: <main> keeps its width.
  const spacer = useDock((s) => {
    const docked = dockLayout(s.viewport, s.collapsed, s.prefWidth);
    return s.expandedFromPush && docked.mode === "push" ? docked.width : 0;
  });
  const dragging = useRef(false);
  const element = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = element.current;
    if (!el) return;
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target instanceof Element && e.target.closest("[data-radix-popper-content-wrapper]")) return;
      e.preventDefault();
      if (useDock.getState().expanded) collapseAssistant();
      else closeAssistant("person");
    };
    // The slide-in has played (the panel's own, not a child's animation).
    const onAnimationEnd = (e: Event) => {
      if (e.target === el) useDock.setState({ entering: false });
    };
    el.addEventListener("keydown", onKeyDown);
    el.addEventListener("animationend", onAnimationEnd);
    el.addEventListener("animationcancel", onAnimationEnd);
    return () => {
      el.removeEventListener("keydown", onKeyDown);
      el.removeEventListener("animationend", onAnimationEnd);
      el.removeEventListener("animationcancel", onAnimationEnd);
    };
  }, [mounted]);
  if (!mounted) return null;
  const expanded = mode === "expanded";

  // The panel is at the right: left makes it wider.
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
    if (!dragging.current || !element.current) return;
    // Measured from the panel's own right edge, wherever the panel sits.
    const right = element.current.getBoundingClientRect().right;
    setPanelWidth(Math.min(max, Math.max(PANEL_MIN, right - e.clientX)));
  };
  const onPointerUp = () => {
    dragging.current = false;
  };

  return (
    <>
      {spacer > 0 && <div aria-hidden="true" data-assistant-spacer="" className="shrink-0" style={{ width: spacer }} />}
      {open && modal && (mode === "overlay" || expanded) && (
        // A pointer on the dimmed page closes the overlay and collapses the window (one step
        // back), as a click outside closes a dialog.
        <div
          className="assistant-scrim animate-fade-in"
          aria-hidden="true"
          onClick={() => (expanded ? collapseAssistant() : closeAssistant("person"))}
        />
      )}
      <FocusScope asChild trapped={modal} loop={modal} onMountAutoFocus={prevent} onUnmountAutoFocus={prevent}>
        {/* A region beside the page, a dialog over it. */}
        <div
          ref={element}
          id="assistant-panel"
          data-assistant-panel=""
          data-mode={mode}
          data-open={open ? "" : undefined}
          data-entering={entering ? "" : undefined}
          hidden={!open}
          {...(modal
            ? { role: "dialog", "aria-modal": "true", "aria-label": "Ask AI" }
            : { role: "region", "aria-label": "Ask AI" })}
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
              aria-label="Resize Ask AI"
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
          <AssistantHeader expanded={expanded} />
          <Suspense
            fallback={
              <LoadingSkeleton label="Loading Ask AI" className="p-4">
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
