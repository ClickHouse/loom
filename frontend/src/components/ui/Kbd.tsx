import type { ReactNode } from "react";

/**
 * Kbd — a key or a short chord. Click UI has no key component, so this is the one key
 * style, on its tokens (the `.kbd` rule in styles.css). Shortcut hints live only in
 * tooltips, the help dialog and the header search field.
 */
export function Kbd({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <kbd className={className ? `kbd ${className}` : "kbd"}>{children}</kbd>;
}
