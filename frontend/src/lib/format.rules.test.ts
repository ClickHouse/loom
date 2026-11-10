import { describe, expect, it } from "vitest";

// A server time is read with parseChTime (docs/development/dashboard-design.md). This checks
// Date.parse only: a regex cannot tell new Date(<string>) from new Date(<number>).

const SOURCES = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("server times", () => {
  it("are never read with Date.parse", () => {
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      [...text.matchAll(/\bDate\.parse\(/g)].map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
    );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(calls).toEqual([]);
  });
});

describe("error toasts", () => {
  // `toastError(context, e)` keeps one lead-in, the request id and the reading time; a
  // hand-built "context: ${humanizeError(e)}" toast reads "Pin failed: Couldn't do that: …".
  it("are raised with toastError, never as a context glued to humanizeError", () => {
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      [...text.matchAll(/toast\(`[^`]*:\s*\$\{humanizeError\(/g)].map(
        (m) => `${path}:${text.slice(0, m.index).split("\n").length}`,
      ),
    );
    expect(calls).toEqual([]);
  });
});
