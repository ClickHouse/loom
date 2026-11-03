import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./errors";
import {
  _formatterCacheSize,
  absoluteTime,
  dayLabel,
  daysLeftToRestore,
  displaySubject,
  errorRequestId,
  formatBytesShort,
  refreshDetail,
  humanizeError,
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

describe("humanizeError", () => {
  it("names the unknown", () => {
    expect(humanizeError(null)).toBe("Unknown error");
    expect(humanizeError("")).toBe("Unknown error");
  });

  it("swaps a known code prefix for its sentence and keeps the detail", () => {
    expect(humanizeError(new Error("AUTH_INVALID: token expired"))).toBe("Sign-in rejected: token expired");
    expect(humanizeError("VALIDATION: seed must not be empty")).toBe("Input rejected: seed must not be empty");
  });

  // An ApiError maps by code, then status, then the server's message — and the raw
  // response body never reaches the screen.
  describe("ApiError", () => {
    it("maps the server code first", () => {
      expect(
        humanizeError(
          new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "principal 'v' cannot access dev/x" }),
        ),
      ).toBe("You don't have permission for this on this namespace. principal 'v' cannot access dev/x");
      expect(
        humanizeError(new ApiError({ status: 401, code: "AUTH_INVALID", message: "Invalid email or password." })),
      ).toBe("Sign-in rejected: Invalid email or password.");
      // A session's token that ran out, or a read sent after another tab ended the session.
      expect(
        humanizeError(
          new ApiError({ status: 401, code: "AUTH_INVALID", message: "token expired: Signature has expired" }),
        ),
      ).toBe("Your session ended — sign in again.");
      expect(humanizeError(new ApiError({ status: 401, code: "AUTH_INVALID", message: "missing bearer token" }))).toBe(
        "Your session ended — sign in again.",
      );
      expect(
        humanizeError(
          new ApiError({ status: 401, code: "AUTH_INVALID", message: "the sign-in session of this token has ended" }),
        ),
      ).toBe("Your session ended — sign in again.");
      expect(
        humanizeError(new ApiError({ status: 401, code: "AUTH_INVALID", message: "token jti 'j1' is revoked" })),
      ).toBe("Your session ended — sign in again.");
      // With auth off, or behind a proxy, there is no session to sign in to.
      for (const flag of ["__LOOM_AUTH_DISABLED", "__LOOM_PROXY_AUTH"] as const) {
        window[flag] = true;
        try {
          expect(
            humanizeError(new ApiError({ status: 401, code: "AUTH_INVALID", message: "missing bearer token" })),
          ).toBe("Sign-in rejected: missing bearer token");
        } finally {
          delete window[flag];
        }
      }
      expect(
        humanizeError(
          new ApiError({ status: 429, code: "RATE_LIMITED", message: "namespace 'x' exceeded memory.set rate limit" }),
        ),
      ).toBe("Slow down — too many requests recently. namespace 'x' exceeded memory.set rate limit");
      expect(
        humanizeError(new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "namespace: field required" })),
      ).toBe("Input rejected: namespace: field required");
    });

    it("falls back to the HTTP status for an unknown code", () => {
      expect(humanizeError(new ApiError({ status: 403, code: "SOME_NEW_CODE", message: "nope" }))).toBe(
        "You don't have permission for this. nope",
      );
      expect(humanizeError(new ApiError({ status: 404, message: "Not Found" }))).toBe("Couldn't find it. Not Found");
      expect(humanizeError(new ApiError({ status: 500 }))).toBe("Loom hit a server error.");
      expect(humanizeError(new ApiError({ status: 503, code: "", message: "" }))).toBe(
        "Loom is unavailable right now — try again in a moment.",
      );
      expect(humanizeError(new ApiError({ status: 418 }))).toBe("Request failed (HTTP 418).");
    });

    it("names the code when the server sent one without a message", () => {
      expect(humanizeError(new ApiError({ status: 409, code: "PINNED_CONFLICT", message: "" }))).toBe(
        "Conflict — something changed underneath this request: PINNED_CONFLICT",
      );
    });

    it("words a typed error from its code and message", () => {
      const denied = new ApiError({
        status: 403,
        code: "AUTH_INSUFFICIENT",
        message: "principal cannot access dev/x",
      });
      expect(humanizeError(denied)).toBe(
        "You don't have permission for this on this namespace. principal cannot access dev/x",
      );
      expect(
        humanizeError(new ApiError({ status: 401, code: "AUTH_INVALID", message: "Invalid email or password." })),
      ).toBe("Sign-in rejected: Invalid email or password.");
    });

    it("exposes the request id for the support line", () => {
      expect(errorRequestId(new ApiError({ status: 500, requestId: "req-1" }))).toBe("req-1");
      expect(errorRequestId(new Error("boom"))).toBe("");
      expect(errorRequestId(null)).toBe("");
    });
  });

  it("returns the sentence alone when the code has no detail", () => {
    expect(humanizeError("NOT_FOUND")).toBe("Couldn't find it.");
  });

  it("names a lost connection for the browser's bare network errors", () => {
    const lost =
      "Lost connection to Loom mid-request. The work may still have committed on the server — re-check the list before retrying.";
    expect(humanizeError(new TypeError("Failed to fetch"))).toBe(lost);
    expect(humanizeError("fetch failed")).toBe(lost);
    expect(humanizeError("Load failed")).toBe(lost);
    expect(humanizeError("NetworkError when attempting to fetch resource.")).toBe(lost);
  });

  it("passes an unknown code and plain text through unchanged", () => {
    expect(humanizeError("SOMETHING_ELSE: detail")).toBe("SOMETHING_ELSE: detail");
    expect(humanizeError("plain message")).toBe("plain message");
    expect(humanizeError("inline-fetch failed (404)")).toBe("inline-fetch failed (404)");
  });
});

describe("truncate", () => {
  it("returns short and exact-length strings unchanged", () => {
    expect(truncate("abc", 5)).toBe("abc");
    expect(truncate("abcde", 5)).toBe("abcde");
    expect(truncate("", 3)).toBe("");
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
