import { afterEach, describe, expect, it } from "vitest";
import { fireEvent } from "@testing-library/react";
import { keepFocusThroughLoading, labelSelectSearch, lastPressed } from "./clickUiFixes";

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

describe("keepFocusThroughLoading", () => {
  let stop = () => {};
  afterEach(() => {
    stop();
    document.body.innerHTML = "";
  });

  it("refocuses the pressed button once its loading state ends", async () => {
    stop = keepFocusThroughLoading();
    document.body.innerHTML = `<button type="button">Run audit</button><button type="button">Other</button>`;
    const run = document.querySelector("button") as HTMLButtonElement;
    run.focus();
    fireEvent.keyDown(run, { key: "Enter" });
    expect(lastPressed()).toBe(run);
    // Click UI disables a loading button, which drops its focus to the body.
    run.disabled = true;
    run.blur();
    await new Promise((r) => setTimeout(r, 0));
    run.disabled = false;
    await new Promise((r) => setTimeout(r, 0));
    expect(run).toHaveFocus();
  });

  it("leaves focus alone when the user has moved on", async () => {
    stop = keepFocusThroughLoading();
    document.body.innerHTML = `<button type="button">Run audit</button><input aria-label="Search" />`;
    const run = document.querySelector("button") as HTMLButtonElement;
    fireEvent.pointerDown(run);
    run.disabled = true;
    const search = document.querySelector("input") as HTMLInputElement;
    search.focus();
    await new Promise((r) => setTimeout(r, 0));
    run.disabled = false;
    await new Promise((r) => setTimeout(r, 0));
    expect(search).toHaveFocus();
  });
});
