import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useKeyboard } from "./hooks";

const OPEN = "[data-row-open]";
// A row: a ui/ListRow, or a table row whose open button carries data-row-open.
const ROW = ".list-row, tr";
const CHECK = '[role="checkbox"], input[type="checkbox"]';
const CONTROL = 'a[href], button, input, select, textarea, [tabindex], [role="button"]';
/** Marks a row control this hook took out of the tab order; its value is the tabindex
 *  attribute the control had ("" for none), given back when its row is current again. */
const OUT = "data-rove-out";

export interface RowNavOptions {
  /** Off while a drawer or dialog over the list owns the keys, or the list is empty. */
  enabled?: boolean;
}

/**
 * Keyboard for a list of `ui/ListRow`s whose open buttons carry `data-row-open`: a roving
 * tabindex (Click UI's Table has no row keys), as the Tasks list moves. The list is one tab
 * stop (the row last focused, else the first), `↑/↓` move the focus from row to row,
 * `Home/End` jump to the ends, `Enter` opens the focused row (its button's own click),
 * and `Space` toggles the row's checkbox instead of opening it. Escape is the caller's (it
 * closes its drawer, or clears a selection). Returns the ref for the list's container.
 *
 * The arrows and `Home/End` move only from a row or the page itself (through `useKeyboard`,
 * which leaves fields alone), so a focused menu trigger or select keeps its own.
 *
 * Only the current row's controls (its tag chips, its menu) are tab stops, after its open
 * button; every other row's are out of the tab order, and a control that takes the focus
 * (a click) makes its row the current one. The checkboxes are never tab stops: `Space`
 * reaches them.
 */
export function useRowNav({ enabled = true }: RowNavOptions = {}): (el: HTMLElement | null) => void {
  const [list, setList] = useState<HTMLElement | null>(null);
  const stop = useRef<HTMLElement | null>(null);

  const rows = useCallback((): HTMLElement[] => (list ? [...list.querySelectorAll<HTMLElement>(OPEN)] : []), [list]);

  // One tab stop: the current row's open button takes tabindex 0, every other row's -1,
  // and so do the other rows' controls. Re-applied as rows come and go (a page change, a
  // search), memoised rows included.
  const roveTo = useCallback(
    (target: HTMLElement | null) => {
      const all = rows();
      const current = target && all.includes(target) ? target : all[0];
      stop.current = current ?? null;
      for (const el of all) {
        el.tabIndex = el === current ? 0 : -1;
        for (const c of rowOf(el).querySelectorAll<HTMLElement>(CONTROL)) {
          if (c === el) continue;
          if (c.matches(CHECK)) c.tabIndex = -1;
          else if (el !== current && c.tabIndex >= 0) {
            c.setAttribute(OUT, c.getAttribute("tabindex") ?? "");
            c.tabIndex = -1;
          } else if (el === current && c.hasAttribute(OUT)) {
            const was = c.getAttribute(OUT);
            if (was) c.setAttribute("tabindex", was);
            else c.removeAttribute("tabindex");
            c.removeAttribute(OUT);
          }
        }
      }
    },
    [rows],
  );

  useEffect(() => {
    if (!list) return;
    roveTo(stop.current);
    // Focus on a row's open button or one of its controls makes that row the current one.
    const onFocusIn = (e: FocusEvent) => {
      const at = e.target instanceof Element ? e.target : null;
      const open = at?.closest<HTMLElement>(OPEN) ?? at?.closest(ROW)?.querySelector<HTMLElement>(OPEN);
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
    const watch = new MutationObserver(() => roveTo(stop.current));
    watch.observe(list, { childList: true, subtree: true });
    return () => {
      list.removeEventListener("focusin", onFocusIn);
      list.removeEventListener("keydown", onSpace);
      list.removeEventListener("keyup", onSpace);
      watch.disconnect();
    };
  }, [list, roveTo]);

  const bindings = useMemo(() => {
    const here = (): HTMLElement | null => {
      const el = document.activeElement;
      return el && list?.contains(el) ? (el.closest<HTMLElement>(ROW)?.querySelector<HTMLElement>(OPEN) ?? null) : null;
    };
    const onList = () => {
      const el = document.activeElement;
      return !el || el === document.body || !!(list && list.contains(el));
    };
    // The row scrolls into view, not its open button: the ring goes round the whole row.
    const focus = (el: HTMLElement | undefined): true => {
      if (el) rowOf(el).scrollIntoView?.({ block: "nearest" });
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
  return open.closest(ROW) ?? open.parentElement ?? open;
}
