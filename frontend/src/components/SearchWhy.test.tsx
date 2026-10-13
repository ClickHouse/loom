import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { SearchHit } from "../api";
import { SearchWhy } from "./SearchWhy";

// One line under a ranked hit — rank, similarity, blended score and the signed ranking
// terms — so the Memories list says why a row is there without leaving for Recall Lab.

const hit = (over: Partial<SearchHit> = {}): SearchHit => ({
  memory_key: "k1",
  memory_type: "semantic",
  content_excerpt: "the user prefers oat milk",
  distance: 0.38,
  similarity: 0.62,
  rank_score: 0.71,
  confidence: 0.9,
  ...over,
});

describe("SearchWhy", () => {
  afterEach(cleanup);

  it("shows rank, similarity, score and the ranking terms by magnitude", () => {
    render(
      <SearchWhy
        rank={3}
        hit={hit({
          explanation: "fact hit from namespace alice; similarity=0.62; rank_score=0.71; mode=hybrid",
          score_components: {
            rank_score: 0.71,
            key_match_boost_effective: 0.12,
            date_proximity_boost: -0.05,
            semantic: 0.25,
          },
        })}
      />,
    );
    const line = screen.getByTestId("search-why");
    expect(line).toHaveTextContent("#3");
    expect(line).toHaveTextContent("similarity 0.62");
    expect(line).toHaveTextContent("score 0.71");
    // Terms by |value|: semantic 0.25, key match 0.12, date proximity -0.05.
    expect(line.textContent).toMatch(/semantic \+0\.25.*key match \+0\.12.*date proximity -0\.05/);
    // The raw server note rides as the hover title, not as visible text.
    expect(line).toHaveAttribute("title", expect.stringContaining("fact hit from namespace alice"));
    expect(line).not.toHaveTextContent("rank_score");
  });

  it("marks a term that pushed the hit down", () => {
    render(<SearchWhy rank={1} hit={hit({ score_components: { reinforcement: -0.2 } })} />);
    const chip = screen.getByText(/reinforcement -0\.20/);
    expect(chip.className).toContain("color-warn");
  });

  it("says so when the server reported no ranking terms", () => {
    render(<SearchWhy rank={1} hit={hit()} />);
    expect(screen.getByTestId("search-why")).toHaveTextContent("no ranking terms reported");
    expect(screen.getByTestId("search-why")).not.toHaveAttribute("title");
  });
});
