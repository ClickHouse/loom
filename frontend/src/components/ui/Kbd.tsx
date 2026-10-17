import type { ReactNode } from "react";

/**
 * Kbd — a key or a short chord. Click UI has no key component, so this is the one key
 * style, on its tokens: Inter 12 px/500, muted text on the muted surface, the default
 * stroke, 4 px radius, 20 px high. Shortcut hints live only in tooltips, the help dialog
 * and the header search field.
 */
export function Kbd({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={`inline-flex items-center justify-center min-w-5 h-5 px-1.5 rounded-[4px] border border-[var(--global-color-stroke-default)] bg-[var(--global-color-background-muted)] font-sans text-xs font-medium text-[var(--global-color-text-muted)] leading-none whitespace-nowrap ${className}`}
    >
      {children}
    </kbd>
  );
}
