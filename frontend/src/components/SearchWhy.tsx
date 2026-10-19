import { Badge } from "@clickhouse/click-ui/Badge";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { SearchHit } from "../api";
import { IconButton } from "./IconButton";
import { PopoverTrigger } from "./ui/Menu";
import { signed, whyTerms } from "../lib/whyNote";

// A search hit's "Why this result" in the Memories list: an info button in the row's meta
// slot whose popover says why the row is there — rank, similarity, blended score and the
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
  const similarity = Number.isFinite(hit.similarity) ? hit.similarity : null;
  const explanation = String(hit.explanation ?? "")
    .replace(/\n/g, " ")
    .trim();
  return (
    <>
      <p className="flex flex-wrap gap-x-2">
        <span className="font-medium text-text">#{rank}</span>
        {similarity !== null && <span>similarity {similarity.toFixed(2)}</span>}
        {typeof hit.rank_score === "number" && <span>score {hit.rank_score.toFixed(2)}</span>}
      </p>
      {terms.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {terms.map((t) => (
            <Badge
              key={t.label}
              size="sm"
              state={t.value < 0 ? "warning" : "default"}
              text={`${t.label} ${signed(t.value)}`}
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
