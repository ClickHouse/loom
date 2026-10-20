import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { SearchHit } from "../api";
import { axeViolations } from "../test/axe";
import { SearchScores, SearchWhy } from "./SearchWhy";

// A ranked hit's similarity and score, shown in the row's meta line, and its "Why this
// result": an info button after the meta whose popover says the rank, similarity, blended
// score and the signed ranking terms.

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

const open = async () => {
  await userEvent.click(screen.getByRole("button", { name: "Why this result" }));
  return screen.findByTestId("search-why");
};

describe("SearchWhy", () => {
  afterEach(cleanup);

  it("is one button until opened; the popover shows rank, similarity, score and the terms by magnitude", async () => {
    const { container } = render(
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
    expect(screen.queryByTestId("search-why")).toBeNull();
    const why = await open();
    expect(why).toHaveTextContent("#3");
    expect(why).toHaveTextContent("similarity 0.62");
    expect(why).toHaveTextContent("score 0.71");
    // Terms by |value|: semantic 0.25, key match 0.12, date proximity -0.05.
    expect(why.textContent).toMatch(/semantic \+0\.25.*key match \+0\.12.*date proximity -0\.05/);
    // The server's note is in the popover as written, flattened to one line.
    expect(why).toHaveTextContent("fact hit from namespace alice");
    expect(await axeViolations(container.ownerDocument.body)).toEqual([]);
  });

  it("shows a reranker's move in places, not as a score", async () => {
    render(<SearchWhy rank={1} hit={hit({ score_components: { rerank_from: 4, rerank_delta: 3, semantic: 0.25 } })} />);
    const why = await open();
    expect(why.textContent).toMatch(/reranked \+3(?!\.)/);
    expect(why.textContent).not.toMatch(/rerank from/);
  });

  it("marks a term that pushed the hit down", async () => {
    render(<SearchWhy rank={1} hit={hit({ score_components: { reinforcement: -0.2 } })} />);
    await open();
    const chip = screen.getByText(/reinforcement -0\.20/);
    expect(chip.closest('[class*="state_warning"]')).not.toBeNull();
  });

  it("says so when the server reported no ranking terms", async () => {
    render(<SearchWhy rank={1} hit={hit()} />);
    expect(await open()).toHaveTextContent("No ranking terms reported");
  });

  it("says a keyword row holds the typed text", async () => {
    render(<SearchWhy contains="refund" />);
    expect(await open()).toHaveTextContent('Contains "refund"');
  });
});

describe("SearchScores", () => {
  afterEach(cleanup);

  it("shows a hit's similarity and score", () => {
    render(<SearchScores hit={hit()} />);
    expect(screen.getByTestId("search-score")).toHaveTextContent("similarity 0.62 · score 0.71");
  });

  it("leaves out the similarity when it is not a number", () => {
    render(<SearchScores hit={hit({ similarity: Number.NaN })} />);
    expect(screen.getByTestId("search-score")).toHaveTextContent(/^score 0\.71$/);
  });

  it("leaves out the score when it is not a number", () => {
    render(<SearchScores hit={hit({ rank_score: Number.NaN })} />);
    expect(screen.getByTestId("search-score")).toHaveTextContent(/^similarity 0\.62$/);
  });

  it("renders nothing when neither score is a number", () => {
    render(<SearchScores hit={hit({ similarity: Number.NaN, rank_score: Number.NaN })} />);
    expect(screen.queryByTestId("search-score")).toBeNull();
  });
});
