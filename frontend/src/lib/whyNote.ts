// The Memories list says why each search hit ranked where it did: every non-zero score
// component by magnitude, signed, in plain words. The base scores (`rank_score`,
// `rank_score_raw`, a reranked row's `rank_score_pre_rerank`) and a rerank's trail of
// positions (`rerank_*`) are not reasons, so they are skipped; the places a reranker moved
// the row (`rerank_delta`) are, and show as `reranked`.

export interface WhyTerm {
  /** Plain-language label for the ranking term. */
  label: string;
  /** Signed contribution; positive lifted the hit, negative pushed it down. */
  value: number;
  /** The value counts places (a reranker's move), not a score. */
  places?: boolean;
}

// Server component name -> label on this page. A name not listed here is
// shown under its own spelling so a new term is never hidden.
const LABELS: Record<string, string> = {
  key_match_boost_effective: "key match",
  key_match: "key match",
  identifier_match: "identifier",
  date_proximity_boost: "date proximity",
  entity_ann_boost_effective: "entity",
  entity_ann_boost: "entity",
  set_expansion: "set expansion",
  foreign: "other namespace",
  semantic: "semantic",
  lexical: "lexical",
  reinforcement: "reinforcement",
  recency: "recency",
  trust: "provenance",
};

const SKIP = new Set(["rank_score_raw", "rank_score", "rank_score_pre_rerank"]);
const MIN_ABS = 0.005;

/** Non-zero score components ranked by magnitude, labelled for humans. */
export function whyTerms(components: Record<string, unknown> | null | undefined): WhyTerm[] {
  if (!components || typeof components !== "object") return [];
  const out: WhyTerm[] = [];
  let moved: WhyTerm | null = null;
  let stale: WhyTerm | null = null;
  for (const [name, raw] of Object.entries(components)) {
    if (name === "rerank_delta" && typeof raw === "number" && raw !== 0) {
      moved = { label: "reranked", value: raw, places: true };
      continue;
    }
    if (name === "code_changed_places" && typeof raw === "number" && raw !== 0) {
      stale = { label: "code changed", value: raw, places: true };
      continue;
    }
    if (SKIP.has(name) || name.startsWith("rerank_")) continue;
    if (typeof raw !== "number" || !Number.isFinite(raw)) continue;
    if (Math.abs(raw) < MIN_ABS) continue;
    out.push({ label: LABELS[name] ?? name.replace(/_/g, " "), value: raw });
  }
  out.sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  // A move counts places, not score: the reranker's, then a stale memory's, lead, as
  // in the MCP why note.
  return [moved, stale, ...out].filter((t): t is WhyTerm => t !== null);
}

/** A term as shown: a score lift as `+0.12`, a reranker's move in places as `+3`. */
export function termText(t: WhyTerm): string {
  return t.places ? `${t.value >= 0 ? "+" : "-"}${Math.abs(Math.round(t.value))}` : signed(t.value);
}

/** Signed two-decimal rendering: `+0.12`, `-0.05`. */
export function signed(v: number): string {
  return `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(2)}`;
}
