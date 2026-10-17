import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useKeyboard } from "./hooks";

const OPEN = "[data-row-open]";
const CHECK = '[role="checkbox"], input[type="checkbox"]';

export interface RowNavOptions {
  /** Off while a drawer or dialog over the list owns the keys, or the list is empty. */
  enabled?: boolean;
}

/**
 * Keyboard for a list of `ui/ListRow`s whose open buttons carry `data-row-open`, the way
 * Click UI's Table and the Tasks list move: the list is one tab stop (the row last focused,
 * else the first), `↑/↓` and `j/k` move the focus from row to row, `Home/End` jump to the
 * ends, `Enter` opens the focused row (its button's own click), and `Space` toggles the
 * row's checkbox instead of opening it. Escape is the caller's (it closes its drawer, or
 * clears a selection). Returns the ref for the list's container.
 *
 * `j/k` work from anywhere on the page that is not a field and honour the single-key
 * switch (`lib/shortcuts.ts`, through `useKeyboard`); the arrows and `Home/End` move only
 * from a row or the page itself, so a focused menu trigger or select keeps its own. The
 * rows' checkboxes leave the tab order with the rows: `Space` reaches them.
 */
export function useRowNav({ enabled = true }: RowNavOptions = {}): (el: HTMLElement | null) => void {
  const [list, setList] = useState<HTMLElement | null>(null);
  const stop = useRef<HTMLElement | null>(null);

  const rows = useCallback((): HTMLElement[] => (list ? [...list.querySelectorAll<HTMLElement>(OPEN)] : []), [list]);

  // One tab stop: the current row's open button takes tabindex 0, every other row's -1.
  // Re-applied as rows come and go (a page change, a search), memoised rows included.
  const roveTo = useCallback(
    (target: HTMLElement | null) => {
      const all = rows();
      const current = target && all.includes(target) ? target : all[0];
      stop.current = current ?? null;
      for (const el of all) {
        el.tabIndex = el === current ? 0 : -1;
        for (const box of rowOf(el).querySelectorAll<HTMLElement>(CHECK)) box.tabIndex = -1;
      }
    },
    [rows],
  );

  useEffect(() => {
    if (!list) return;
    roveTo(stop.current);
    const onFocusIn = (e: FocusEvent) => {
      const open = e.target instanceof Element ? e.target.closest<HTMLElement>(OPEN) : null;
      if (open && open !== stop.current) roveTo(open);
    };
    // A button clicks on Space's keyup, so both halves are taken.
    const onSpace = (e: KeyboardEvent) => {
      if (e.key !== " " || e.defaultPrevented || !(e.target instanceof Element) || !e.target.matches(OPEN)) return;
      const box = rowOf(e.target).querySelector<HTMLElement>(CHECK);
      if (!box) return;
      e.preventDefault();
      if (e.type === "keydown" && !e.repeat) box.click();
    };
    list.addEventListener("focusin", onFocusIn);
    list.addEventListener("keydown", onSpace);
    list.addEventListener("keyup", onSpace);
    const watch = typeof MutationObserver === "undefined" ? null : new MutationObserver(() => roveTo(stop.current));
    watch?.observe(list, { childList: true, subtree: true });
    return () => {
      list.removeEventListener("focusin", onFocusIn);
      list.removeEventListener("keydown", onSpace);
      list.removeEventListener("keyup", onSpace);
      watch?.disconnect();
    };
  }, [list, roveTo]);

  const bindings = useMemo(() => {
    const here = (): HTMLElement | null => {
      const el = document.activeElement;
      return el && list?.contains(el)
        ? (el.closest<HTMLElement>(".list-row")?.querySelector<HTMLElement>(OPEN) ?? null)
        : null;
    };
    const onList = () => {
      const el = document.activeElement;
      return !el || el === document.body || !!(list && list.contains(el));
    };
    const focus = (el: HTMLElement | undefined): true => {
      el?.scrollIntoView?.({ block: "nearest" });
      el?.focus({ preventScroll: true });
      return true;
    };
    const move = (step: number): true => {
      const all = rows();
      const at = here();
      // From outside the list, the first move lands on its tab stop.
      if (!at) return focus(stop.current && all.includes(stop.current) ? stop.current : all[0]);
      return focus(all[Math.max(0, Math.min(all.length - 1, all.indexOf(at) + step))]);
    };
    return {
      j: () => move(1),
      k: () => move(-1),
      ArrowDown: () => onList() && move(1),
      ArrowUp: () => onList() && move(-1),
      Home: () => !!here() && focus(rows()[0]),
      End: () => !!here() && focus(rows().at(-1)),
    };
  }, [list, rows]);
  useKeyboard(bindings, enabled && !!list);

  return setList;
}

/** The row an open button belongs to: its `ui/ListRow`, else the button's parent. */
function rowOf(open: Element): Element {
  return open.closest(".list-row") ?? open.parentElement ?? open;
}
