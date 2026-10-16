import { afterEach, describe, expect, it } from "vitest";
import { labelSelectSearch } from "./clickUiFixes";

describe("labelSelectSearch", () => {
  let stop = () => {};
  afterEach(() => {
    stop();
    document.body.innerHTML = "";
  });

  it("names Click UI's Select filter field when it appears", async () => {
    stop = labelSelectSearch();
    const input = document.createElement("input");
    input.dataset.testid = "select-search-input";
    document.body.append(input);
    await new Promise((r) => setTimeout(r, 0));
    expect(input).toHaveAttribute("aria-label", "Filter options");
  });
});
