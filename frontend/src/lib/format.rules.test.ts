import { describe, expect, it } from "vitest";

// The time rule (docs/development/dashboard-design.md): a server time is read with
// `parseChTime`. `Date.parse` reads a zoneless time as local, so it belongs in lib/format.ts
// at most.
// `new Date(<string>)` has the same trap, but a regex cannot tell a string argument from a
// number or a Date, so this checks `Date.parse` only.

const SOURCES = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("server times", () => {
  it("are never read with Date.parse outside lib/format.ts", () => {
    const calls = Object.entries(SOURCES)
      .filter(([path]) => path !== "./format.ts")
      .flatMap(([path, text]) =>
        [...text.matchAll(/\bDate\.parse\(/g)].map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
      );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(calls).toEqual([]);
  });
});
