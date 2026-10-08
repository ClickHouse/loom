import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

/** A copy button that answers in place: the label turns into a tick and
 *  "Copied" for two seconds, then returns. A toast at the bottom of the
 *  viewport would slide under the mint dialog, so the person could not tell whether the
 *  click had taken. Falls back to "Copy failed" when the clipboard refuses. */
export function CopyButton({
  text,
  label,
  copiedLabel = "Copied",
  className,
  ariaLabel,
}: {
  /** The text to copy, or a function producing it at click time. */
  text: string | (() => string);
  label: string;
  copiedLabel?: string;
  className?: string;
  ariaLabel?: string;
}) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = setTimeout(() => setState("idle"), 2000);
    return () => clearTimeout(t);
  }, [state]);
  const onClick = async () => {
    const value = typeof text === "function" ? text() : text;
    try {
      await navigator.clipboard.writeText(value);
      setState("done");
    } catch {
      setState("failed");
    }
  };
  const base =
    className ??
    "absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-2xs font-medium bg-[var(--color-surface)] border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-3)]";
  const tone = state === "done" ? " text-success border-success/50" : state === "failed" ? " text-danger" : "";
  return (
    <button type="button" onClick={onClick} className={base + tone} aria-label={ariaLabel ?? label} aria-live="polite">
      {state === "done" ? (
        <>
          <Check size={11} aria-hidden="true" /> {copiedLabel}
        </>
      ) : state === "failed" ? (
        <>Copy failed — select the text</>
      ) : (
        <>
          <Copy size={11} aria-hidden="true" /> {label}
        </>
      )}
    </button>
  );
}
