import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listTime, listTimeTitle, parseChTime } from "./format";

// One date rule for every list row: how long ago within the week, then the
// day, with the year only when it is not this one.
describe("listTime", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-17T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("reads as how long ago within the last week", () => {
    expect(listTime("2026-10-17T11:59:30Z")).toBe("just now");
    expect(listTime("2026-10-17T12:00:10Z")).toBe("just now"); // a little clock skew
    expect(listTime("2026-10-17T11:48:00Z")).toBe("12m ago");
    expect(listTime("2026-10-17T09:00:00Z")).toBe("3h ago");
    expect(listTime("2026-10-14T12:00:00Z")).toBe("3d ago");
  });

  it("is the day from a week back, with the year when it is another one", () => {
    expect(listTime("2026-09-20T14:32:00Z")).toBe("Sep 20");
    expect(listTime("2025-09-20T14:32:00Z")).toBe("Sep 20, 2025");
  });

  it("reads ClickHouse's own format as UTC", () => {
    expect(listTime("2026-10-17 11:48:00")).toBe("12m ago");
    expect(listTime("2026-10-17 11:47:59.123456")).toBe("12m ago");
  });

  it("is empty for nothing or a value that is not a time", () => {
    expect(listTime(null)).toBe("");
    expect(listTime("")).toBe("");
    expect(listTime("soon")).toBe("");
  });

  it("titles the time in UTC", () => {
    expect(listTimeTitle("2026-09-20T14:32:09Z")).toBe("Sep 20, 2026, 14:32 UTC");
    expect(listTimeTitle("2026-09-20 02:05:00")).toBe("Sep 20, 2026, 02:05 UTC");
    expect(listTimeTitle("nope")).toBe("");
  });
});

describe("parseChTime", () => {
  it("reads 'YYYY-MM-DD hh:mm:ss[.fff]' as UTC", () => {
    expect(parseChTime("2026-09-20 14:32:00")?.toISOString()).toBe("2026-09-20T14:32:00.000Z");
    expect(parseChTime("2026-09-20 14:32:00.5")?.toISOString()).toBe("2026-09-20T14:32:00.500Z");
    expect(parseChTime("2026-09-20 14:32:00.123456789")?.toISOString()).toBe("2026-09-20T14:32:00.123Z");
  });

  it("reads ISO, with or without an offset, and a bare date", () => {
    expect(parseChTime("2026-09-20T14:32:00Z")?.toISOString()).toBe("2026-09-20T14:32:00.000Z");
    expect(parseChTime("2026-09-20T16:32:00+02:00")?.toISOString()).toBe("2026-09-20T14:32:00.000Z");
    expect(parseChTime("2026-09-20T14:32:00")?.toISOString()).toBe("2026-09-20T14:32:00.000Z");
    expect(parseChTime("2026-09-20")?.toISOString()).toBe("2026-09-20T00:00:00.000Z");
  });

  it("is null for nothing or garbage", () => {
    expect(parseChTime(null)).toBeNull();
    expect(parseChTime(undefined)).toBeNull();
    expect(parseChTime("")).toBeNull();
    expect(parseChTime("yesterday")).toBeNull();
  });
});
