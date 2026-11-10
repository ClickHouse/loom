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
  isServiceSubject,
  relativeTime,
  restoreText,
  timeOfDay,
  truncate,
  sentenceCase,
} from "./format";
import { ERROR_CODE_PREFIXES, errorLine, humanizeError } from "./errorWords";

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
  const api = (o: ConstructorParameters<typeof ApiError>[0]) => humanizeError(new ApiError(o));

  it("reads a dashboard bug or an empty throw as the dashboard's fault, not its text", () => {
    const fault = "Something went wrong in the dashboard. Reload the page.";
    expect(humanizeError(null)).toBe(fault);
    expect(humanizeError("")).toBe(fault);
    expect(humanizeError(new TypeError("Cannot read properties of undefined (reading 'x')"))).toBe(fault);
    expect(humanizeError(new DOMException("the operation is insecure", "SecurityError"))).toBe(fault);
    expect(humanizeError({ code: 1 })).toBe(fault);
  });

  it("passes the parser's words through for text the person supplied", () => {
    expect(humanizeError(new SyntaxError("Unexpected token } in JSON at position 12"))).toBe(
      "Unexpected token } in JSON at position 12",
    );
  });

  it("reads a timed-out call as Loom not answering, and a cancelled one as cancelled", () => {
    expect(humanizeError(new DOMException("signal timed out", "TimeoutError"))).toBe(
      "Loom didn't answer in time. It may still finish; check before trying again.",
    );
    expect(humanizeError(new DOMException("signal is aborted without reason", "AbortError"))).toBe("Cancelled.");
  });

  it("swaps a known code prefix for its sentence and keeps the detail", () => {
    expect(humanizeError(new Error("AUTH_INVALID: token expired"))).toBe("Sign-in rejected: token expired");
    expect(humanizeError("NOT_FOUND")).toBe("Not found — it may have been deleted, or be somewhere you can't read.");
  });

  it("passes an unknown code and the dashboard's own words through unchanged", () => {
    expect(humanizeError("SOMETHING_ELSE: detail")).toBe("SOMETHING_ELSE: detail");
    expect(humanizeError("plain message")).toBe("plain message");
    expect(humanizeError(new Error("Pick a file first"))).toBe("Pick a file first");
  });

  it("names a lost connection for the browser's bare network errors", () => {
    const lost =
      "Lost connection to Loom mid-request. The work may still have committed on the server — re-check the list before retrying.";
    expect(humanizeError(new TypeError("Failed to fetch"))).toBe(lost);
    // A read lost nothing: no "may still have committed". A caller's own lead-in alone
    // keeps the warning, since most of those are writes.
    expect(humanizeError(new TypeError("Failed to fetch"), { read: true })).toBe("Lost connection to Loom.");
    expect(humanizeError(new TypeError("Failed to fetch"), { bare: true })).toBe(lost);
    expect(humanizeError("fetch failed")).toBe(lost);
    expect(humanizeError("Load failed")).toBe(lost);
    expect(humanizeError("NetworkError when attempting to fetch resource.")).toBe(lost);
  });

  describe("ApiError", () => {
    // Each row: what the server answered, and the one line the person reads.
    const rows: [string, ConstructorParameters<typeof ApiError>[0], string][] = [
      [
        "a precondition that is not an edit race names no race",
        {
          status: 409,
          code: "PRECONDITION_FAILED",
          message: "the upload expired; upload the file again",
          detail: { category: "input", retryable: false },
        },
        "Couldn't do that: the upload expired; upload the file again",
      ],
      [
        "a name clash is not 'input rejected'",
        {
          status: 409,
          code: "PINNED_CONFLICT",
          message: "key x is pinned",
          detail: { category: "input", retryable: false },
        },
        "Couldn't do that: key x is pinned",
      ],
      [
        "a missing row reads as not found",
        {
          status: 404,
          code: "SOURCE_NOT_FOUND",
          message: "no source s1",
          detail: { category: "input", retryable: false },
        },
        "Not found: no source s1",
      ],
      [
        "an operator's fix says retrying won't help",
        {
          status: 503,
          code: "GATEWAY_REQUIRED",
          message: "model calls need the inference gateway",
          detail: { category: "configuration", retryable: false },
        },
        "This needs a change to Loom's setup: model calls need the inference gateway. Trying again won't help until that's changed.",
      ],
      [
        "a rate limit says how long to wait",
        {
          status: 429,
          code: "RATE_LIMITED",
          message: "namespace 'x' exceeded its rate limit",
          detail: { category: "capacity", retryable: true, retry_after_s: 3 },
        },
        "Too many requests. namespace 'x' exceeded its rate limit. Try again in 3 s.",
      ],
      [
        "the wait comes from the Retry-After header when the body has none",
        {
          status: 503,
          code: "AUTH_UNAVAILABLE",
          message: "the grant store did not answer",
          detail: { category: "dependency", retryable: true },
          retryAfter: 2,
        },
        "A service Loom relies on didn't answer: the grant store did not answer. Try again in 2 s.",
      ],
      [
        "a refusal whose check could not run says so, not 'your role'",
        {
          status: 403,
          code: "AUTH_INSUFFICIENT",
          message: "grants could not be read",
          detail: { category: "dependency", retryable: true },
        },
        "Loom couldn't check your access just now. grants could not be read. Try again in a moment.",
      ],
      [
        "a real refusal names the role",
        {
          status: 403,
          code: "AUTH_INSUFFICIENT",
          message: "writer needed on dev/x",
          detail: { category: "auth", retryable: false },
        },
        "Your role doesn't allow this: writer needed on dev/x",
      ],
      [
        "an internal error takes no retry advice: a read failed the same way",
        { status: 500, code: "INTERNAL_ERROR", message: "", detail: { category: "internal", retryable: false } },
        "Loom hit an internal error. If it keeps happening, send the request id to your operator (INTERNAL_ERROR).",
      ],
      [
        "a busy write that may have landed says to check first",
        { status: 503, code: "OVERLOADED", message: "", detail: { category: "capacity", retryable: false } },
        "Loom is busy right now (OVERLOADED). It may have been saved; check before trying again.",
      ],
      [
        "no retry tail when the server already says it",
        {
          status: 503,
          code: "EXTRACTION_QUEUE_UNAVAILABLE",
          message: "resending can extract the messages twice",
          detail: { category: "dependency", retryable: true },
        },
        "A service Loom relies on didn't answer: resending can extract the messages twice",
      ],
      [
        "an upgrade says when it works again, and no tail",
        {
          status: 503,
          code: "SCHEMA_PENDING",
          message: "table x missing",
          detail: { category: "configuration", retryable: false },
        },
        "Loom is being upgraded and this isn't ready yet. It works again once the upgrade finishes.",
      ],
      [
        "a code with no message is kept in brackets, never as the sentence",
        { status: 422, code: "BAD_SHA", message: "", detail: { category: "input", retryable: false } },
        "Not accepted (BAD_SHA).",
      ],
      ["an unknown status names no cause", { status: 418 }, "Couldn't do that."],
      ["a proxy's 503 with no body names no timeout", { status: 503 }, "Loom didn't answer."],
      ["a 504 is a timeout", { status: 504 }, "Loom didn't answer in time."],
      [
        "a proxy's 503 with a Retry-After says when",
        { status: 503, retryAfter: 5 },
        "Loom didn't answer. Try again in 5 s.",
      ],
      [
        "a committed write whose search copy lags reads as saved, with no doubt cast",
        {
          status: 503,
          code: "PUBLICATION_PENDING",
          message: "the write is committed; do not repeat it. Its ClickHouse rows follow shortly",
          detail: { category: "dependency", retryable: false, committed: true },
        },
        "Saved. It shows up in lists and search shortly; don't send it again.",
      ],
      [
        "a reused request key names the reuse, not 'already done'",
        { status: 409, code: "IDEMPOTENT_CONFLICT", message: "Idempotency-Key reused with different body" },
        "This request key was already used for a different request: Idempotency-Key reused with different body",
      ],
      [
        "a FastAPI 422",
        { status: 422, code: "VALIDATION_FAILED", message: "namespace: field required" },
        "Not accepted: namespace: field required",
      ],
    ];
    it.each(rows)("%s", (_name, given, line) => {
      expect(api(given)).toBe(line);
    });

    it("leaves the lead-in to a caller that brings its own", () => {
      const err = new ApiError({
        status: 409,
        code: "PRECONDITION_FAILED",
        message: "the name col-x is taken in acme",
      });
      expect(humanizeError(err, { bare: true })).toBe("the name col-x is taken in acme");
      expect(humanizeError(new ApiError({ status: 404 }), { bare: true })).toBe("Not found.");
      // A code's own sentence carries the next step, so it stays.
      expect(
        humanizeError(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "admin needed on t" }), {
          bare: true,
        }),
      ).toBe("Your role doesn't allow this. Admin needed on t.");
    });

    it("keeps the retry tail when the server's words only contain the letters of 'again'", () => {
      expect(
        api({
          status: 503,
          code: "OVERLOADED",
          message: "writes against it are queued",
          detail: { category: "capacity", retryable: true },
        }),
      ).toBe("Loom is busy right now. writes against it are queued. Try again in a moment.");
    });

    it("leaves retry advice to the tail: no code sentence says to try again", () => {
      for (const [code, sentence] of Object.entries(ERROR_CODE_PREFIXES)) {
        expect(sentence, code).not.toMatch(/retry|again|resend/i);
      }
    });

    it("words the upload's refusals, for when the format list could not check the file first", () => {
      expect(api({ status: 415, code: "UNSUPPORTED_FILE", message: ".doc files are not supported" })).toBe(
        "Loom can't read this file type: .doc files are not supported",
      );
    });

    it("says a session ended, except where there is no session to sign in to", () => {
      expect(api({ status: 401, code: "AUTH_INVALID", message: "missing bearer token" })).toBe(
        "Your session ended — sign in again.",
      );
      const ended = new ApiError({ status: 401, code: "AUTH_INVALID", message: "token jti 'j1' is revoked" });
      ended.sessionEnded = true;
      expect(humanizeError(ended)).toBe("Your session ended — sign in again.");
      expect(api({ status: 401, code: "AUTH_INVALID", message: "token jti 'j1' is revoked" })).toBe(
        "Sign-in rejected: token jti 'j1' is revoked",
      );
      for (const flag of ["__LOOM_AUTH_DISABLED", "__LOOM_PROXY_AUTH"] as const) {
        window[flag] = true;
        try {
          expect(api({ status: 401, code: "AUTH_INVALID", message: "missing bearer token" })).toBe(
            "Sign-in rejected: missing bearer token",
          );
        } finally {
          delete window[flag];
        }
      }
    });

    it("exposes the request id for the support line", () => {
      expect(errorRequestId(new ApiError({ status: 500, requestId: "req-1" }))).toBe("req-1");
      expect(errorRequestId(new Error("boom"))).toBe("");
      expect(errorRequestId(null)).toBe("");
    });
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

describe("errorLine", () => {
  it("closes the reason before the request id, and words a lost read as a read", () => {
    expect(
      errorLine(
        new ApiError({ status: 409, code: "PRECONDITION_FAILED", message: "the upload expired", requestId: "r1" }),
      ),
    ).toBe("the upload expired. Request id r1.");
    expect(errorLine(new TypeError("Failed to fetch"))).toBe("Lost connection to Loom.");
  });
});
