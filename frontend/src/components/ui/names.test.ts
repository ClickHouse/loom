import { describe, expect, it } from "vitest";
import * as ui from "./index";

// One name, one component: a file outside components/ui that declares its own component
// under a primitive's name (a local `Stat` beside `ui/Stat`) reads as the shared one.

const sources = import.meta.glob<string>(["../../**/*.tsx", "!../../**/*.test.tsx", "!./**"], {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("ui primitive names", () => {
  it("no other file declares a component named after a ui primitive", () => {
    const names = Object.keys(ui).filter((n) => /^[A-Z]/.test(n));
    const clashes = Object.entries(sources).flatMap(([file, src]) =>
      names.filter((n) => new RegExp(`\\b(function|const|class) ${n}\\b`).test(src)).map((n) => `${file}: ${n}`),
    );
    expect(Object.keys(sources).length).toBeGreaterThan(50);
    expect(clashes).toEqual([]);
  });
});
