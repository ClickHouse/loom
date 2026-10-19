import { afterEach, describe, expect, it, vi } from "vitest";
import { isApplePlatform, paletteShortcut } from "./platform";

// The search shortcut is labelled the way the keyboard prints it: ⌘K on Apple, Ctrl K elsewhere.
describe("platform shortcut labels", () => {
  afterEach(() => vi.unstubAllGlobals());

  const on = (platform: string) => vi.stubGlobal("navigator", { platform });

  it("writes ⌘K on a Mac and an iPad", () => {
    on("MacIntel");
    expect(paletteShortcut()).toBe("⌘K");
    on("iPad");
    expect(isApplePlatform()).toBe(true);
  });

  it("writes Ctrl K on Windows and Linux", () => {
    on("Win32");
    expect(paletteShortcut()).toBe("Ctrl K");
    on("Linux x86_64");
    expect(isApplePlatform()).toBe(false);
  });

  it("prefers the client hint when the browser sends one", () => {
    vi.stubGlobal("navigator", { platform: "", userAgentData: { platform: "macOS" } });
    expect(paletteShortcut()).toBe("⌘K");
  });
});
