// The Memories list says why each search hit ranked where it did: every non-zero score
// component by magnitude, signed, in plain words. `rank_score` and `rank_score_raw` are
// the base, not a reason, so they are skipped.

export interface WhyTerm {
  /** Plain-language label for the ranking term. */
  label: string;
  /** Signed contribution; positive lifted the hit, negative pushed it down. */
  value: number;
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

const SKIP = new Set(["rank_score_raw", "rank_score"]);
const MIN_ABS = 0.005;

/** Non-zero score components ranked by magnitude, labelled for humans. */
export function whyTerms(components: Record<string, unknown> | null | undefined): WhyTerm[] {
  if (!components || typeof components !== "object") return [];
  const out: WhyTerm[] = [];
  for (const [name, raw] of Object.entries(components)) {
    if (SKIP.has(name) || typeof raw !== "number" || !Number.isFinite(raw)) continue;
    if (Math.abs(raw) < MIN_ABS) continue;
    out.push({ label: LABELS[name] ?? name.replace(/_/g, " "), value: raw });
  }
  out.sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  return out;
}

/** Signed two-decimal rendering: `+0.12`, `-0.05`. */
export function signed(v: number): string {
  return `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(2)}`;
}

/** A search hit's similarity: the server's own figure, else 1 − distance. */
export function hitSimilarity(hit: { similarity?: number; distance: number }): number {
  return typeof hit.similarity === "number" && Number.isFinite(hit.similarity) ? hit.similarity : 1 - hit.distance;
}
