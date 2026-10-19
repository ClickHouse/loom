import { useCallback, useMemo, useRef, useState } from "react";
import { useKeyboard } from "./hooks";

export interface RowSelection {
  /** The selected keys among the rows on the page, in no particular order. */
  selected: ReadonlySet<string>;
  /** Toggle one row; with `range` (a Shift-click), select every row from the last one
   *  toggled to this one instead. */
  toggle: (key: string, range?: boolean) => void;
  /** Every row on the page is selected (and there is one). */
  allSelected: boolean;
  /** Select every row on the page, or clear them all once they are. */
  toggleAll: () => void;
  clear: () => void;
}

/**
 * The checkbox selection of a list page: one row at a time as Click UI's selectable Table,
 * plus Loom's own range and clear (Click UI's Table has neither): a Shift-click selects the
 * range from the row last toggled, the select-all checkbox takes every row on the page, and
 * Escape clears the selection (only while there is one, so Escape is otherwise left to the
 * page). `keys` are the page's rows in their order on screen; a selected row that leaves the
 * page (a new page, a search) leaves the selection, so a bulk action only ever acts on rows
 * in view.
 */
export function useRowSelection(keys: readonly string[], { enabled = true }: { enabled?: boolean } = {}): RowSelection {
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const onPage = useMemo(() => new Set(keys), [keys]);
  const selected = useMemo(() => {
    const kept = [...picked].filter((k) => onPage.has(k));
    return kept.length === picked.size ? picked : new Set(kept);
  }, [picked, onPage]);
  // A row that left the page leaves the selection for good: one that comes back (the page
  // before, a cleared search, another namespace's row of the same key) is not selected.
  if (selected !== picked) setPicked(selected);

  // Read through refs, so the callbacks stay stable for memoised rows.
  const keysRef = useRef(keys);
  keysRef.current = keys;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const anchor = useRef<string | null>(null);

  const toggle = useCallback((key: string, range = false) => {
    const all = keysRef.current;
    const next = new Set(selectedRef.current);
    const from = anchor.current === null ? -1 : all.indexOf(anchor.current);
    const to = all.indexOf(key);
    if (range && from >= 0 && to >= 0) {
      const [lo, hi] = from < to ? [from, to] : [to, from];
      for (const k of all.slice(lo, hi + 1)) next.add(k);
    } else if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    anchor.current = key;
    setPicked(next);
  }, []);

  const clear = useCallback(() => {
    anchor.current = null;
    setPicked(new Set());
  }, []);

  const allSelected = keys.length > 0 && selected.size === keys.length;
  const toggleAll = useCallback(() => {
    anchor.current = null;
    const all = keysRef.current;
    setPicked(selectedRef.current.size === all.length ? new Set() : new Set(all));
  }, []);

  const escape = useMemo(() => ({ Escape: clear }), [clear]);
  useKeyboard(escape, enabled && selected.size > 0);

  return { selected, toggle, allSelected, toggleAll, clear };
}
