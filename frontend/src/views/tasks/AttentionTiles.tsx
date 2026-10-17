import { ATTENTIONS, ATTENTION_META, type Attention } from "../../lib/tasks";

/**
 * Four tiles that answer "what needs me?": triage to decide, blocked on a person, failed,
 * a lease ending within ten minutes. Each is a toggle filter over the loaded rows; a zero
 * is muted. No coloured dot: the lanes' badges carry the state.
 */
export function AttentionTiles({
  counts,
  active,
  onToggle,
}: {
  counts: Record<Attention, number>;
  active: Attention | null;
  onToggle: (k: Attention) => void;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: a labelled cluster of toggle tiles, not form fields
    <div className="tq-attn" role="group" aria-label="Needs you">
      {ATTENTIONS.map((k) => {
        const n = counts[k];
        const meta = ATTENTION_META[k];
        return (
          <button
            key={k}
            type="button"
            className="tq-tile"
            data-zero={n === 0}
            aria-pressed={active === k}
            onClick={() => onToggle(k)}
            aria-label={`${meta.label}: ${n}`}
          >
            <span>
              <span className="block text-xs text-text-muted">{meta.label}</span>
              <span className="n tabular">{n}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
