// The shell tax.
//
// The shell's own org-level reads — badge numbers, the approvals count, a repository list
// for a closed popover — are not what the first paint waits for. `afterPaint` runs work
// once the browser has painted (two animation frames) and is idle; the session cache lets
// a badge show its last answer at once on a full reload and refresh behind, so the
// deferral is invisible.
import { useEffect, useRef, type DependencyList } from "react";

/** Run `fn` after the next paint and when the main thread is idle (at most
 *  `timeoutMs` later). Returns a cancel function. In environments without
 *  animation frames (tests) it falls back to a short timer. */
export function afterPaint(fn: () => void, timeoutMs = 400): () => void {
  let cancelled = false;
  let idleId: number | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const run = () => {
    if (!cancelled) fn();
  };
  const schedule = () => {
    if (cancelled) return;
    if (typeof window.requestIdleCallback === "function")
      idleId = window.requestIdleCallback(run, { timeout: timeoutMs });
    else timer = setTimeout(run, 0);
  };
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(() => requestAnimationFrame(schedule));
  } else {
    timer = setTimeout(schedule, 30);
  }
  return () => {
    cancelled = true;
    if (idleId !== null && typeof window.cancelIdleCallback === "function") window.cancelIdleCallback(idleId);
    if (timer !== null) clearTimeout(timer);
  };
}

/** `useEffect` whose body runs after paint + idle instead of at commit. The
 *  body may return a cleanup, called on deps change / unmount as usual. */
export function useAfterPaint(fn: () => void | (() => void), deps: DependencyList, enabled = true): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let cleanup: void | (() => void);
    const cancel = afterPaint(() => {
      cleanup = fnRef.current();
    });
    return () => {
      cancel();
      if (typeof cleanup === "function") cleanup();
    };
    // The caller's deps are forwarded (the rule cannot see through a spread); `fn` rides fnRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);
}

const SESSION_PREFIX = "loom_badge_v1:";

/** A badge answer remembered for this tab's session: `null` when absent,
 *  expired or unreadable. */
export function readSession<T>(key: string, ttlMs: number): T | null {
  try {
    const raw = sessionStorage.getItem(SESSION_PREFIX + key);
    if (!raw) return null;
    // Cast: only writeSession writes this prefix; `at` is still checked below.
    const { at, v } = JSON.parse(raw) as { at: number; v: T };
    if (typeof at !== "number" || Date.now() - at > ttlMs) return null;
    return v;
  } catch {
    return null;
  }
}

export function writeSession<T>(key: string, v: T): void {
  try {
    sessionStorage.setItem(SESSION_PREFIX + key, JSON.stringify({ at: Date.now(), v }));
  } catch {
    /* storage unavailable */
  }
}
