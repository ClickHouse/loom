import { describe, expect, it } from "vitest";
import { oneOf } from "./oneOf";

const MODES = ["deny", "review", "auto"] as const;

describe("oneOf", () => {
  it("returns a member as its union type", () => {
    const m: (typeof MODES)[number] | null = oneOf(MODES, "review");
    expect(m).toBe("review");
  });

  it("returns the empty string when it is a member", () => {
    expect(oneOf(["", "go"], "")).toBe("");
  });

  it("rejects a value outside the set and null", () => {
    expect(oneOf(MODES, "Review")).toBeNull();
    expect(oneOf(MODES, "")).toBeNull();
    expect(oneOf(MODES, null)).toBeNull();
  });
});
