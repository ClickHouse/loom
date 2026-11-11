import { describe, expect, it } from "vitest";
import { clip, exact, quoted } from "./AskAboutThis";

// The names "Ask about this" puts in the assistant's context line: one line, cut, quoted.

describe("clip", () => {
  it("keeps a short value as it is", () => {
    expect(clip("user/pref/lang")).toBe("user/pref/lang");
  });

  it("cuts a long value to its length, the last character an ellipsis", () => {
    const cut = clip("x".repeat(300), 200);
    expect(cut).toHaveLength(200);
    expect(cut.endsWith("x…")).toBe(true);
  });

  it("puts a multi-line value on one line", () => {
    expect(clip("first line\n\n  second\tline  ")).toBe("first line second line");
  });

  it("never splits a character outside the BMP at the cut", () => {
    // 8 letters then emoji (two UTF-16 units each): a cut at 10 keeps 9 code points and the
    // ellipsis, where a cut by UTF-16 unit would leave half an emoji.
    expect(clip(`${"a".repeat(8)}😀😀bc`, 10)).toBe(`${"a".repeat(8)}😀…`);
    expect(Array.from(clip("😀".repeat(50), 10))).toHaveLength(10);
    expect(clip("😀".repeat(50), 10)).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
  });
});

describe("quoted", () => {
  it("writes the value as a JSON string, its quotes escaped and its line breaks gone", () => {
    expect(quoted('Say "hi"\nthen stop')).toBe('"Say \\"hi\\" then stop"');
  });
});

describe("exact", () => {
  it("writes an identifier as a JSON string, never cut or respaced", () => {
    const long = `k/${"x".repeat(300)}`;
    expect(exact(long)).toBe(JSON.stringify(long));
    expect(exact("a\tb  c")).toBe('"a\\tb  c"');
  });
});
