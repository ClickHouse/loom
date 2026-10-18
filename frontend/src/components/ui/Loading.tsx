import { Icon } from "@clickhouse/click-ui/Icon";
import { useEffect, useState, type ReactNode } from "react";

/**
 * Loading states, in Click UI's vocabulary: its `loading-animated` icon and a short label
 * beside it, the row its Table shows while `loading` ("Loading data"). Which one to use:
 *
 * - a list or grid whose rows have a known shape: `LoadingSkeleton` around `Skeleton`s
 *   shaped like those rows, so nothing moves when the data lands;
 * - any other first load (a panel, a dialog body, a page section): `Loading`;
 * - a Click UI `Table`: `TableSkeleton` (components/Skeleton.tsx) as its `noDataMessage`,
 *   which keeps the header and waits the same 300 ms;
 * - a button that started the work: Click UI's `Button loading`;
 * - a refresh of data already on screen: keep the data, mark its container `aria-busy`.
 *
 * Neither shows anything for the first 300 ms, so a fast answer never flashes a loader.
 */

const DELAY_MS = 300;

/** True once `ms` have passed since mount. */
function useAfter(ms: number): boolean {
  const [passed, setPassed] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setPassed(true), ms);
    return () => window.clearTimeout(timer);
  }, [ms]);
  return passed;
}

/** Click UI's loader icon. Decorative: the label beside it says what is loading. */
function Spinner({ size = "sm" }: { size?: "xs" | "sm" | "md" }) {
  return <Icon name="loading-animated" size={size} aria-hidden="true" />;
}

export interface LoadingProps {
  /** What is loading, as a sentence fragment: "Loading memories". */
  label?: string;
  /** `block` centres it in the region it fills; `inline` sits at the start of a line. */
  layout?: "block" | "inline";
  className?: string;
}

export function Loading({ label = "Loading", layout = "block", className = "" }: LoadingProps) {
  const shown = useAfter(DELAY_MS);
  const place = layout === "block" ? "justify-center py-8" : "";
  // The status region exists from the start, so the label is announced when it appears.
  return (
    // Click UI's Table loader: a small spinner and 12 px text in the default colour.
    <div role="status" className={`flex items-center gap-2 text-xs ${place} ${className}`}>
      {shown && (
        <>
          <Spinner />
          <span>{label}</span>
        </>
      )}
    </div>
  );
}

export interface LoadingSkeletonProps {
  label: string;
  /** `Skeleton` shapes laid out like the content they stand in for. */
  children: ReactNode;
  className?: string;
}

export function LoadingSkeleton({ label, children, className = "" }: LoadingSkeletonProps) {
  const shown = useAfter(DELAY_MS);
  return (
    <div role="status" aria-label={label} aria-busy="true" className={className}>
      <div aria-hidden="true" className={`transition-opacity duration-150 ${shown ? "" : "opacity-0"}`}>
        {children}
      </div>
    </div>
  );
}
