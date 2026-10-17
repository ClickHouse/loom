import { describe, expect, it } from "vitest";

// The loading rule (docs/development/dashboard-design.md): a skeleton is always inside a
// `LoadingSkeleton`, which names it for a screen reader and holds it back 300 ms. A file that
// renders `Skeleton`, `SkeletonCard` or `SkeletonText` has each one inside a `LoadingSkeleton`.

const SOURCES = import.meta.glob<string>(["../../**/*.tsx", "!../../**/*.test.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
});
const ALLOWED = new Set(["../Skeleton.tsx", "./Loading.tsx"]);

describe("skeletons", () => {
  it("render only inside LoadingSkeleton", () => {
    // Each skeleton has a LoadingSkeleton open around it: more opened before it than closed.
    const inside = (text: string, at: number) => {
      const before = text.slice(0, at);
      return before.split("<LoadingSkeleton").length > before.split("</LoadingSkeleton>").length;
    };
    const bare = Object.entries(SOURCES)
      .filter(([path]) => !ALLOWED.has(path))
      .flatMap(([path, text]) =>
        [...text.matchAll(/<Skeleton(Card|Text)?[\s/>]/g)]
          .filter((m) => !inside(text, m.index ?? 0))
          .map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
      );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(bare).toEqual([]);
  });
});
