import { afterEach, describe, expect, it } from "vitest";
import { createElement } from "react";
import { Select } from "@clickhouse/click-ui/Select";
import { render, screen, waitFor } from "../test/rtl";
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

// The shim finds Click UI's Select parts by their test ids; a release that renames them
// would leave the filter field unnamed again.
describe("labelSelectSearch on a real Click UI Select", () => {
  let stop = () => {};
  afterEach(() => stop());

  it("names the filter field of an open Select", async () => {
    stop = labelSelectSearch();
    render(
      createElement(
        Select,
        { label: "Kind", onSelect: () => {}, showSearch: true },
        createElement(Select.Item, { value: "a", label: "Alpha" }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /Kind/ }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Filter options" })).toBeInTheDocument());
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

  it("records nothing once stopped", () => {
    stop = keepFocusThroughLoading();
    document.body.innerHTML = `<button type="button">Run audit</button><button type="button">Other</button>`;
    const [run, other] = [...document.querySelectorAll("button")];
    fireEvent.pointerDown(run);
    stop();
    fireEvent.pointerDown(other);
    expect(lastPressed()).toBe(run);
  });

  it("keeps focus on the dialog while a button inside it loads", async () => {
    stop = keepFocusThroughLoading();
    document.body.innerHTML = `<div role="dialog" tabindex="-1"><button type="button">Load more</button></div>`;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]') as HTMLElement;
    const more = document.querySelector("button") as HTMLButtonElement;
    more.focus();
    fireEvent.keyDown(more, { key: "Enter" });
    more.disabled = true;
    await new Promise((r) => setTimeout(r, 0));
    expect(dialog).toHaveFocus();
    more.disabled = false;
    await new Promise((r) => setTimeout(r, 0));
    expect(more).toHaveFocus();
  });

  it("forgets a press once the user presses somewhere that is not a control", async () => {
    stop = keepFocusThroughLoading();
    document.body.innerHTML = `<button type="button">Run audit</button><p>text</p>`;
    const run = document.querySelector("button") as HTMLButtonElement;
    fireEvent.pointerDown(run);
    fireEvent.pointerDown(document.querySelector("p") as HTMLElement);
    expect(lastPressed()).toBeNull();
    // A later enable of the stale button does not take the focus or scroll the page to it.
    run.disabled = true;
    await new Promise((r) => setTimeout(r, 0));
    run.disabled = false;
    await new Promise((r) => setTimeout(r, 0));
    expect(run).not.toHaveFocus();
  });

  it("forgets a press once it has given the focus back", async () => {
    stop = keepFocusThroughLoading();
    document.body.innerHTML = `<button type="button">Run audit</button>`;
    const run = document.querySelector("button") as HTMLButtonElement;
    run.focus();
    fireEvent.keyDown(run, { key: "Enter" });
    // Click UI disables the loading button; the browser drops its focus to the body.
    run.blur();
    run.disabled = true;
    await new Promise((r) => setTimeout(r, 0));
    expect(document.activeElement).toBe(document.body);
    run.disabled = false;
    await new Promise((r) => setTimeout(r, 0));
    expect(run).toHaveFocus();
    expect(lastPressed()).toBeNull();
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
