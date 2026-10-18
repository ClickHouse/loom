import { ATTENTIONS, ATTENTION_META, type Attention } from "../../lib/tasks";

/**
 * Four tiles that answer "what needs me?": triage to decide, blocked on a person, failed,
 * a lease ending within ten minutes. Each is a toggle filter over the loaded rows, drawn
 * as Click UI's small BigStat (its label, value, stroke and radius; styles.css `.tq-tile`)
 * because a BigStat cannot be a button. The number is the signal: no dot, and a zero is
 * muted. Before the rows load, or when the read failed, a tile shows "–", never a zero.
 */
export function AttentionTiles({
  counts,
  active,
  onToggle,
}: {
  /** Null while the rows are loading or could not be read. */
  counts: Record<Attention, number> | null;
  active: Attention | null;
  onToggle: (k: Attention) => void;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a labelled cluster of toggle tiles, not form fields
    <div className="tq-attn" role="group" aria-label="Needs you">
      {ATTENTIONS.map((k) => {
        const n = counts?.[k];
        const meta = ATTENTION_META[k];
        return (
          <button
            key={k}
            type="button"
            className="tq-tile"
            data-zero={n === 0}
            aria-pressed={active === k}
            disabled={n === undefined}
            onClick={() => onToggle(k)}
            aria-label={`${meta.label}: ${n ?? "not loaded"}`}
          >
            {/* The button's name is its aria-label. A phone has no tiles: the Filters menu
                holds them (FiltersMenu), so the label is never cut short. */}
            <span className="lbl">{meta.label}</span>
            <span className="n tabular">{n ?? "–"}</span>
          </button>
        );
      })}
    </div>
  );
}
