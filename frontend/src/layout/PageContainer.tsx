import type { ReactNode } from "react";

/**
 * Single source of truth for the top-level page wrapper. Every dashboard page renders
 * inside this so width and padding stay consistent: a 16 px gutter on a phone (Click UI's
 * small container padding), 24 px from 640 px, 32 px on a wide screen. Pass
 * `className` for view-specific extras (e.g. `space-y-3`, `pb-24`); don't
 * override the width or padding from outside — that's the drift this prevents.
 *
 * The page uses the full width of the content column (a `max-w-6xl` cap would leave a
 * third of a wide screen empty beside every table and board); prose that wants a measure
 * caps itself (`max-w-2xl` on explanatory paragraphs), everything else grows.
 */
export function PageContainer({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`w-full py-5 px-4 sm:py-8 sm:px-6 xl:px-8 ${className}`.trim()}>{children}</div>;
}
