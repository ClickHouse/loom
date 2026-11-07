import { cloneElement, useId, type MouseEvent, type ReactElement } from "react";
import { Tip } from "./Tooltip";

/**
 * A control that is unavailable for a reason the user should be able to read. With a
 * `reason`, the child stays focusable but `aria-disabled` (a natively disabled control takes
 * no hover or focus, so the reason would be unreachable), its click does nothing, and the
 * reason is its tooltip and accessible description. Without one, the child renders as is,
 * inside the same tooltip turned off: a reason that comes and goes never remounts the child,
 * so a focused control keeps its focus. `tip` is the tooltip, shown under the reason when
 * there is one; the description is the reason alone.
 *
 * The child is a Click UI Button. `data-unavailable` carries its `type` so styles.css can
 * give it the disabled look Click UI only gives `:disabled`.
 */
export function Unavailable({
  reason,
  tip,
  children,
}: {
  reason?: string | null;
  tip?: string;
  children: ReactElement;
}) {
  const id = useId();
  const content =
    reason && tip ? (
      <>
        {reason}
        <span className="block break-all">{tip}</span>
      </>
    ) : (
      reason || tip
    );
  return (
    <>
      <Tip content={content} disabled={!content}>
        {reason
          ? cloneElement(children, {
              "aria-disabled": "true",
              "data-unavailable": (children.props as { type?: string }).type ?? "primary",
              "aria-describedby": id,
              onClick: (e: MouseEvent) => e.preventDefault(),
            })
          : children}
      </Tip>
      {reason && (
        <span id={id} className="sr-only">
          {reason}
        </span>
      )}
    </>
  );
}
