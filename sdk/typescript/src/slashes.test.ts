import { describe, expect, it } from "vitest";
import { trimSlashes } from "./slashes.js";

describe("trimSlashes", () => {
  it("drops the trailing run, and the leading one when asked", () => {
    expect(trimSlashes("https://loom.example/api///")).toBe("https://loom.example/api");
    expect(trimSlashes("//a/b//")).toBe("//a/b");
    expect(trimSlashes("//a/b//", true)).toBe("a/b");
    expect(trimSlashes("///", true)).toBe("");
    expect(trimSlashes("")).toBe("");
  });

  it("is linear in a long run of slashes", () => {
    const long = `${"/".repeat(200_000)}x`;
    const started = Date.now();
    expect(trimSlashes(long, true)).toBe("x");
    expect(Date.now() - started).toBeLessThan(1000);
  });
});
