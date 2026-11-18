import { describe, expect, it } from "vitest";
import { hasIdentityWhitespace } from "./whitespace.js";

describe("server identity whitespace", () => {
  it.each(["\u001c", "\u001d", "\u001e", "\u001f", "\u0085", " ", "\t", "\n", "\u00a0", "\u2007", "\u202f", "\u3000"])(
    "detects %j alone and at either edge",
    (space) => {
      expect(hasIdentityWhitespace(space)).toBe(true);
      expect(hasIdentityWhitespace(`${space}alice`)).toBe(true);
      expect(hasIdentityWhitespace(`alice${space}`)).toBe(true);
      expect(hasIdentityWhitespace(`a${space}b`)).toBe(false);
    },
  );

  it.each(["", "alice", "josé", "\ufeffalice", "alice\ufeff"])("does not flag %j", (value) => {
    expect(hasIdentityWhitespace(value)).toBe(false);
  });
});
