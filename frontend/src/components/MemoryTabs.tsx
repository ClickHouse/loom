import { Clock, FlaskConical } from "lucide-react";
import { useStore, viewHref, type View } from "../lib/state";

/**
 * Memories and Recall are one section with two tabs, not two rail destinations. Recall is
 * the "why did this rank here" tool over the same namespace's memories — a mode of
 * reading them, so it sits under the Memories heading. Both pages render this strip under
 * their title; the routes (`/me/memories`, `/me/recall`) are unchanged, so deep links and
 * the route budget still hold.
 */
const MEMORY_TABS: Array<{ id: View; label: string; icon: typeof Clock }> = [
  { id: "memories", label: "Memories", icon: Clock },
  { id: "recall", label: "Recall", icon: FlaskConical },
];

export function MemoryTabs({ active }: { active: View }) {
  const setView = useStore((s) => s.setView);
  return (
    <nav aria-label="Memory section" className="mb-5 flex items-center gap-1 border-b border-[var(--color-border)]">
      {MEMORY_TABS.map((t) => {
        const Icon = t.icon;
        const on = t.id === active;
        return (
          <a
            key={t.id}
            href={viewHref(t.id)}
            aria-current={on ? "page" : undefined}
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
              e.preventDefault();
              if (!on) setView(t.id);
            }}
            className={`inline-flex items-center gap-1.5 px-3 py-2 -mb-px text-sm border-b-2 no-underline transition-colors ${
              on
                ? "border-[var(--color-accent)] text-[var(--color-text)] font-medium"
                : "border-transparent text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
            }`}
          >
            <Icon size={13} aria-hidden="true" /> {t.label}
          </a>
        );
      })}
    </nav>
  );
}
