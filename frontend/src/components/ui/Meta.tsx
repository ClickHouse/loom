import type { HTMLAttributes, ReactNode } from "react";

/**
 * MetaList / MetaRow — a label/value grid for the metadata of one thing: `<dl>` with the
 * label column sized to its content and the value column taking the rest. Values wrap
 * (`break-words`), never ellipsis — "381 — co-mentions on the edge that reached it" is
 * read in full or not at all. Digits are tabular; `mono` for identifiers and dates;
 * `tone` colours a warning or danger row.
 */
export function MetaList({
  children,
  className = "",
  ...rest
}: { children: ReactNode; className?: string } & Omit<HTMLAttributes<HTMLDListElement>, "children">) {
  return (
    <dl className={`grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs ${className}`} {...rest}>
      {children}
    </dl>
  );
}

export interface MetaRowProps {
  label: ReactNode;
  children: ReactNode;
  mono?: boolean;
  tone?: "default" | "muted" | "warn" | "danger";
  /** Extra classes for the value cell. */
  className?: string;
}

const TONE = { default: "", muted: "text-text-subtle", warn: "text-warn", danger: "text-danger" } as const;

export function MetaRow({ label, children, mono = false, tone = "default", className = "" }: MetaRowProps) {
  return (
    <>
      <dt className={`whitespace-nowrap ${tone === "warn" || tone === "danger" ? TONE[tone] : "text-text-muted"}`}>
        {label}
      </dt>
      <dd className={`min-w-0 break-words tabular-nums ${mono ? "font-mono" : ""} ${TONE[tone]} ${className}`}>
        {children}
      </dd>
    </>
  );
}
