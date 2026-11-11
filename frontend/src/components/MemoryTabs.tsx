import type { MouseEvent } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { navigationHref, useStore } from "../lib/state";

/**
 * Memories and Recall are one section with two tabs, not two rail destinations. Recall is
 * the "why did this rank here" tool over the same namespace's memories — a mode of reading
 * them, so it sits under the Memories heading. Each page renders this strip under its
 * title; both are routes (`/memories`, `/recall`), so deep links, Back and the route budget
 * hold. What the namespace's usage says as a whole is the assistant's starters (the
 * Memories header's Insights button).
 */
export type MemoryTab = "memories" | "recall";

const MEMORY_TABS: Array<{ id: MemoryTab; label: string; icon: IconName }> = [
  { id: "memories", label: "Memories", icon: "clock" },
  { id: "recall", label: "Recall", icon: "flask" },
];

const modified = (e: MouseEvent) => e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;

function tabHref(tab: MemoryTab): string {
  return navigationHref(tab === "recall" ? "recall" : "memories", {});
}

function go(tab: MemoryTab) {
  const { navigate, setView } = useStore.getState();
  // Recall carries no query of its own.
  if (tab === "memories") navigate("memories", { query: {} });
  else setView(tab);
}

export function MemoryTabs({ active }: { active: MemoryTab }) {
  // Each tab is a link to its page, so a modified click still opens it in a new tab. The
  // panels are the pages themselves, so the triggers control no panel here.
  return (
    <Tabs
      value={active}
      onValueChange={(v) => {
        if (v !== active) go(v as MemoryTab);
      }}
      activationMode="manual"
      className="mb-4"
    >
      <Tabs.TriggersList aria-label="Memory section">
        {MEMORY_TABS.map((t) => (
          <Tabs.Trigger key={t.id} value={t.id} asChild>
            <a
              href={tabHref(t.id)}
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
