import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CopyButton } from "./CopyButton";

afterEach(cleanup);

describe("CopyButton", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("copies and answers in place with a tick, then returns to its label", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CopyButton text={() => "the-token"} label="Copy config" />);
    const btn = screen.getByRole("button", { name: "Copy config" });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(writeText).toHaveBeenCalledWith("the-token");
    expect(btn.textContent).toContain("Copied");
    await act(async () => {
      vi.advanceTimersByTime(2100);
    });
    expect(btn.textContent).toContain("Copy config");
  });

  it("says so when the clipboard refuses", async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    render(<CopyButton text="x" label="Copy" />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    });
    expect(screen.getByRole("button", { name: "Copy" }).textContent).toContain("Copy failed");
  });
});
