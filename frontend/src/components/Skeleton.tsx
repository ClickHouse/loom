import { LoadingSkeleton } from "./ui/Loading";

/** Skeleton placeholders that shimmer while data loads. */
export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} />;
}

export function SkeletonText({ lines = 3 }: { lines?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton key={i} className={`h-3 ${i === lines - 1 ? "w-2/3" : "w-full"}`} />
      ))}
    </div>
  );
}

export function SkeletonCard() {
  return (
    <div className="rounded-sm border border-border bg-surface p-4 space-y-3">
      <div className="flex items-center justify-between">
        <Skeleton className="h-3 w-32" />
        <Skeleton className="h-3 w-12" />
      </div>
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-3 w-1/2" />
    </div>
  );
}

/** A list row's placeholder: ui/ListRow's frame at the height its list's rows measure
 *  (styles.css), so the list does not move when the rows land. `row` is a two-line row (a
 *  thread, a namespace, a run, a repository, a profile section). */
export function RowSkeleton({ kind = "row" }: { kind?: "row" | "document" | "memory" }) {
  return (
    <div className="list-row list-row-skeleton" data-kind={kind}>
      <div className="w-full space-y-2">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
      </div>
    </div>
  );
}

/** A Click UI Table's first read, as its `noDataMessage`: the header row stays in place
 *  over rows of bars, shown after 300 ms like every loader (`ui/LoadingSkeleton`). Click
 *  UI's own `loading` shows its loader at once, so a fast answer would flash it. */
export function TableSkeleton({ label, rows = 5 }: { label: string; rows?: number }) {
  return (
    <LoadingSkeleton label={label} className="w-full">
      <div className="space-y-5 py-2">
        {Array.from({ length: rows }).map((_, i) => (
          <Skeleton key={i} className="h-4 w-full" />
        ))}
      </div>
    </LoadingSkeleton>
  );
}
