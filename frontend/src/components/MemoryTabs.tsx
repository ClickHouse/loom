import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { navigationHref, useStore } from "../lib/state";

/**
 * Memories, Recall and Insights are one section with three tabs, not three rail
 * destinations. Recall is the "why did this rank here" tool over the same namespace's
 * memories — a mode of reading them, so it sits under the Memories heading. Insights holds
 * the usage audit and the mentor, which read the namespace as a whole and were stacked
 * above the list, so the first memory sat 440 px down the page. Each page renders this
 * strip under its title; Memories and Recall are routes (`/memories`, `/recall`), and
 * Insights is the Memories route with `?section=insights`, so deep links, Back and the
 * route budget hold.
 */
export type MemoryTab = "memories" | "recall" | "insights";

const INSIGHTS = "insights";
const SECTION_KEY = "section";

const MEMORY_TABS: Array<{ id: MemoryTab; label: string; icon: IconName }> = [
  { id: "memories", label: "Memories", icon: "clock" },
  { id: "recall", label: "Recall", icon: "flask" },
  { id: "insights", label: "Insights", icon: "light-bulb" },
];

const modified = (e: MouseEvent) => e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0;

function tabHref(tab: MemoryTab): string {
  return navigationHref(
    tab === "recall" ? "recall" : "memories",
    tab === "insights" ? { [SECTION_KEY]: INSIGHTS } : {},
  );
}

function go(tab: MemoryTab) {
  const { navigate, setView } = useStore.getState();
  if (tab === "insights") navigate("memories", { query: { [SECTION_KEY]: INSIGHTS } });
  // Memories from Insights drops `?section=`; Recall carries no query of its own.
  else if (tab === "memories") navigate("memories", { query: {} });
  else setView(tab);
}

/** The tab the Memories route shows: Insights when its URL says so. Read again after a
 *  navigation (the tabs push one) and on Back or Forward. */
export function useMemoriesSection(): "memories" | "insights" {
  const navSeq = useStore((s) => s.navSeq);
  const [popped, setPopped] = useState(0);
  useEffect(() => {
    const on = () => setPopped((n) => n + 1);
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  // Read again whenever a navigation or a Back moved the URL.
  return useMemo(
    () => (new URLSearchParams(window.location.search).get(SECTION_KEY) === INSIGHTS ? "insights" : "memories"),
    [navSeq, popped],
  );
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
