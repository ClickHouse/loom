import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  /** Operator detail (op names, worker names, env flags) behind a closed
   *  "How it works" disclosure, so the description itself stays in plain
   * language. */
  details?: ReactNode;
}

export function EmptyState({ icon: Icon, title, description, action, details }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      <div className="mb-6 grid place-items-center w-16 h-16 rounded-2xl bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
        <Icon size={28} strokeWidth={1.5} />
      </div>
      <h2 className="text-lg font-semibold text-[var(--color-text)] tracking-tight">{title}</h2>
      {description && (
        <p className="mt-2 max-w-md text-base text-[var(--color-text-muted)] leading-relaxed">{description}</p>
      )}
      {details && (
        <details className="mt-4 max-w-md text-sm text-[var(--color-text-muted)]">
          <summary className="cursor-pointer select-none text-[var(--color-text)] hover:underline">
            How it works
          </summary>
          <div className="mt-2 text-left leading-relaxed">{details}</div>
        </details>
      )}
      {action && (
        <button
          type="button"
          onClick={action.onClick}
          className="mt-6 inline-flex items-center gap-2 rounded-lg bg-[var(--color-accent)] px-4 py-2 text-base font-medium text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)] transition-colors"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
