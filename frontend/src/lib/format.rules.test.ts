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
