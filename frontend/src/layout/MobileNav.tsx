import { Suspense, lazy, useState } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { useStore, viewHref } from "../lib/state";
import { openAssistantSoon } from "../lib/assistantDock";
import { useMedia } from "../lib/useMedia";
import { EDGE_QUERY, useLauncher } from "./AssistantLauncher";
import { MAIN, type NavItem } from "./Sidebar";
import { ACTIVE, PRIMARY_IDS } from "./mobileTabs";

// The "More" sheet opens on a click: a chunk of its own.
const MoreSheet = lazy(() => import("./MobileMore"));

/**
 * Phone navigation (below `md`): two primary tabs, the assistant's launcher and a "More"
 * dialog (./MobileMore.tsx; Click UI's dialog fills the screen below 640 px).
 */
const PRIMARY: NavItem[] = MAIN.filter((m) => PRIMARY_IDS.includes(m.id));

// One tab of the bar: its icon over its word.
const TAB = "relative flex flex-col items-center justify-center gap-0.5 flex-1 h-full";

export function MobileNav() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const [open, setOpen] = useState(false);
  const moreActive = !PRIMARY_IDS.includes(view);
  // From md the assistant edge is the launcher (layout/AssistantLauncher.tsx).
  const wide = useMedia(EDGE_QUERY);

  return (
    <>
      <nav
        aria-label="Sections"
        className="md:hidden h-14 border-t border-border bg-surface flex items-center justify-around shrink-0"
      >
        {PRIMARY.map((p) => {
          const active = view === p.id;
          return (
            <a
              key={p.id}
              href={viewHref(p.id)}
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                setView(p.id);
              }}
              className={`${TAB} transition-colors no-underline hover:no-underline ${
                active ? `${ACTIVE} text-text` : "text-text-muted"
              }`}
              aria-current={active ? "page" : undefined}
            >
              {/* The current tab carries the selected fill and a bar, not only a text colour. */}
              {active && <span aria-hidden="true" className="absolute inset-x-3 top-0 h-0.5 rounded-b bg-text" />}
              <Icon name={p.icon} size="md" aria-hidden="true" />
              <span className="text-xs font-medium">{p.label}</span>
            </a>
          );
        })}
        {!wide && <AssistantItem />}
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          aria-expanded={open}
          className={`${TAB} ${moreActive ? `${ACTIVE} text-text` : "text-text-muted"}`}
        >
          {moreActive && <span aria-hidden="true" className="absolute inset-x-3 top-0 h-0.5 rounded-b bg-text" />}
          <Icon name="dots-horizontal" size="md" aria-hidden="true" />
          <span className="text-xs font-medium">More</span>
        </button>
      </nav>

      {open && (
        <Suspense fallback={null}>
          <MoreSheet onClose={() => setOpen(false)} />
        </Suspense>
      )}
    </>
  );
}

/** The bottom nav's Assistant item: it opens the panel, which is modal below md (so the nav
 *  is inert under it). Not a page: never the current tab's fill or bar. */
function AssistantItem() {
  const { ref, mark, props } = useLauncher();
  return (
    <button ref={ref} {...props} onClick={openAssistantSoon} className={`${TAB} text-text-muted`}>
      {mark}
      <span className="text-xs font-medium">Assistant</span>
    </button>
  );
}
