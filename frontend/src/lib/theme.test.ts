import { afterEach, describe, expect, it, vi } from "vitest";
import { applyTheme, setTheme, storedTheme } from "./theme";

describe("theme", () => {
  afterEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.theme;
    vi.restoreAllMocks();
  });

  it("defaults to dark with nothing stored", () => {
    expect(storedTheme()).toBe("dark");
  });

  it("remembers the light theme and marks the root element", () => {
    setTheme("light");
    expect(storedTheme()).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("falls back to dark when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(storedTheme()).toBe("dark");
  });

  it("still switches the page when storage cannot be written", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    setTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
