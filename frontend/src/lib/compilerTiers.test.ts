import { describe, expect, it } from "vitest";
import type { CodeCompilerTier } from "../api";
import { tierCards } from "./compilerTiers";

// The Health tab shows the compiler tiers the repository runs, read from the status row,
// not a C++ card and a Python card by name.

const tier = (over: Partial<CodeCompilerTier>): CodeCompilerTier => ({
  producer: "typescript",
  tool: "scip-typescript",
  label: "TypeScript / JavaScript",
  watermark_key: "scip_commit_ts",
  commit: "ef0add7b2598",
  lag: 0,
  updated_at: "2026-09-18 20:54:08",
  primary: true,
  workers: ["code_scip_ts_delta"],
  note: "n",
  ...over,
});

describe("tierCards", () => {
  it("a TypeScript repository shows its scip-typescript tier and no C++ card", () => {
    const cards = tierCards({ compiler_tiers: [tier({})] });
    expect(cards).toHaveLength(1);
    expect(cards[0].title).toBe("Compiler tier · TypeScript / JavaScript (scip-typescript)");
    expect(cards[0].icon).toBe("typescript");
    expect(cards[0].watermark).toBe("ef0add7b2598");
    expect(cards[0].lag).toBe(0);
    expect(cards[0].workers).toEqual(["code_scip_ts_delta"]);
    expect(cards[0].updatedAt).toBe("2026-09-18 20:54:08");
  });

  it("a second live tier is marked as also indexed, after the primary", () => {
    const cards = tierCards({
      compiler_tiers: [
        tier({ producer: "clang", tool: "scip-clang", label: "C++", watermark_key: "scip_commit", lag: 0 }),
        tier({
          producer: "python",
          tool: "scip-python",
          label: "Python",
          watermark_key: "scip_commit_py",
          primary: false,
          lag: 3,
        }),
      ],
    });
    expect(cards.map((c) => c.title)).toEqual([
      "Compiler tier · C++ (scip-clang)",
      "Compiler tier · Python (scip-python) · also indexed",
    ]);
    expect(cards[1].icon).toBe("python");
  });

  it("the configured tier is shown unset before its first run", () => {
    const cards = tierCards({
      compiler_tiers: [
        tier({
          producer: "go",
          tool: "scip-go",
          label: "Go",
          watermark_key: "scip_commit_go",
          commit: "",
          lag: -1,
          updated_at: "",
        }),
      ],
    });
    expect(cards[0].watermark).toBe("");
    expect(cards[0].updatedAt).toBeUndefined();
  });

  it("an entry with no compiler tier gets one not-configured card", () => {
    const cards = tierCards({ compiler_tiers: [] });
    expect(cards).toHaveLength(1);
    expect(cards[0].title).toBe("Compiler tier · not configured");
    expect(cards[0].icon).toBe("none");
    expect(cards[0].lag).toBe(-1);
  });
});
