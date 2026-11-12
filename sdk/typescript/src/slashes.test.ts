import { describe, expect, it } from "vitest";
import { trimSlashes, trimTrailingSlashes } from "./slashes.js";

/** Every string of up to `n` characters drawn from "/" and "a". */
function allStrings(n: number): string[] {
  let level = [""];
  const out = [""];
  for (let i = 0; i < n; i++) {
    level = level.flatMap((s) => [`${s}/`, `${s}a`]);
    out.push(...level);
  }
  return out;
}

describe("slash trims", () => {
  it("give what the regexes they replace give, for every short string", () => {
    for (const s of allStrings(7)) {
      expect(trimTrailingSlashes(s)).toBe(s.replace(/\/+$/, ""));
      expect(trimSlashes(s)).toBe(s.replace(/^\/+|\/+$/g, ""));
    }
  });

  it("are linear in a long run of slashes", () => {
    const long = `${"/".repeat(200_000)}x`;
    const started = Date.now();
    expect(trimTrailingSlashes(long)).toBe(long);
    expect(trimSlashes(long)).toBe("x");
    // A run that touches neither end is the slow case for the two-ended regex.
    const inner = `x${"/".repeat(200_000)}x`;
    expect(trimSlashes(inner)).toBe(inner);
    expect(Date.now() - started).toBeLessThan(1000);
  });
});
