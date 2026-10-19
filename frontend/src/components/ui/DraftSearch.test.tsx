import { act, cleanup, render, screen } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DraftSearch } from "./DraftSearch";

// Fake timers: each test steps the debounce itself, and userEvent its key delay. They also
// follow the clock (shouldAdvanceTime), because Testing Library ends every userEvent call
// by waiting on a zero timeout that it steps only under jest's fake timers.
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("DraftSearch", () => {
  it('settle="enter" hands over the text on Enter and an emptied box at once, never per key', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const onSearch = vi.fn();
    render(<DraftSearch settle="enter" onSearch={onSearch} aria-label="Search" />);
    const box = screen.getByLabelText("Search");
    await user.type(box, "lane");
    expect(onSearch).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(onSearch).toHaveBeenLastCalledWith("lane");
    await user.clear(box);
    expect(onSearch).toHaveBeenLastCalledWith("");
    expect(onSearch).toHaveBeenCalledTimes(2);
  });

  it("settle={ debounceMs } hands over the text once the keys pause, and ignores Enter", async () => {
    const user = userEvent.setup({ delay: null, advanceTimers: vi.advanceTimersByTime });
    const onSearch = vi.fn();
    render(<DraftSearch settle={{ debounceMs: 50 }} onSearch={onSearch} aria-label="Search" />);
    await user.type(screen.getByLabelText("Search"), "lane{Enter}");
    expect(onSearch).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(50));
    expect(onSearch).toHaveBeenCalledWith("lane");
    act(() => vi.advanceTimersByTime(500));
    expect(onSearch).toHaveBeenCalledTimes(1);
  });

  it("settle={ debounceMs } hands over nothing on mount", () => {
    const onSearch = vi.fn();
    render(<DraftSearch settle={{ debounceMs: 20 }} onSearch={onSearch} aria-label="Search" />);
    act(() => vi.advanceTimersByTime(500));
    expect(onSearch).not.toHaveBeenCalled();
  });
});
