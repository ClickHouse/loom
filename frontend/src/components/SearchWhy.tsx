import type { SearchHit } from "../api";
import { signed, whyTerms } from "../lib/whyNote";

// Under each ranked hit in the Memories list, one quiet line that says why it is there —
// rank, similarity, and the ranking terms that moved it (key match, date proximity,
// entity, reinforcement, ...). The server already computes all of it (`explanation` +
// `score_components` on memory.search); this makes it visible without leaving the list
// for Recall Lab, which shows the vector leg only.

interface SearchWhyProps {
  hit: SearchHit;
  /** 1-based position in the ranked result list. */
  rank: number;
}

export function SearchWhy({ hit, rank }: SearchWhyProps) {
  const terms = whyTerms(hit.score_components);
  const similarity = typeof hit.distance === "number" && Number.isFinite(hit.distance) ? 1 - hit.distance : null;
  const explanation = String(hit.explanation ?? "")
    .replace(/\n/g, " ")
    .trim();
  return (
    <div
      className="mt-1 px-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs text-[var(--color-text-muted)] tabular"
      title={explanation || undefined}
      data-testid="search-why"
    >
      <span className="font-medium text-[var(--color-text)]">#{rank}</span>
      {similarity !== null && <span>similarity {similarity.toFixed(2)}</span>}
      {typeof hit.rank_score === "number" && <span>score {hit.rank_score.toFixed(2)}</span>}
      {terms.length === 0 && !explanation && <span className="italic">no ranking terms reported</span>}
      {terms.map((t) => (
        <span
          key={t.label}
          className={`px-1.5 rounded-md bg-[var(--color-surface-2)] ${t.value < 0 ? "text-[var(--color-warn)]" : ""}`}
        >
          {t.label} {signed(t.value)}
        </span>
      ))}
    </div>
  );
}
