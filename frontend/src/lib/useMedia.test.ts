import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useMedia } from "./useMedia";

describe("useMedia", () => {
  it("follows a media query as it starts and changes to match", () => {
    let matches = false;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", (query: string) => ({
      get matches() {
        return matches;
      },
      media: query,
      addEventListener: (_: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    }));
    const { result } = renderHook(() => useMedia("(max-width: 639px)"));
    expect(result.current).toBe(false);
    matches = true;
    act(() => {
      for (const fn of listeners) fn();
    });
    expect(result.current).toBe(true);
    vi.unstubAllGlobals();
  });
});
