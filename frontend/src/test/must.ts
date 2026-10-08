import { expect } from "vitest";

/**
 * Assert a test value is present and hand it back narrowed. A missing value
 * fails as a named assertion (`<what> is null`) instead of a TypeError on the
 * next property access, which is what a `!` assertion would give.
 */
export function must<T>(value: T, what: string): NonNullable<T> {
  if (value === null || value === undefined) {
    expect.unreachable(`${what} is ${value === null ? "null" : "undefined"}`);
  }
  return value;
}
