import { afterEach, describe, expect, it } from "vitest";
import { labelSelectSearch } from "./clickUiFixes";

describe("labelSelectSearch", () => {
  let stop = () => {};
  afterEach(() => {
    stop();
    document.body.innerHTML = "";
  });

  it("names Click UI's Select filter field, its clear button and its dialog when they appear", async () => {
    stop = labelSelectSearch();
    document.body.innerHTML = `<div role="dialog"><input data-testid="select-search-input" /><button data-testid="select-search-close"></button></div>`;
    await new Promise((r) => setTimeout(r, 0));
    expect(document.querySelector("input")).toHaveAttribute("aria-label", "Filter options");
    expect(document.querySelector("button")).toHaveAttribute("aria-label", "Clear filter");
    expect(document.querySelector('[role="dialog"]')).toHaveAttribute("aria-label", "Options");
  });

  it("names a clear button added after the list opened", async () => {
    stop = labelSelectSearch();
    document.body.innerHTML = `<div role="dialog"><input data-testid="select-search-input" /></div>`;
    await new Promise((r) => setTimeout(r, 0));
    const clear = document.createElement("button");
    clear.dataset.testid = "select-search-close";
    document.querySelector('[role="dialog"]')?.append(clear);
    await new Promise((r) => setTimeout(r, 0));
    expect(clear).toHaveAttribute("aria-label", "Clear filter");
  });
});
