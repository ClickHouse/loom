import type { MouseEvent } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { useStore, viewHref, type View } from "../lib/state";

/**
 * Memories and Recall are one section with two tabs, not two rail destinations. Recall is
 * the "why did this rank here" tool over the same namespace's memories — a mode of
 * reading them, so it sits under the Memories heading. Both pages render this strip under
 * their title; the routes (`/memories`, `/recall`) are unchanged, so deep links and
 * the route budget still hold.
 */
const MEMORY_TABS: Array<{ id: View; label: string; icon: IconName }> = [
  { id: "memories", label: "Memories", icon: "clock" },
  { id: "recall", label: "Recall", icon: "flask" },
];

const modified = (e: MouseEvent) => e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;

export function MemoryTabs({ active }: { active: View }) {
  const setView = useStore((s) => s.setView);
  // Each tab is a link to its route, so a modified click still opens it in a new tab. The
  // panels are the routes themselves, so the triggers control no panel here.
  return (
    <Tabs
      value={active}
      onValueChange={(v) => {
        if (v !== active) setView(v as View);
      }}
      activationMode="manual"
      className="mb-5"
    >
      <Tabs.TriggersList aria-label="Memory section">
        {MEMORY_TABS.map((t) => (
          <Tabs.Trigger key={t.id} value={t.id} asChild>
            <a
              href={viewHref(t.id)}
              aria-controls={undefined}
              aria-current={t.id === active ? "page" : undefined}
              onMouseDown={(e) => {
                if (modified(e)) e.preventDefault();
              }}
              onClick={(e) => {
                if (!modified(e)) e.preventDefault();
              }}
              className="inline-flex items-center gap-1.5 no-underline"
            >
              <Icon name={t.icon} size="xs" aria-hidden="true" />
              {t.label}
            </a>
          </Tabs.Trigger>
        ))}
      </Tabs.TriggersList>
    </Tabs>
  );
}
