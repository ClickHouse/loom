// The dashboard reports its own paint. One `ui.vitals` row per page view — the route, the
// first view's largest contentful paint, the view's long tasks, its DOM size, its /v1
// calls and the build — posted when the viewer leaves the view (a navigation or the page
// hiding), never inside the view's own measured window. Every observer is optional: a
// browser without one reports what it has.
import { authHeader } from "../api";
import { readCalls, resetCalls } from "./vitalsCounter";

type Identity = { org: string; namespace: string };

let route = "";
let longTaskMs = 0;
let lcpMs: number | null = null;
// The longest interaction of the view — an event-timing entry's duration (input delay +
// processing + presentation), the figure INP is defined by; null when none reached the
// threshold.
let inpMs: number | null = null;
const INP_THRESHOLD_MS = 40;
let firstView = true;
let started = false;

function observe(type: string, cb: (entries: PerformanceEntryList) => void): void {
  try {
    new PerformanceObserver((l) => cb(l.getEntries())).observe({ type, buffered: true });
  } catch {
    /* not supported */
  }
}

/** Install the observers once per document. */
export function installVitals(): void {
  if (started) return;
  started = true;
  observe("longtask", (entries) => {
    for (const e of entries) longTaskMs += e.duration;
  });
  observe("largest-contentful-paint", (entries) => {
    for (const e of entries) lcpMs = Math.round(e.startTime);
  });
  try {
    // lib.dom's PerformanceObserverInit has no `durationThreshold` (Event Timing).
    const init: PerformanceObserverInit & { durationThreshold: number } = {
      type: "event",
      buffered: false,
      durationThreshold: INP_THRESHOLD_MS,
    };
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        const d = Math.round(e.duration);
        if (inpMs === null || d > inpMs) inpMs = d;
      }
    }).observe(init);
  } catch {
    /* no event timing */
  }
}

/** A view began: counters start over. */
export function startView(next: string): void {
  route = next;
  longTaskMs = 0;
  inpMs = null;
  resetCalls();
}

/** The current view ended: one row, fire-and-forget, `keepalive` so a page
 *  that is closing still delivers it. */
export function flushView(identity: Identity | null, build: string): void {
  if (!route || !identity || !identity.org || !identity.namespace) return;
  const body = {
    org: identity.org,
    namespace: identity.namespace,
    agent: "loom-ui",
    consumer: "loom-ui",
    route,
    lcp_ms: firstView ? lcpMs : null,
    long_task_ms: Math.round(longTaskMs),
    inp_ms: inpMs,
    dom_nodes: typeof document !== "undefined" ? document.querySelectorAll("*").length : 0,
    ...readCalls(),
    build: build || "",
    cold: firstView,
  };
  firstView = false;
  route = "";
  try {
    void fetch("/v1/ui.vitals", {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json", ...authHeader() },
      body: JSON.stringify(body),
    }).catch(() => {
      /* telemetry never surfaces */
    });
  } catch {
    /* no fetch */
  }
}

/** Tests: forget everything. */
export function _resetVitals(): void {
  route = "";
  longTaskMs = 0;
  lcpMs = null;
  inpMs = null;
  firstView = true;
  started = false;
  resetCalls();
}
