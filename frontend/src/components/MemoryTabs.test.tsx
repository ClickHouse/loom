import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { axeViolations } from "../test/axe";
import { MemoryTabs } from "./MemoryTabs";

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
});
