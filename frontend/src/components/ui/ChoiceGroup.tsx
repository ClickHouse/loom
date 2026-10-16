import { RadioGroup } from "@clickhouse/click-ui/RadioGroup";
import { type ReactNode, useEffect, useId, useRef } from "react";

export interface ChoiceOption<V extends string> {
  value: V;
  label: string;
  hint?: string;
  disabled?: boolean;
}

export interface ChoiceGroupProps<V extends string> {
  label: string;
  value: V;
  onChange: (value: V) => void;
  options: readonly ChoiceOption<V>[];
  disabled?: boolean;
  inline?: boolean;
  error?: ReactNode;
}

/**
 * ChoiceGroup — the single-choice control. Click UI's RadioGroup copies each item's
 * label into `aria-label` with String(label), and its group label points at a div, so
 * here the labels are strings, the visible label names the group through
 * `aria-labelledby`, and a hint is the radio's description.
 */
export function ChoiceGroup<V extends string>({
  label,
  value,
  onChange,
  options,
  disabled,
  inline = false,
  error,
}: ChoiceGroupProps<V>) {
  const labelId = useId();
  return (
    <RadioGroup
      value={value}
      onValueChange={(v) => {
        const next = options.find((o) => o.value === v);
        if (next) onChange(next.value);
      }}
      disabled={disabled}
      inline={inline}
      error={error}
      label={<span id={labelId}>{label}</span>}
      aria-labelledby={labelId}
    >
      {options.map((o) => (
        <Choice key={o.value} option={o} />
      ))}
    </RadioGroup>
  );
}

function Choice<V extends string>({ option }: { option: ChoiceOption<V> }) {
  const ref = useRef<HTMLDivElement>(null);
  const hintId = useId();
  const { hint } = option;
  // Click UI's Item spreads extra props onto its wrapper, not the radio, so the
  // description is set on the radio itself.
  useEffect(() => {
    const radio = ref.current?.querySelector('[role="radio"]');
    if (!radio) return;
    if (hint) radio.setAttribute("aria-describedby", hintId);
    else radio.removeAttribute("aria-describedby");
  }, [hint, hintId]);
  return (
    <div ref={ref}>
      <RadioGroup.Item value={option.value} label={option.label} disabled={option.disabled} />
      {hint && (
        <p id={hintId} className="ml-6 text-xs text-text-muted">
          {hint}
        </p>
      )}
    </div>
  );
}
