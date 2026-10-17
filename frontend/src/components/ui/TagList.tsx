import { Chip } from "./Chip";

export interface TagListProps {
  tags: string[];
  /** Pressing a tag filters the list by it, or takes the filter off again. */
  onTagClick?: (tag: string) => void;
  /** Tags the list is filtered by, lowercased: their chips read as pressed. */
  activeTags?: ReadonlySet<string>;
  /** Tags shown before the rest fold into "+N". */
  max?: number;
  className?: string;
}

/**
 * A row's tags: outlined chips (an agent is a filled badge, so outline against fill tells
 * the two apart without a `#`), at most three, then "+N" with the rest in its tooltip. A
 * tag the list is filtered by reads as pressed, the fill Click UI gives a checked control.
 */
export function TagList({ tags, onTagClick, activeTags, max = 3, className = "" }: TagListProps) {
  if (tags.length === 0) return null;
  const shown = tags.slice(0, max);
  const rest = tags.slice(max);
  return (
    <div className={`flex flex-wrap items-center gap-1.5 ${className}`}>
      {shown.map((t) => (
        <Chip
          key={t}
          label={t}
          outline
          onClick={onTagClick ? () => onTagClick(t) : undefined}
          selected={onTagClick ? (activeTags?.has(t.toLowerCase()) ?? false) : undefined}
          ariaLabel={onTagClick ? `Filter by tag ${t}` : undefined}
          className="relative z-[1]"
        />
      ))}
      {rest.length > 0 && (
        <Chip
          label={`+${rest.length}`}
          outline
          ariaLabel={`${rest.length} more tags: ${rest.join(", ")}`}
          tip={rest.join(", ")}
        />
      )}
    </div>
  );
}
