import { cloneElement, useId, type MouseEvent, type ReactElement } from "react";
import { Tip } from "./Tooltip";

/**
 * A control that is unavailable for a reason the user should be able to read. With a
 * `reason`, the child stays focusable but `aria-disabled` (a natively disabled control takes
 * no hover or focus, so the reason would be unreachable), its click does nothing, and the
 * reason is its tooltip and accessible description. Without one, the child renders as is.
 */
export function Unavailable({ reason, children }: { reason?: string | null; children: ReactElement }) {
  const id = useId();
  if (!reason) return children;
  return (
    <>
      <Tip content={reason}>
        {cloneElement(children, {
          "aria-disabled": "true",
          "aria-describedby": id,
          onClick: (e: MouseEvent) => e.preventDefault(),
        })}
      </Tip>
      <span id={id} className="sr-only">
        {reason}
      </span>
    </>
  );
}
