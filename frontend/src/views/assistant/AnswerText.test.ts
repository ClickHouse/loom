// citationNumbers numbers only the markers the answer draws.
import { describe, expect, it } from "vitest";
import { citationNumbers } from "./AnswerText";

describe("citationNumbers", () => {
  it("skips a marker in a table cell past the header's count, which is not drawn", () => {
    const text = [
      "| Key | Owner |",
      "| --- | --- |",
      "| billing | ada [memory:billing/owner@v1] | [memory:billing/extra@v2] |",
      "",
      "Invoices go out on Fridays [memory:billing/invoices@v3].",
    ].join("\n");
    expect([...citationNumbers(text)]).toEqual([
      ["[memory:billing/owner@v1]", 1],
      ["[memory:billing/invoices@v3]", 2],
    ]);
  });
});
