import { act, render, screen } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Loading, LoadingSkeleton } from "./Loading";
import { Skeleton } from "../Skeleton";

describe("Loading", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // A fast answer never flashes a loader; the status region is there from the start, so
  // the label is announced when it appears.
  it("shows Click UI's loader and its label only after a short wait", () => {
    render(<Loading label="Loading memories" />);
    const status = screen.getByRole("status");
    expect(status).toBeEmptyDOMElement();
    act(() => vi.advanceTimersByTime(300));
    expect(status).toHaveTextContent("Loading memories");
    expect(status.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("LoadingSkeleton", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // The placeholder holds the content's place from the start (no layout shift), shows
  // after the same wait, and is read as one label, not as empty boxes.
  it("keeps its space, reveals the placeholder after the wait, and is read as its label", () => {
    render(
      <LoadingSkeleton label="Loading runs">
        <Skeleton className="h-4" />
      </LoadingSkeleton>,
    );
    const status = screen.getByRole("status", { name: "Loading runs" });
    expect(status).toHaveAttribute("aria-busy", "true");
    const shapes = status.querySelector("[aria-hidden='true']");
    expect(shapes).toHaveClass("opacity-0");
    expect(status).toHaveTextContent(/^$/);
    act(() => vi.advanceTimersByTime(300));
    expect(shapes).not.toHaveClass("opacity-0");
    // The live region's text, not its label, is what a screen reader announces.
    expect(status).toHaveTextContent("Loading runs");
  });
});
