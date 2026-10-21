import { Badge } from "@clickhouse/click-ui/Badge";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { SearchHit } from "../api";
import { IconButton } from "./IconButton";
import { PopoverTrigger } from "./ui/Menu";
import { termText, whyTerms } from "../lib/whyNote";

// A ranked search hit in the Memories list shows its similarity and blended score in the
// row's meta line (SearchScores), and its "Why this result" info button, after the meta,
// opens a popover that says why the row is there — rank, similarity, blended score and the
// ranking terms that moved it (key match, date proximity, entity, reinforcement, ...), or,
// for a row that holds the typed text, that it does. The server already computes all of it
// (`explanation` + `score_components` on memory.search); the full breakdown, every leg, is
// Recall Lab's.

type SearchWhyProps =
  | {
      hit: SearchHit;
      /** 1-based position in the ranked result list. */
      rank: number;
      contains?: never;
    }
  | { contains: string; hit?: never; rank?: never };

/** A hit's similarity and blended score to two places; null for one the server did not send. */
function scoresOf(hit: SearchHit): { similarity: string | null; score: string | null } {
  return {
    similarity: Number.isFinite(hit.similarity) ? hit.similarity.toFixed(2) : null,
    score: Number.isFinite(hit.rank_score) ? hit.rank_score.toFixed(2) : null,
  };
}

/** How well a hit matched, in the row's meta line; under 640 px the score alone on screen,
 *  with the similarity still read out. */
export function SearchScores({ hit }: { hit: SearchHit }) {
  const { similarity, score } = scoresOf(hit);
  if (similarity === null && score === null) return null;
  return (
    <span data-testid="search-score" className="tabular whitespace-nowrap">
      {similarity !== null && (
        <span className="sr-only sm:not-sr-only sm:whitespace-nowrap">
          similarity {similarity}
          {score !== null && " · "}
        </span>
      )}
      {score !== null && <span>score {score}</span>}
    </span>
  );
}

export function SearchWhy(props: SearchWhyProps) {
  return (
    <Popover>
      <PopoverTrigger>
        <IconButton icon="info-in-circle" size="sm" label="Why this result" />
      </PopoverTrigger>
      <Popover.Content align="end" aria-label="Why this result" className="w-[min(320px,90vw)]">
        <div data-testid="search-why" className="space-y-2 text-xs text-text-muted tabular">
          {props.hit ? <Ranked hit={props.hit} rank={props.rank} /> : <p>Contains "{props.contains}"</p>}
        </div>
      </Popover.Content>
    </Popover>
  );
}

function Ranked({ hit, rank }: { hit: SearchHit; rank: number }) {
  const terms = whyTerms(hit.score_components);
  const { similarity, score } = scoresOf(hit);
  const explanation = String(hit.explanation ?? "")
    .replace(/\n/g, " ")
    .trim();
  return (
    <>
      <p className="flex flex-wrap gap-x-2">
        <span className="font-medium text-text">#{rank}</span>
        {similarity !== null && <span>similarity {similarity}</span>}
        {score !== null && <span>score {score}</span>}
      </p>
      {terms.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {terms.map((t) => (
            <Badge
              key={t.label}
              size="sm"
              state={t.value < 0 ? "warning" : "default"}
              text={`${t.label} ${termText(t)}`}
            />
          ))}
        </div>
      ) : (
        !explanation && <p>No ranking terms reported</p>
      )}
      {explanation && <p className="break-words">{explanation}</p>}
    </>
  );
}
