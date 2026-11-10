import { describe, expect, it } from "vitest";
import { ApiError } from "./errors";
import { ERROR_CODE_PREFIXES, errorLine, humanizeError } from "./errorWords";
import { errorRequestId } from "./format";

// What the dashboard says for a failure (lib/errorWords.ts): one line that says what
// happened and what to do, and names no cause it is not sure of.

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
    // A read that timed out left nothing behind to check.
    expect(humanizeError(new DOMException("signal timed out", "TimeoutError"), { read: true })).toBe(
      "Loom didn't answer in time.",
    );
  });

  it("swaps a known code prefix for its sentence and keeps the detail", () => {
    expect(humanizeError(new Error("AUTH_INVALID: token expired"))).toBe("Sign-in rejected: token expired.");
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
    // Chrome's words when the body of an answer is cut after its headers.
    expect(humanizeError(new TypeError("network error"))).toBe(lost);
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
        "Couldn't do that: the upload expired; upload the file again.",
      ],
      [
        "a name clash is not 'input rejected'",
        {
          status: 409,
          code: "PINNED_CONFLICT",
          message: "key x is pinned",
          detail: { category: "input", retryable: false },
        },
        "Couldn't do that: key x is pinned.",
      ],
      [
        "a missing row reads as not found",
        {
          status: 404,
          code: "SOURCE_NOT_FOUND",
          message: "no source s1",
          detail: { category: "input", retryable: false },
        },
        "Not found: no source s1.",
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
        "Over a limit for now: namespace 'x' exceeded its rate limit. Try again in 3 s.",
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
        "A service Loom relies on failed: the grant store did not answer. Try again in 2 s.",
      ],
      [
        "a refusal whose check could not run says so, not 'your role'",
        {
          status: 403,
          code: "AUTH_INSUFFICIENT",
          message: "grants could not be read",
          detail: { category: "dependency", retryable: true },
        },
        "Loom couldn't check your access just now: grants could not be read. Try again in a moment.",
      ],
      [
        "a real refusal names the role",
        {
          status: 403,
          code: "AUTH_INSUFFICIENT",
          message: "writer needed on dev/x",
          detail: { category: "auth", retryable: false },
        },
        "Your role doesn't allow this: writer needed on dev/x.",
      ],
      [
        "an internal error on a write may have left the write behind",
        {
          status: 500,
          code: "INTERNAL_ERROR",
          message: "internal error; request_id=abc",
          detail: { category: "internal", retryable: false },
        },
        "Loom hit an internal error. If it keeps happening, tell your operator. Request id abc. It may have been saved; check before trying again.",
      ],
      [
        "a busy write that may have landed says to check first",
        { status: 503, code: "OVERLOADED", message: "", detail: { category: "capacity", retryable: false } },
        "Loom is busy right now. It may have been saved; check before trying again.",
      ],
      [
        "a write the server marks unsafe to resend keeps that warning though its words say retry",
        {
          status: 503,
          code: "BACKEND_UNAVAILABLE",
          message: "the columns of t cannot be read yet; retry",
          detail: { category: "dependency", retryable: false },
        },
        "A service Loom relies on failed: the columns of t cannot be read yet; retry. It may have been saved; check before trying again.",
      ],
      [
        "no retry tail when the server already says it",
        {
          status: 503,
          code: "EXTRACTION_QUEUE_UNAVAILABLE",
          message: "resending can extract the messages twice",
          detail: { category: "dependency", retryable: true },
        },
        "A service Loom relies on failed: resending can extract the messages twice.",
      ],
      [
        "a migration not applied yet keeps the server's words and says retrying won't help",
        {
          status: 503,
          code: "SCHEMA_PENDING",
          message: "table x missing",
          detail: { category: "configuration", retryable: false },
        },
        "Loom's database is not upgraded for this yet: table x missing. Trying again won't help until that's changed.",
      ],
      [
        "a code with no message is kept in brackets, never as the sentence",
        { status: 422, code: "BAD_SHA", message: "", detail: { category: "input", retryable: false } },
        "Not accepted (BAD_SHA).",
      ],
      ["an unknown status names no cause", { status: 418 }, "Couldn't do that."],
      [
        "a proxy's 503 with no body names no timeout, and warns a writer",
        { status: 503 },
        "Loom didn't answer. It may have been saved; check before trying again.",
      ],
      [
        "a 504 is a timeout",
        { status: 504 },
        "Loom didn't answer in time. It may have been saved; check before trying again.",
      ],
      [
        "a proxy's 503 on a write warns before it gives a wait",
        { status: 503, retryAfter: 5 },
        "Loom didn't answer. It may have been saved; check before trying again.",
      ],
      [
        "a committed write whose search copy lags reads as saved, with no doubt cast",
        {
          status: 503,
          code: "PUBLICATION_PENDING",
          message: "the write is committed; do not repeat it. Its ClickHouse rows follow shortly",
          detail: { category: "dependency", retryable: false, committed: true },
        },
        "Done, but lists and search have not caught up yet. Don't send it once more; reload in a moment to see the result.",
      ],
      [
        "a request still running names that, not a load problem",
        {
          status: 503,
          code: "IDEMPOTENT_IN_PROGRESS",
          message: "a request with this idempotency key is still running; retry once it answers",
          detail: { category: "capacity", retryable: true },
        },
        "Not done yet: a request with this idempotency key is still running; retry once it answers.",
      ],
      [
        "a call raced by the same person's sign-in in another tab is safe to send once more",
        { status: 401, code: "SIGNED_IN_ELSEWHERE" },
        "You signed in afresh in another tab. Nothing was lost. It is safe to send it once more.",
      ],
      [
        "a reused request key names the reuse, not 'already done'",
        { status: 409, code: "IDEMPOTENT_CONFLICT", message: "Idempotency-Key reused with different body" },
        "This request key was already used for a different request: Idempotency-Key reused with different body.",
      ],
      [
        "a FastAPI 422",
        { status: 422, code: "VALIDATION_FAILED", message: "namespace: field required" },
        "Not accepted: namespace: field required.",
      ],
    ];
    it.each(rows)("%s", (_name, given, line) => {
      expect(api(given)).toBe(line);
    });

    it("keeps a name at the start of the server's words as written", () => {
      expect(
        humanizeError(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "acme is the only admin" }), {
          bare: true,
        }),
      ).toBe("Your role doesn't allow this — acme is the only admin.");
    });

    it("keeps the category's words after a caller's lead-in for a code with no message", () => {
      expect(
        humanizeError(
          new ApiError({ status: 503, code: "SOME_NEW_CODE", detail: { category: "capacity", retryable: true } }),
          { bare: true },
        ),
      ).toBe("Loom is busy (SOME_NEW_CODE). Try again in a moment.");
    });

    it("names only the status after a caller's lead-in when nothing else was said", () => {
      expect(humanizeError(new ApiError({ status: 418 }), { bare: true })).toBe("Loom answered with status 418.");
    });

    it("says an internal error's request id once, in the line, wherever it is shown", () => {
      const err = new ApiError({
        status: 500,
        code: "INTERNAL_ERROR",
        message: "internal error; request_id=r9",
        requestId: "r9",
        detail: { category: "internal", retryable: false },
      });
      const line = "Loom hit an internal error. If it keeps happening, tell your operator. Request id r9.";
      expect(humanizeError(err, { read: true })).toBe(line);
      expect(errorLine(err, { read: true })).toBe(line);
    });

    it("takes an internal error's id from the server's words when the header is missing", () => {
      const err = new ApiError({
        status: 500,
        code: "INTERNAL_ERROR",
        message: "internal error; request_id=r7",
        detail: { category: "internal", retryable: false },
      });
      expect(humanizeError(err, { read: true })).toBe(
        "Loom hit an internal error. If it keeps happening, tell your operator. Request id r7.",
      );
    });

    it("tells a read what a 5xx leaves to do, with no write warning", () => {
      expect(humanizeError(new ApiError({ status: 503, retryAfter: 5 }), { read: true })).toBe(
        "Loom didn't answer. Try again in 5 s.",
      );
      expect(humanizeError(new ApiError({ status: 504 }), { read: true })).toBe("Loom didn't answer in time.");
      expect(
        humanizeError(
          new ApiError({ status: 500, code: "INTERNAL_ERROR", detail: { category: "internal", retryable: false } }),
          { read: true },
        ),
      ).toBe("Loom hit an internal error. If it keeps happening, tell your operator.");
    });

    it("tells a read, not a writer, that the call can go again when the server marked it unsafe", () => {
      const busy = new ApiError({
        status: 503,
        code: "OVERLOADED",
        detail: { category: "capacity", retryable: false },
      });
      expect(humanizeError(busy, { read: true })).toBe("Loom is busy right now. Try again in a moment.");
      expect(humanizeError(busy)).toBe("Loom is busy right now. It may have been saved; check before trying again.");
    });

    it("an ok answer whose body is unreadable may have been a committed write", () => {
      const err = new ApiError({ status: 200, code: "ANSWER_UNREADABLE" });
      expect(humanizeError(err)).toBe(
        "Loom's answer could not be read; a proxy in front of it may have answered instead. It may have been saved; check before trying again.",
      );
      expect(humanizeError(err, { read: true })).toBe(
        "Loom's answer could not be read; a proxy in front of it may have answered instead. Try again in a moment.",
      );
    });

    it("leaves the lead-in to a caller that brings its own", () => {
      const err = new ApiError({
        status: 409,
        code: "PRECONDITION_FAILED",
        message: "the name col-x is taken in acme.",
      });
      expect(humanizeError(err, { bare: true })).toBe("the name col-x is taken in acme.");
      expect(humanizeError(new ApiError({ status: 404 }), { bare: true })).toBe("Not found.");
      // A code's own sentence carries the next step, so it stays.
      expect(
        humanizeError(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "admin needed on t" }), {
          bare: true,
        }),
      ).toBe("Your role doesn't allow this — admin needed on t.");
    });

    it("keeps the retry tail when the server's words only contain the letters of 'again'", () => {
      expect(
        api({
          status: 503,
          code: "OVERLOADED",
          message: "writes against it are queued",
          detail: { category: "capacity", retryable: true },
        }),
      ).toBe("Loom is busy right now: writes against it are queued. Try again in a moment.");
    });

    it("leaves retry advice to the tail: no code sentence says to try again", () => {
      for (const [code, sentence] of Object.entries(ERROR_CODE_PREFIXES)) {
        expect(sentence, code).not.toMatch(/retry|again|resend/i);
      }
    });

    it("words the upload's refusals, for when the format list could not check the file first", () => {
      expect(api({ status: 415, code: "UNSUPPORTED_FILE", message: ".doc files are not supported" })).toBe(
        "Loom can't read this file type: .doc files are not supported.",
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
        "Sign-in rejected: token jti 'j1' is revoked.",
      );
      for (const flag of ["__LOOM_AUTH_DISABLED", "__LOOM_PROXY_AUTH"] as const) {
        window[flag] = true;
        try {
          expect(api({ status: 401, code: "AUTH_INVALID", message: "missing bearer token" })).toBe(
            "Sign-in rejected: missing bearer token.",
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

describe("errorLine", () => {
  it("closes the reason before the request id, and words a lost read as a read", () => {
    expect(
      errorLine(
        new ApiError({ status: 409, code: "PRECONDITION_FAILED", message: "the upload expired", requestId: "r1" }),
      ),
    ).toBe("the upload expired. Request id r1.");
    expect(errorLine(new TypeError("Failed to fetch"), { read: true })).toBe("Lost connection to Loom.");
    // Without `read` the line keeps the warning that a write may have landed.
    expect(errorLine(new TypeError("Failed to fetch"))).toMatch(/may still have committed/);
  });
});
