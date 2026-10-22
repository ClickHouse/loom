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

// jsdom has no scrolling; delayed transcript navigation still calls this method.
Element.prototype.scrollIntoView = () => {};

// React's markup warnings fail the test that caused them: a <div> inside a <p> (Click UI
// draws its icon, badge and ellipsis as divs) and a ref handed to a component that cannot
// take one (a Radix Portal tracks its child by its ref).
const MARKUP_WARNINGS = /validateDOMNesting|cannot be given refs/;
const markupWarnings: string[] = [];
const consoleError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  const text = args.map(String).join(" ");
  if (MARKUP_WARNINGS.test(text)) markupWarnings.push(text.split("\n")[0]);
  consoleError(...args);
};

afterEach(() => {
  cleanup();
  const found = markupWarnings.splice(0);
  if (found.length > 0) throw new Error(`React markup warning: ${found.join(" | ")}`);
});
