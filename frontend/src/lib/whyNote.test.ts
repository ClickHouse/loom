import { describe, expect, it } from "vitest";
import { signed, termText, whyTerms } from "./whyNote";

// Non-zero components by magnitude, signed; `rank_score` / `rank_score_raw` are the base,
// not a reason; sub-0.005 terms are noise.

describe("signed / whyTerms", () => {
  it("orders by magnitude, labels known terms, skips the base score and noise", () => {
    const terms = whyTerms({
      rank_score: 0.37,
      rank_score_raw: 0.37,
      semantic: 0.25,
      key_match_boost_effective: 0.12,
      date_proximity_boost: -0.3,
      entity_ann_boost: 0.001,
      foreign: 1,
    });
    expect(terms.map((t) => t.label)).toEqual(["other namespace", "date proximity", "semantic", "key match"]);
    expect(terms[1].value).toBe(-0.3);
  });

  it("names the reranker's move and skips the rest of its trail", () => {
    const terms = whyTerms({
      rank_score: 0.6,
      rank_score_pre_rerank: 0.37,
      rerank_from: 4,
      rerank_pos: 1,
      rerank_delta: 3,
      rerank_external: 1,
      semantic: 0.25,
    });
    expect(terms).toEqual([
      { label: "reranked", value: 3, places: true },
      { label: "semantic", value: 0.25 },
    ]);
    expect(terms.map(termText)).toEqual(["+3", "+0.25"]);
    // The move leads even when a score term is larger than it.
    const big = whyTerms({ rerank_delta: 1, rank_score_before_defer: 1.2 });
    expect(big.map((t) => t.label)).toEqual(["reranked", "rank score before defer"]);
  });

  it("names the places a memory whose code changed moved down, after a reranker's move", () => {
    const terms = whyTerms({ code_changed_places: -3, rerank_delta: 2, semantic: 0.25 });
    expect(terms.map((t) => t.label)).toEqual(["reranked", "code changed", "semantic"]);
    expect(terms.map(termText)).toEqual(["+2", "-3", "+0.25"]);
  });

  it("shows an unknown component under its own spelling rather than hiding it", () => {
    expect(whyTerms({ brand_new_term: 0.2 })).toEqual([{ label: "brand new term", value: 0.2 }]);
  });

  it("tolerates a missing or malformed map", () => {
    expect(whyTerms(undefined)).toEqual([]);
    expect(whyTerms(null)).toEqual([]);
    expect(whyTerms({ semantic: "0.5", nan: Number.NaN })).toEqual([]);
  });

  it("signed keeps two decimals with an explicit sign", () => {
    expect(signed(0.125)).toBe("+0.13");
    expect(signed(-0.004)).toBe("-0.00");
  });
});
