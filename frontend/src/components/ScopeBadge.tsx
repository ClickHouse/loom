import { Globe, Layers } from "lucide-react";
import { RoleChip } from "./RoleChip";
import { useStore } from "../lib/state";
import { isReferenceKind, kindOf } from "../lib/namespace";

/**
 * ScopeBadge — tells the user exactly what data they're looking at.
 *
 * Loom views mix scopes — most are namespace-scoped (Activity for the
 * selected namespace), one is Loom-wide (worker health), some are
 * derived from a primary key. Without an explicit indicator, users
 * see "page named Usage, namespace dropdown at the top, columns of
 * data" and reasonably assume it's filtered. When the columns happen
 * to show null/empty values (because of SQL aggregation), they
 * conclude "the page is broken" — when it's just unscoped display.
 *
 * Place this just under the page H1 in every admin view. Users learn
 * the pattern: badge says where the numbers come from, full stop.
 */

type Scope = { kind: "namespace"; value: string } | { kind: "loom-wide" };

export function ScopeBadge(props: Scope) {
  // A code index or a tracker namespace is named with its kind, the way the picker tags
  // it, so a page on `code-clickhouse` says what kind of namespace it is reading. The
  // kind comes from list_mine.
  const mine = useStore((s) => s.myNamespaces);
  const nsKind = props.kind === "namespace" ? kindOf(props.value, mine) : undefined;
  if (props.kind === "loom-wide") {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded-md">
        <Globe size={11} aria-hidden="true" /> Loom-wide
      </span>
    );
  }
  // An empty identity means no namespace was selected (no saved pick, no own namespace,
  // no usable claim). Say so instead of rendering an empty monospace slot next to
  // "namespace".
  if (!props.value) {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-[var(--color-warn)] bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded-md">
        <Layers size={11} aria-hidden="true" /> No namespace selected
      </span>
    );
  }
  // The caller's role rides beside the scope. `min-h-7` matches the chip's height so the
  // row is the same size before and after the role resolves — no layout shift when it
  // appears.
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 min-h-7">
      <span className="inline-flex items-center gap-1.5 px-2 py-1 text-xs text-[var(--color-text-muted)] bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded-md">
        <Layers size={11} aria-hidden="true" /> namespace{" "}
        <span className="font-medium text-[var(--color-text)] font-mono">{props.value}</span>
        {isReferenceKind(nsKind) && (
          <span className="rounded px-1 py-px border border-[var(--color-border)] text-2xs text-[var(--color-text-muted)]">
            {nsKind}
          </span>
        )}
      </span>
      <RoleChip namespace={props.value} />
    </span>
  );
}
