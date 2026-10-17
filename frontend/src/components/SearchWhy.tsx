import { Badge } from "@clickhouse/click-ui/Badge";
import type { SearchHit } from "../api";
import { IconButton } from "./IconButton";
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
  const similarity = Number.isFinite(hit.similarity) ? hit.similarity : null;
  const explanation = String(hit.explanation ?? "")
    .replace(/\n/g, " ")
    .trim();
  return (
    <div
      className="mt-1 px-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-text-muted tabular"
      data-testid="search-why"
    >
      <span className="font-medium text-text">#{rank}</span>
      {similarity !== null && <span>similarity {similarity.toFixed(2)}</span>}
      {typeof hit.rank_score === "number" && <span>score {hit.rank_score.toFixed(2)}</span>}
      {terms.length === 0 && !explanation && <span className="italic">no ranking terms reported</span>}
      {terms.map((t) => (
        <Badge
          key={t.label}
          size="sm"
          state={t.value < 0 ? "warning" : "default"}
          text={`${t.label} ${signed(t.value)}`}
        />
      ))}
      {explanation && <IconButton icon="info-in-circle" size="xs" label="Ranking note" tooltip={explanation} />}
    </div>
  );
}
