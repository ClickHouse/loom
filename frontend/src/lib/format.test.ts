import { afterEach, describe, expect, it, vi } from "vitest";
import {
  _formatterCacheSize,
  absoluteTime,
  dayLabel,
  daysLeftToRestore,
  displaySubject,
  formatBytesShort,
  refreshDetail,
  isServiceSubject,
  relativeTime,
  restoreText,
  timeOfDay,
  truncate,
  sentenceCase,
} from "./format";

describe("displaySubject", () => {
  it("shows a dash for nothing", () => {
    expect(displaySubject(null)).toBe("—");
    expect(displaySubject(undefined)).toBe("—");
    expect(displaySubject("")).toBe("—");
  });

  it("leaves human subjects alone", () => {
    expect(displaySubject("alice@example.com")).toBe("alice@example.com");
    expect(displaySubject("personal-alice")).toBe("personal-alice");
    expect(isServiceSubject("alice@example.com")).toBe(false);
  });

  it("turns a service-account subject into `<Client> bot`", () => {
    expect(isServiceSubject("sa:reporter-a1b2c3")).toBe(true);
    expect(displaySubject("sa:reporter-a1b2c3")).toBe("Reporter bot");
    expect(displaySubject("sa:data_sync-bot-0123456789ab")).toBe("Data Sync Bot bot");
  });

  it("falls back to `Service account` when no client name is left", () => {
    expect(displaySubject("sa:")).toBe("Service account");
    expect(displaySubject("sa:-abcdef")).toBe("Service account");
  });
});

describe("truncate", () => {
  it("returns short and exact-length strings unchanged", () => {
    expect(truncate("abc", 5)).toBe("abc");
    expect(truncate("abcde", 5)).toBe("abcde");
    expect(truncate("", 3)).toBe("");
  });

  it("cuts at a boundary between the characters a reader sees, within max units", () => {
    // max counts UTF-16 units, as the callers' length checks do; the cut steps back to
    // the last whole character that fits.
    expect(truncate("abc😀d", 5)).toBe("abc…");
    expect(truncate("ab😀cd", 5)).toBe("ab😀…");
    expect(truncate("😀😀😀", 6)).toBe("😀😀😀");
    expect(truncate("ab🇺🇸cd", 7)).toBe("ab🇺🇸…");
    expect(truncate("ab🇺🇸cd", 6)).toBe("ab…");
    expect(truncate("ab👍🏽cd", 7)).toBe("ab👍🏽…");
    expect(truncate("ab👩‍💻cd", 8)).toBe("ab👩‍💻…");
    expect(truncate("abe\u0301cd", 5)).toBe("abe\u0301…");
    expect(truncate("abe\u0301cd", 4)).toBe("ab…");
  });

  it("marks a value one unit over its limit as cut, emoji or not", () => {
    const value = `😀${"word ".repeat(100)}`;
    const cut = truncate(value.slice(0, 321), 320);
    expect(cut.endsWith("…")).toBe(true);
    expect(cut.length).toBeLessThanOrEqual(320);
  });

  it("cuts between code points where the browser has no Intl.Segmenter", async () => {
    vi.resetModules();
    const segmenter = Object.getOwnPropertyDescriptor(Intl, "Segmenter");
    Object.defineProperty(Intl, "Segmenter", { value: undefined, configurable: true, writable: true });
    try {
      const fresh = await import("./format");
      expect(fresh.truncate("ab😀cd", 5)).toBe("ab😀…");
      expect(fresh.truncate("ab🇺🇸cd", 7)).toBe("ab🇺🇸…");
      expect(fresh.truncate("ab🇺🇸cd", 6)).toBe("ab🇺…");
    } finally {
      if (segmenter) Object.defineProperty(Intl, "Segmenter", segmenter);
    }
  });

  it("cuts to max characters including the ellipsis", () => {
    expect(truncate("abcdefgh", 5)).toBe("abcd…");
    expect(truncate("abcdefgh", 5)).toHaveLength(5);
  });

  it("does not leave a space before the ellipsis", () => {
    expect(truncate("abc defgh", 5)).toBe("abc…");
  });
});

// One Intl.DateTimeFormat per shape, built once — a page of a hundred cards built a
// hundred.
describe("date formatters are built once", () => {
  it("timeOfDay, dayLabel and relativeTime reuse their formatter instances", () => {
    const before = _formatterCacheSize();
    const iso = "2026-09-19T11:05:00Z";
    expect(timeOfDay(iso)).toMatch(/\d/);
    expect(timeOfDay("2026-09-18T09:30:00Z")).toMatch(/\d/);
    const afterTime = _formatterCacheSize();
    expect(afterTime - before).toBeLessThanOrEqual(1);
    dayLabel("2025-01-03T10:00:00Z");
    dayLabel("2025-02-03T10:00:00Z");
    relativeTime("2020-01-01T00:00:00Z");
    relativeTime("2019-06-01T00:00:00Z");
    // two shapes at most were added by dayLabel (month-day / month-day-year) and one by relativeTime
    expect(_formatterCacheSize() - afterTime).toBeLessThanOrEqual(4);
    const n = _formatterCacheSize();
    for (let i = 0; i < 50; i++) {
      timeOfDay(iso);
      dayLabel("2025-01-03T10:00:00Z");
      relativeTime("2019-06-01T00:00:00Z");
    }
    expect(_formatterCacheSize()).toBe(n);
  });
});

describe("formatBytesShort", () => {
  it("shows bytes below a kilobyte", () => {
    expect(formatBytesShort(0)).toBe("0 B");
    expect(formatBytesShort(1023)).toBe("1023 B");
  });
  it("shows kilobytes from 1024 up to a megabyte", () => {
    expect(formatBytesShort(1024)).toBe("1.0 KB");
    expect(formatBytesShort(1536)).toBe("1.5 KB");
    expect(formatBytesShort(1024 * 1024 - 1)).toBe("1024.0 KB");
  });
  it("shows megabytes from 1024 KB up", () => {
    expect(formatBytesShort(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytesShort(5 * 1024 * 1024 * 1024)).toBe("5120.0 MB");
  });
});

describe("refreshDetail", () => {
  it("drops the fields a code.refresh caller left empty", () => {
    expect(refreshDetail("agent=catchup expected_head=")).toBe("agent=catchup");
    expect(refreshDetail("agent=ci expected_head=4645c25c46fc")).toBe("agent=ci expected_head=4645c25c46fc");
    expect(refreshDetail("")).toBe("");
  });
});

// A day heading over the memories is a date, so it can be told apart from last week's:
// "Today" and "Yesterday" carry theirs, and an earlier day is its date, never a weekday.
describe("dayLabel", () => {
  afterEach(() => vi.useRealTimers());
  it("names the date of every day", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 17, 12));
    expect(dayLabel(new Date(2026, 9, 17, 9).toISOString())).toBe("Today · Oct 17");
    expect(dayLabel(new Date(2026, 9, 16, 9).toISOString())).toBe("Yesterday · Oct 16");
    expect(dayLabel(new Date(2026, 9, 15, 9).toISOString())).toBe("Oct 15");
    expect(dayLabel(new Date(2025, 11, 31, 9).toISOString())).toBe("Dec 31, 2025");
    // A ClickHouse time: no zone (UTC), a space, nine fraction digits.
    expect(dayLabel("2026-10-16 12:00:00.123456789")).toBe(dayLabel("2026-10-16T12:00:00.123Z"));
  });
});

// Some ops answer a ClickHouse time as it prints ("2026-10-16 12:00:00.123456789": UTC, no
// zone). In Los Angeles (UTC-7 in October), a time read as local would be seven hours later
// than its ISO spelling.
describe("ClickHouse times", () => {
  const ch = "2026-10-16 12:00:00.123456789";
  const iso = "2026-10-16T12:00:00.123Z";
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });
  it("reads a ClickHouse time as the UTC instant it names, in Los Angeles too", () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-16T14:00:00Z"));
    expect(relativeTime(ch)).toBe("1h ago");
    expect(timeOfDay(ch)).toBe(timeOfDay(iso));
    expect(timeOfDay(ch)).not.toBe("");
    expect(absoluteTime(ch)).toBe(absoluteTime(iso));
    expect(absoluteTime(ch)).not.toBe(ch);
  });
});

describe("sentenceCase", () => {
  it("capitalises the first letter of a wire value and keeps the rest", () => {
    expect(sentenceCase("assistant")).toBe("Assistant");
    expect(sentenceCase("public")).toBe("Public");
    expect(sentenceCase("")).toBe("");
  });
});

// The server's tombstoned_at is a ClickHouse time with no zone, in UTC. Read as local time
// west of UTC, a row deleted 30 hours ago would be 23 hours old and keep all 14 days.
describe("daysLeftToRestore", () => {
  const now = Date.parse("2026-10-18T12:00:00Z");
  afterEach(() => vi.unstubAllEnvs());

  it("reads a zoneless ClickHouse time as UTC, in Los Angeles too", () => {
    expect(daysLeftToRestore("2026-10-17 06:00:00", now)).toBe(13);
    vi.stubEnv("TZ", "America/Los_Angeles");
    expect(daysLeftToRestore("2026-10-17 06:00:00", now)).toBe(13);
  });

  it("counts whole days down from 14 and stops at 0; no time is null", () => {
    expect(daysLeftToRestore("2026-10-18 11:00:00", now)).toBe(14);
    expect(daysLeftToRestore("2026-10-04 12:00:00", now)).toBe(0);
    expect(daysLeftToRestore("2026-09-01 00:00:00", now)).toBe(0);
    expect(daysLeftToRestore(null, now)).toBeNull();
    expect(daysLeftToRestore("not a time", now)).toBeNull();
    // A deletion stamped a second ahead of this clock is still day one, never 15.
    expect(daysLeftToRestore("2026-10-18 12:00:01", Date.parse("2026-10-18T12:00:00Z"))).toBe(14);
  });
});

describe("restoreText", () => {
  afterEach(() => vi.useRealTimers());

  it("says the days left, one day in the singular, and the grace window for no time", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(Date.parse("2026-10-18T12:00:00Z"));
    expect(restoreText("2026-10-05 11:00:00")).toBe("1 day left to restore");
    expect(restoreText("2026-10-17 06:00:00")).toBe("13 days left to restore");
    expect(restoreText(null)).toBe("in grace window");
  });
});
