// Probe globals the Playwright specs install with addInitScript / page.evaluate and read
// back. Test-only: the app never sets them. Declared as present because every spec
// installs its probe before reading it; a probe that never installed throws on the read,
// which fails the test. The specs compile under tsconfig.node.json, where
// src/vite-env.d.ts does not apply.
export {};

declare global {
  interface Window {
    /** Start time of the latest largest-contentful-paint entry. */
    __lcp: number;
    /** Durations of the long tasks observed so far. */
    __lt: number[];
    /** perf-budget: Element.scrollTo calls since boot. */
    __scrolls: number;
    /** perf-budget: durations of the event-timing entries since the last reset. */
    __ev: number[];
    /** graph, perf-budget: layout-shift entries and their summed value. */
    __ls: { entries: number; value: number };
    /** graph: MutationObserver records under the canvas. */
    __mut: number;
    /** preview-isolation: what an uploaded file's script would set on the page; never set. */
    __pwned?: string;
    /** preview-isolation: Content-Security-Policy violations the page reported. */
    __cspViolations: string[];
  }
}
