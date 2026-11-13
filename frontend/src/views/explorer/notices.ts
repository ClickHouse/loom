import type { TaxonomyResponse } from "../../api";
import { parseChTime, relativeTime } from "../../lib/format";
import { NODE_CAP, shorten } from "./model";

/**
 * The Explorer's result copy: pure functions from a result to the words shown for it, so
 * the toast, the inspector line and the status line say the same thing and a test can pin
 * the words without a DOM.
 *
 * An expand that changed nothing is still a result. `expandNotice` turns an
 * `expandModel` outcome into the one-line notice under the selected node
 * ("Expanded: 0 new within one hop · 3 already shown · strongest 200 kept",
 * each part only when it applies) and the shorter toast that names the node
 * ("Expanded: +7 under logical").
 *
 * `communitiesCopy` reads `namespace.taxonomy` for the Facets Colour /
 * Communities section: how many communities are shown and when they were
 * computed, or — when none are — what computes them and when, in a sentence that is never clamped.
 */

export interface ExpandOutcome {
  entity: string;
  /** Entities appended under the node. */
  added: number;
  /** Neighbours the server returned that the tree already held. */
  known: number;
  /** The node cap cut the new children short (or left no room). */
  capped: boolean;
  cap?: number;
}

export interface ExpandNotice {
  entity: string;
  /** The inspector line, under the node it is about. */
  line: string;
  /** The toast: names the node, one clause per fact. */
  toast: string;
  tone: "ok" | "error";
}

export function expandNotice(o: ExpandOutcome): ExpandNotice {
  const cap = o.cap ?? NODE_CAP;
  const parts: string[] = [];
  if (o.known > 0) parts.push(`${o.known} already shown`);
  if (o.capped) parts.push(`strongest ${cap} kept`);
  const tail = parts.length ? ` · ${parts.join(" · ")}` : "";
  const head = o.added > 0 ? `+${o.added}` : "0 new within one hop";
  const name = shorten(o.entity, 24);
  return {
    entity: o.entity,
    line: `Expanded: ${head}${tail}`,
    toast:
      o.added > 0
        ? `Expanded: +${o.added} under ${name}${tail}`
        : o.capped
          ? `No room under ${name} · strongest ${cap} kept`
          : `Nothing new under ${name} within one hop${o.known > 0 ? ` · ${o.known} already shown` : ""}`,
    tone: "ok",
  };
}

export function expandFailure(entity: string, message: string): ExpandNotice {
  return {
    entity,
    line: `Expand failed: ${message}`,
    toast: `Expand of ${shorten(entity, 24)} failed: ${message}`,
    tone: "error",
  };
}

/** How many communities `namespace.taxonomy` is asked for; at that count the
 *  list is the largest N, not all of them, and the copy says so. */
export const COMMUNITIES_SHOWN = 12;

export const NO_COMMUNITIES_COPY =
  "No communities yet. When an operator of this deployment has turned them on, they are computed every 6 hours for namespaces explored in the last 14 days; exploring this one counts.";

export type CommunitiesCopy =
  | { kind: "none"; line: string }
  | { kind: "some"; line: string; computed: string | null; absolute: string | null };

/** An absolute UTC stamp for a tooltip: "2026-09-16 08:33 UTC". */
export function absoluteUtc(iso: string): string | null {
  const d = parseChTime(iso);
  if (!d) return null;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function communitiesCopy(
  t: Pick<TaxonomyResponse, "communities" | "communities_computed_at"> | null,
): CommunitiesCopy {
  const n = t?.communities.length ?? 0;
  if (n === 0) return { kind: "none", line: NO_COMMUNITIES_COPY };
  const at = t?.communities_computed_at || "";
  const rel = at ? relativeTime(at) : "";
  const count = n >= COMMUNITIES_SHOWN ? `${n} largest communities` : `${n} communit${n === 1 ? "y" : "ies"}`;
  return {
    kind: "some",
    line: rel ? `${count} · computed ${rel}` : count,
    computed: rel || null,
    absolute: at ? absoluteUtc(at) : null,
  };
}
