import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { axeViolations } from "../test/axe";
import { MemoryTabs } from "./MemoryTabs";
import { renderHook } from "../test/rtl";
import { useMemoriesSection } from "./MemoryTabs";

describe("MemoryTabs", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("marks the current tab and switches route on a plain click only", async () => {
    const setView = vi.fn();
    useStore.setState({ setView });
    const { container } = render(<MemoryTabs active="memories" />);
    const memories = screen.getByRole("tab", { name: "Memories" });
    const recall = screen.getByRole("tab", { name: "Recall" });
    expect(memories).toHaveAttribute("aria-selected", "true");
    expect(memories).toHaveAttribute("aria-current", "page");
    expect(recall).toHaveAttribute("href");

    fireEvent.mouseDown(recall, { button: 0, metaKey: true });
    expect(setView).not.toHaveBeenCalled();
    fireEvent.mouseDown(recall, { button: 0 });
    expect(setView).toHaveBeenCalledWith("recall");
    expect(await axeViolations(container)).toEqual([]);
  });

  // The usage audit and the mentor read the whole namespace; they sit in a third tab, so
  // the list starts under the toolbar.
  it("has an Insights tab, the Memories page with ?section=insights", () => {
    const navigate = vi.fn();
    useStore.setState({ navigate });
    render(<MemoryTabs active="memories" />);
    const insights = screen.getByRole("tab", { name: "Insights" });
    expect(insights).toHaveAttribute("href", "/memories?section=insights");
    fireEvent.mouseDown(insights, { button: 0 });
    expect(navigate).toHaveBeenCalledWith("memories", { query: { section: "insights" } });
  });

  it("goes back to the list from Insights without the section", () => {
    const navigate = vi.fn();
    useStore.setState({ navigate });
    render(<MemoryTabs active="insights" />);
    expect(screen.getByRole("tab", { name: "Insights" })).toHaveAttribute("aria-selected", "true");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Memories" }), { button: 0 });
    expect(navigate).toHaveBeenCalledWith("memories", { query: {} });
  });

  it("reads the section from the URL, again after Back", () => {
    window.history.replaceState(null, "", "/memories?section=insights");
    const { result } = renderHook(() => useMemoriesSection());
    expect(result.current).toBe("insights");
    act(() => {
      window.history.replaceState(null, "", "/memories");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(result.current).toBe("memories");
  });
});
