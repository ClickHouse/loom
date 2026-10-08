/** `errorFromResponse` picks the class by HTTP status, as the Python SDK's `from_response` does for a code it does not register. */
import { describe, expect, it } from "vitest";
import {
  AuthError,
  BackendUnavailable,
  errorFromResponse,
  IdempotentConflict,
  LoomError,
  NotFound,
  PreconditionFailed,
  RateLimited,
  ValidationError,
} from "./errors.js";

describe("errorFromResponse", () => {
  it.each([
    [404, "NOT_FOUND", NotFound],
    [403, "FORBIDDEN", AuthError],
    [409, "PINNED_CONFLICT", PreconditionFailed],
    [409, "KEY_HOLDS_FACT", PreconditionFailed],
    [409, "IDEMPOTENT_CONFLICT", IdempotentConflict],
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
});
