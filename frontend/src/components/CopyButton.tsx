import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";

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
  /** Placement only (margin, position); the look is Click UI's secondary button. */
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
  return (
    <Button
      type="secondary"
      htmlType="button"
      iconLeft={state === "done" ? "check" : state === "idle" ? "copy" : undefined}
      onClick={onClick}
      className={className ?? "absolute top-2 right-2"}
      aria-label={ariaLabel ?? label}
      aria-live="polite"
    >
      {state === "done" ? copiedLabel : state === "failed" ? "Copy failed — select the text" : label}
    </Button>
  );
}
