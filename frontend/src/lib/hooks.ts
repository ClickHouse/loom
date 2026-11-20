import { useEffect, useRef, type DependencyList } from "react";

const INTERACTIVE =
  'button, a[href], summary, select, [role="button"], [role="link"], [role="menuitem"], [role="tab"], [role="option"], [role="checkbox"], [role="switch"]';
const LAYER = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]';

/** Bind page-level keys (a list's arrows, Escape closing a drawer). Disabled while the
 *  user is typing in an input / textarea / contenteditable region.
 *
 * Two more cases are left alone: Enter and Space on a focused control — a button, link,
 *  menu item, tab, or anything else tabbable — keep their native activation, so a list
 *  view binding `Enter` cannot swallow Enter on the header's "New" button; a key
 *  pressed inside an open dialog, menu or listbox belongs to that layer; and a key a
 *  focused widget already handled (a menu trigger's ArrowDown) is not handled twice.
 *  A binding that returns `false` declines the key, which keeps its default (scrolling). */
export function useKeyboard(bindings: Record<string, () => boolean | void>, enabled: boolean = true): void {
  useEffect(() => {
    if (!enabled) return;
    const isTyping = (): boolean => {
      const el = document.activeElement;
      if (!(el instanceof HTMLElement)) return false;
      const tag = el.tagName.toLowerCase();
      return tag === "input" || tag === "textarea" || el.isContentEditable;
    };
    const activatesFocusedControl = (e: KeyboardEvent): boolean => {
      if (e.key !== "Enter" && e.key !== " ") return false;
      const el = document.activeElement;
      return (
        (el instanceof HTMLElement || el instanceof SVGElement) &&
        el !== document.body &&
        (el.matches(INTERACTIVE) || (el.tabIndex >= 0 && el.hasAttribute("tabindex")))
      );
    };
    const insideLayer = (e: KeyboardEvent): boolean => {
      const t = e.target;
      return t instanceof Element && !!t.closest(LAYER);
    };
    const onKey = (e: KeyboardEvent): void => {
      if (e.defaultPrevented || isTyping()) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return; // skip when modifiers
      if (activatesFocusedControl(e) || insideLayer(e)) return;
      const handler = bindings[e.key];
      if (handler && handler() !== false) e.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [bindings, enabled]);
}

/** A data effect whose read is cancelled when its view moves on. `run` gets an
 *  `AbortSignal` to hand to the api wrapper and a `live()` predicate to consult after
 *  every await before writing state — a response that lands after the deps changed
 *  (namespace A's rows under namespace B's header) is dropped instead of rendered.
 *  Rejections from the aborted request are swallowed here; `run` still owns its own error
 *  state for live failures. */
export function useAbortableEffect(
  run: (signal: AbortSignal, live: () => boolean) => void | Promise<void>,
  deps: DependencyList,
): void {
  useEffect(() => {
    const ctl = new AbortController();
    let alive = true;
    void Promise.resolve()
      .then(() => run(ctl.signal, () => alive))
      .catch(() => {
        /* superseded or failed — run() reports live failures itself */
      });
    return () => {
      alive = false;
      ctl.abort();
    };
    // The caller's deps are forwarded; the caller is responsible for their completeness.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** A poll that sleeps while the tab is hidden. Ticks every `ms` while `document.hidden`
 *  is false; a tick that falls while the tab is in the background is skipped, and the
 *  first `visibilitychange` back to visible fires `fn` once so the view catches up
 *  immediately instead of waiting out the interval. `fn` is read through a ref, so the
 *  latest closure runs without re-arming the timer. `enabled=false` arms nothing. */
export function useVisiblePoll(fn: () => void, ms: number, enabled: boolean = true): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let missed = false;
    const tick = (): void => {
      if (document.hidden) {
        missed = true;
        return;
      }
      fnRef.current();
    };
    const onVisible = (): void => {
      if (document.hidden || !missed) return;
      missed = false;
      fnRef.current();
    };
    const id = window.setInterval(tick, ms);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [ms, enabled]);
}

/** True while mounted: an async answer checks it before it acts for the component. */
export function useMounted(): { readonly current: boolean } {
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return mounted;
}
