// Shared vitest setup: jest-dom matchers on `expect`, and an unmount after every test so
// one test's DOM never leaks into the next.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no ResizeObserver; Radix's popper and TextClamp's measurement
// both ask for one. A no-op keeps them constructible — nothing in jsdom has a size to
// observe anyway.
if (typeof globalThis.ResizeObserver === "undefined") {
  class ResizeObserverStub implements ResizeObserver {
    observe(): void {
      /* no layout in jsdom */
    }
    unobserve(): void {
      /* no layout in jsdom */
    }
    disconnect(): void {
      /* no layout in jsdom */
    }
  }
  globalThis.ResizeObserver = ResizeObserverStub;
}

afterEach(() => {
  cleanup();
});
