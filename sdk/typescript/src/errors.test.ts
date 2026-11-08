/** `errorFromResponse` picks the class by HTTP status, as the Python SDK's `from_response` does for a code it does not register. */
import { describe, expect, it } from "vitest";
import {
  AuthError,
  BackendUnavailable,
  classed,
  connectFailed,
  errorFromResponse,
  IdempotentConflict,
  IdempotentInProgress,
  LoomError,
  NETWORK_ERROR_CODE,
  NotFound,
  PreconditionFailed,
  RateLimited,
  TimeoutError,
  ValidationError,
} from "./errors.js";

describe("errorFromResponse", () => {
  it.each([
    [404, "NOT_FOUND", NotFound],
    [403, "FORBIDDEN", AuthError],
    [409, "PINNED_CONFLICT", PreconditionFailed],
    [409, "KEY_HOLDS_FACT", PreconditionFailed],
    [409, "IDEMPOTENT_CONFLICT", IdempotentConflict],
    [409, "OVERRIDE_NOT_STORED", LoomError],
    [503, "IDEMPOTENT_IN_PROGRESS", IdempotentInProgress],
    [422, "BAD_NAMESPACE", ValidationError],
    [503, "OVERLOADED", BackendUnavailable],
  ] as const)("%i %s", (status, code, cls) => {
    const e = errorFromResponse("memory.set", status, { detail: { code, message: "m" } }, "rid");
    expect(e.constructor).toBe(cls);
    expect(e.code).toBe(code);
    expect(e.status).toBe(status);
  });

  it("keeps an unmapped status generic", () => {
    const e = errorFromResponse("memory.set", 423, { detail: { code: "LOCKED", message: "held" } }, "rid");
    expect(e.constructor).toBe(LoomError);
    expect(e.code).toBe("LOCKED");
  });

  it("builds a 429's RateLimited with the wait it is given, one second by default", () => {
    const body = { detail: { code: "RATE_LIMITED", message: "slow down" } };
    const e = errorFromResponse("memory.set", 429, body, "rid", 7) as RateLimited;
    expect(e).toBeInstanceOf(RateLimited);
    expect([e.retryAfterS, e.code, e.message, e.requestId]).toEqual([7, "RATE_LIMITED", "slow down", "rid"]);
    expect((errorFromResponse("memory.set", 429, body, "rid") as RateLimited).retryAfterS).toBe(1);
  });

  it("reads category and retryable from the detail, with a fallback when it names none", () => {
    const body = { detail: { code: "LOCKED", message: "m", category: "input", retryable: true } };
    const e = errorFromResponse("memory.set", 423, body, "rid");
    expect([e.category, e.retryable]).toEqual(["input", true]);
    const proxy = errorFromResponse("memory.set", 502, { detail: "Bad Gateway" }, "rid");
    expect([proxy.category, proxy.retryable]).toEqual(["dependency", true]);
    const refused = errorFromResponse("memory.set", 403, { detail: "Forbidden" }, "rid");
    expect([refused.category, refused.retryable]).toEqual(["auth", false]);
    // A NETWORK error the transport did not class: a response cut off, which may have run.
    const cutOff = new LoomError("response cut off", { code: NETWORK_ERROR_CODE });
    expect([cutOff.category, cutOff.retryable]).toEqual(["dependency", false]);
    const config = errorFromResponse("memory.set", 503, { detail: { code: "GATEWAY_REQUIRED" } }, "rid");
    expect([config.category, config.retryable]).toEqual(["configuration", false]);
    const noCustomer = errorFromResponse("memory.set", 500, { detail: { code: "MODEL_CALL_UNATTRIBUTED" } }, "rid");
    expect([noCustomer.category, noCustomer.retryable]).toEqual(["internal", false]);
    const misuse = new LoomError("pass apiKey or tokenProvider, not both");
    expect([misuse.category, misuse.retryable]).toEqual(["input", false]);
  });

  // The server's rule by status (server/core/errors.py error_class), for a body without the fields.
  it.each([
    [400, "input", false],
    [401, "auth", false],
    [403, "auth", false],
    [404, "input", false],
    [408, "capacity", true],
    [409, "input", false],
    [422, "input", false],
    [429, "capacity", true],
    [500, "internal", false],
    [502, "dependency", true],
    [503, "dependency", true],
    [504, "dependency", true],
  ] as const)("falls back to the server rule for %i", (status, category, retryable) => {
    const e = new LoomError("m", { status });
    expect([e.category, e.retryable]).toEqual([category, retryable]);
  });

  it("classes an error raised before sending by its family", () => {
    const down = new BackendUnavailable("cannot reach the identity provider");
    expect([down.category, down.retryable]).toEqual(["dependency", true]);
    const signedOut = new AuthError("not signed in");
    expect([signedOut.category, signedOut.retryable]).toEqual(["auth", false]);
  });

  it("takes the class an SDK-raised error carries, and keeps the detail the server's", () => {
    const wait = classed(new TimeoutError("still running", { op: "knowledge.ingest_status" }), "capacity", true);
    expect([wait.category, wait.retryable]).toEqual(["capacity", true]);
    expect(wait.detail).toEqual({});
  });
});

describe("connectFailed", () => {
  it("finds a connect error on the error, its cause or an AggregateError's errors", () => {
    expect(connectFailed(new TypeError("fetch failed", { cause: { code: "ECONNREFUSED" } }))).toBe(true);
    expect(connectFailed(Object.assign(new Error("request failed"), { code: "ENOTFOUND" }))).toBe(true);
    const dualStack = Object.assign(new AggregateError([{ code: "ETIMEDOUT", syscall: "connect" }], ""), {
      code: "ETIMEDOUT",
    });
    expect(connectFailed(new TypeError("fetch failed", { cause: dualStack }))).toBe(true);
  });

  it("does not take a dropped socket, a read timeout or a bare TypeError for one", () => {
    expect(connectFailed(new TypeError("fetch failed", { cause: { code: "UND_ERR_SOCKET" } }))).toBe(false);
    expect(connectFailed(new TypeError("fetch failed", { cause: { code: "ETIMEDOUT", syscall: "read" } }))).toBe(false);
    expect(connectFailed(new TypeError("Failed to fetch"))).toBe(false);
  });
});
