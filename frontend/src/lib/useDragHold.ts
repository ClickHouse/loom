import { type DragEvent, useCallback, useRef, useState } from "react";

/**
 * What a drag holds, for the places it may land on to show it. `start` is a dragstart's: it
 * sets the drag's data and holds `value` a tick later, as Chrome aborts a drag whose
 * dragstart reshapes the page (the places that take it are marked); `end` is the dragend's
 * and a drop's, and a drag that ends within that tick holds nothing.
 */
export function useDragHold<T>() {
  const [held, setHeld] = useState<T | null>(null);
  const dragging = useRef(false);
  const start = useCallback((e: DragEvent, value: T, data: string) => {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", data);
    dragging.current = true;
    window.setTimeout(() => {
      if (dragging.current) setHeld(() => value);
    }, 0);
  }, []);
  const end = useCallback(() => {
    dragging.current = false;
    setHeld(null);
  }, []);
  return { held, start, end };
}
