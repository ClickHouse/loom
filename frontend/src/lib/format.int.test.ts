// One formatter for every integer and tooltip time.
import { describe, expect, it } from "vitest";
import { absoluteTime, fmtInt } from "./format";

describe("fmtInt / absoluteTime", () => {
  it("formats integers through one cached Intl.NumberFormat", () => {
    const ref = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
    expect(fmtInt(1234567)).toBe(ref.format(1234567));
    expect(fmtInt(0)).toBe("0");
    expect(fmtInt(undefined)).toBe("0");
    expect(fmtInt(12.7)).toBe(ref.format(12.7));
  });
  it("formats a tooltip time through the cached formatter and keeps unparseable input", () => {
    const out = absoluteTime("2026-09-19T16:10:00Z");
    expect(out).toMatch(/2026/);
    expect(absoluteTime("not-a-date")).toBe("not-a-date");
    expect(absoluteTime(undefined)).toBe("");
  });
});
