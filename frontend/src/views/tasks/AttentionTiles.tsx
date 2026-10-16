import { ATTENTIONS, ATTENTION_META, type Attention } from "../../lib/tasks";

/**
 * Four tiles that answer "what needs me?": triage to decide, blocked on a person, failed,
 * a lease ending within ten minutes. Each is a toggle filter over the loaded rows; a zero
 * is muted.
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
              <span className="block text-xs text-[var(--color-text-muted)]">{meta.label}</span>
              <span className="n tabular">{n}</span>
            </span>
            <span
              className="w-2 h-2 rounded-full shrink-0"
              style={{ background: n ? meta.color : "var(--color-surface-3)" }}
              aria-hidden="true"
            />
          </button>
        );
      })}
    </div>
  );
}
