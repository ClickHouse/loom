import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DraftSearch } from "./DraftSearch";

afterEach(cleanup);

describe("DraftSearch", () => {
  it('settle="enter" hands over the text on Enter and an emptied box at once, never per key', async () => {
    const onSearch = vi.fn();
    render(<DraftSearch settle="enter" onSearch={onSearch} aria-label="Search" />);
    const box = screen.getByLabelText("Search");
    await userEvent.type(box, "lane");
    expect(onSearch).not.toHaveBeenCalled();
    await userEvent.keyboard("{Enter}");
    expect(onSearch).toHaveBeenLastCalledWith("lane");
    await userEvent.clear(box);
    expect(onSearch).toHaveBeenLastCalledWith("");
    expect(onSearch).toHaveBeenCalledTimes(2);
  });

  it("settle={ debounceMs } hands over the text once the keys pause, and ignores Enter", async () => {
    const onSearch = vi.fn();
    render(<DraftSearch settle={{ debounceMs: 50 }} onSearch={onSearch} aria-label="Search" />);
    await waitFor(() => expect(onSearch).toHaveBeenCalledWith(""));
    onSearch.mockClear();
    await userEvent.type(screen.getByLabelText("Search"), "lane{Enter}", { delay: null });
    await waitFor(() => expect(onSearch).toHaveBeenCalledWith("lane"));
    expect(onSearch).toHaveBeenCalledTimes(1);
  });
});
