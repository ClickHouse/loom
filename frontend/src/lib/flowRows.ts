// The Flow page's "Tool activity" table shows the namespace the page is scoped to, not
// every namespace the caller can read.
//
// The sharing summary is computed over every namespace the caller holds a
// grant on (that is what "what you're sharing" needs). Summed per agent under a header
// naming the selected namespace, it would credit one namespace with another's reads.
// These helpers pick the rows for one namespace, say when that namespace is outside the
// summary's scope, and sum the rest on demand.
import type { SharingAgentNamespaceRow, SharingAgentRow, SharingSummary } from "../api";

export type ToolActivityScope =
  | { kind: "namespace"; namespace: string; rows: SharingAgentRow[]; otherPlaces: string[] }
  | { kind: "all"; rows: SharingAgentNamespaceRow[]; places: string[] }
  | { kind: "outside"; namespace: string; rows: SharingAgentNamespaceRow[]; places: string[] };

/** Sum per-namespace rows into one row per agent, most active first. */
export function sumByAgent(rows: SharingAgentNamespaceRow[]): SharingAgentRow[] {
  const by = new Map<string, SharingAgentRow>();
  for (const r of rows) {
    const cur = by.get(r.agent) ?? { agent: r.agent, writes: 0, recalls: 0, cross_namespace_recalls: 0 };
    cur.writes += r.writes;
    cur.recalls += r.recalls;
    cur.cross_namespace_recalls += r.cross_namespace_recalls;
    by.set(r.agent, cur);
  }
  return [...by.values()].sort((a, b) => b.writes + b.recalls - (a.writes + a.recalls));
}

function byActivity<T extends SharingAgentRow>(rows: T[]): T[] {
  return [...rows].sort((a, b) => b.writes + b.recalls - (a.writes + a.recalls));
}

/**
 * Rows for the Tool activity table.
 * - `showAll` → every agent × namespace row, with the namespace column.
 * - the selected namespace is in the summary's scope → its rows only, plus
 *   the other places that carry activity.
 * - the selected namespace is outside the scope (no grant) → say so and fall
 *   back to the full table rather than an empty one.
 */
export function toolActivityRows(
  data: SharingSummary,
  namespace: string | undefined,
  showAll: boolean,
): ToolActivityScope {
  const per = data.agent_namespaces;
  // The places with activity (namespaces, repositories, collections), each once.
  const places = [...new Set(per.map((r) => r.namespace))];
  if (showAll || !namespace) return { kind: "all", rows: byActivity(per), places };
  const inScope = data.namespaces.some((n) => n.namespace === namespace);
  if (!inScope) return { kind: "outside", namespace, rows: byActivity(per), places };
  const mine = per.filter((r) => r.namespace === namespace);
  const otherPlaces = places.filter((n) => n !== namespace);
  return { kind: "namespace", namespace, rows: sumByAgent(mine), otherPlaces };
}
