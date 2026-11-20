import { type DragEvent, type ReactNode, useState } from "react";
import { DRAG_TYPE } from "../../lib/useDragHold";

/**
 * DropZone — a dashed area files can be dropped on, around the control that picks them
 * (Click UI has no drop target). The zone is no button: the picker inside is how a keyboard
 * or a touch screen picks files. While `busy` (a file on its way) it shows no target and the
 * cursor says no drop, so the browser fires no drop, and one that comes anyway is not handed on.
 * A document or a collection the page moves by dragging (`DRAG_TYPE`) is no file: the zone
 * leaves it alone.
 */
export function DropZone({
  onDrop,
  busy = false,
  className = "",
  children,
  ...rest
}: {
  onDrop: (e: DragEvent<HTMLDivElement>) => void;
  busy?: boolean;
  className?: string;
  children: ReactNode;
  "data-testid"?: string;
}) {
  const [over, setOver] = useState(false);
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: a drop target; the picker inside is how a keyboard picks files
    // biome-ignore lint/a11y/noNoninteractiveElementInteractions: the same drop target
    <div
      {...rest}
      onDragOver={(e) => {
        if (e.dataTransfer.types?.includes(DRAG_TYPE)) return;
        e.preventDefault();
        if (busy) e.dataTransfer.dropEffect = "none";
        else setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        if (e.dataTransfer.types?.includes(DRAG_TYPE)) return;
        e.preventDefault();
        setOver(false);
        if (!busy) onDrop(e);
      }}
      className={`rounded-md border border-dashed ${over ? "border-accent bg-accent-soft" : "border-border-strong"} ${className}`}
    >
      {children}
    </div>
  );
}
