import type { ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { RoleChip } from "./RoleChip";
import { useStore } from "../lib/state";
import { isReferenceKind, kindOf } from "../lib/namespace";

/**
 * ScopeBadge — tells the user exactly what data they're looking at.
 *
 * Loom views mix scopes — most are namespace-scoped (Audit log for the
 * selected namespace), one is Loom-wide (worker health), some are
 * derived from a primary key. Without an explicit indicator, users
 * see "page named Usage, namespace picker at the top, columns of
 * data" and reasonably assume it's filtered. When the columns happen
 * to show null/empty values (because of SQL aggregation), they
 * conclude "the page is broken" — when it's just unscoped display.
 *
 * Every page passes it to its PageHeader, beside the title. Users learn
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
    return <Badge text={<Labelled icon="globe">Loom-wide</Labelled>} />;
  }
  // An empty identity means no namespace was selected (no saved pick, no own namespace,
  // no usable claim). Say so instead of rendering an empty monospace slot next to
  // "namespace".
  if (!props.value) {
    return <Badge text={<Labelled icon="cards">No namespace selected</Labelled>} state="warning" />;
  }
  // The caller's role rides beside the scope. `min-h-6` matches the chip's height so the
  // row is the same size before and after the role resolves — no layout shift when it
  // appears.
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 min-h-6">
      <Badge
        ellipsisContent={false}
        text={
          <Labelled icon="cards">
            namespace <span className="font-mono text-text">{props.value}</span>
            {isReferenceKind(nsKind) && <> · {nsKind}</>}
          </Labelled>
        }
      />
      <RoleChip namespace={props.value} />
    </span>
  );
}

// Badge's own `icon` is announced by its name ("globe"); this one is decorative.
function Labelled({ icon, children }: { icon: IconName; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      <Icon name={icon} size="xs" aria-hidden="true" />
      <span>{children}</span>
    </span>
  );
}
