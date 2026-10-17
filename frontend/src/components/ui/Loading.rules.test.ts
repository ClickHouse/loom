import { describe, expect, it } from "vitest";

// The loading rule (docs/development/dashboard-design.md): a skeleton is always inside a
// `LoadingSkeleton`, which names it for a screen reader and holds it back 300 ms. A file that
// renders `Skeleton`, `SkeletonCard` or `SkeletonText` must also use `LoadingSkeleton`.

const SOURCES = import.meta.glob<string>(["../../**/*.tsx", "!../../**/*.test.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
});
const ALLOWED = new Set(["../Skeleton.tsx", "./Loading.tsx"]);

describe("skeletons", () => {
  it("render only inside LoadingSkeleton", () => {
    const bare = Object.entries(SOURCES)
      .filter(([path, text]) => !ALLOWED.has(path) && /<Skeleton(Card|Text)?[\s/>]/.test(text))
      .filter(([, text]) => !text.includes("<LoadingSkeleton"))
      .map(([path]) => path);
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(bare).toEqual([]);
  });
});
