/** A passage's place in its document, in words, for each kind of locator a format carries. */
import { describe, expect, it } from "vitest";
import { citation, formatLocator } from "./locator";

describe("formatLocator", () => {
  it("says lines for text, a key for TOML, pages, slides and a sheet's rows", () => {
    expect(formatLocator("markdown", { line_start: 10, line_end: 24 })).toBe("lines 10–24");
    expect(formatLocator("sql", { line_start: 4, line_end: 4 })).toBe("line 4");
    expect(formatLocator("toml", { line_start: 3, line_end: 5, key_path: "server.port" })).toBe("key server.port");
    expect(formatLocator("pdf", { page_start: 3, page_end: 3, char_start: 0, char_end: 90 })).toBe("page 3");
    expect(formatLocator("pdf", { page_start: 3, page_end: 5 })).toBe("pages 3–5");
    expect(formatLocator("pptx", { page_start: 2, page_end: 3 })).toBe("slides 2–3");
    expect(formatLocator("xlsx", { sheet: "Orders", row_start: 2, row_end: 40 })).toBe("Orders, rows 2–40");
    expect(formatLocator("csv", { row_start: 2, row_end: 2 })).toBe("row 2");
  });

  it("says nothing for a passage with no place (text a caller extracted)", () => {
    expect(formatLocator("", null)).toBe("");
    expect(formatLocator("markdown", {})).toBe("");
  });
});

describe("citation", () => {
  it("names the revision a passage was read from and its place", () => {
    expect(citation({ format: "pdf", revision: 3, locator: { page_start: 7 } })).toBe("revision 3, page 7");
    expect(citation({ format: "", revision: 0, locator: null })).toBe("");
    expect(citation({ format: "markdown", revision: 0, locator: { line_start: 4 } })).toBe("line 4");
    expect(citation({ format: "text", revision: 2, locator: null })).toBe("revision 2");
    expect(citation({})).toBe("");
  });
});
