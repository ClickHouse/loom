import type { ReactNode } from "react";

/**
 * Kbd — a keyboard hint: one key or a short chord in a mono cap at the 11 px floor, on
 * the page background with the border token, so it reads as a key and never as body text.
 */
export function Kbd({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={`inline-flex items-center justify-center min-w-[1.6em] h-5 px-1 rounded border border-border bg-bg font-mono text-2xs text-text-subtle leading-none ${className}`}
    >
      {children}
    </kbd>
  );
}
