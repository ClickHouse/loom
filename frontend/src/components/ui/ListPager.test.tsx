import { fireEvent, render, screen } from "../../test/rtl";
import { describe, expect, it, vi } from "vitest";
import { ListPager, usePageSize } from "./ListPager";
import { renderHook, act } from "@testing-library/react";

// Under a list: Click UI's Pagination as its full bar, the range on the left, the pages and
// the page size on the right. A page change takes the reader to the top of the list, focus
// included, and says which page it is.
describe("ListPager", () => {
  it("says the range, moves to the next page, and brings the list's top into focus", () => {
    const onPage = vi.fn();
    render(
      <>
        <h2 id="list-top" tabIndex={-1}>
          Memories
        </h2>
        <ListPager
          label="Memories pages"
          page={0}
          pageSize={50}
          total={1234}
          shown={50}
          onPage={onPage}
          topId="list-top"
        />
      </>,
    );
    const nav = screen.getByRole("navigation", { name: "Memories pages" });
    expect(nav).toHaveTextContent("1–50 of 1,234");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onPage).toHaveBeenCalledWith(1);
    expect(screen.getByRole("heading", { name: "Memories" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 25");
  });

  // Click UI names its arrows after their icons ("chevron-left") and its page field after
  // the number in it.
  it("names its arrows and its page field", () => {
    render(<ListPager label="Memories pages" page={1} pageSize={50} total={500} shown={50} onPage={() => {}} />);
    expect(screen.getByRole("button", { name: "Previous page" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Page" })).toHaveValue(2);
  });

  it("without a total, stops at a short page", () => {
    render(<ListPager label="Thread pages" page={1} pageSize={30} shown={12} onPage={() => {}} />);
    const nav = screen.getByRole("navigation", { name: "Thread pages" });
    expect(nav).toHaveTextContent("31–42");
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("renders nothing when everything fits on one page", () => {
    const { container } = render(
      <ListPager label="x" page={0} pageSize={50} total={12} shown={12} onPage={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("usePageSize", () => {
  it("remembers the size per view", () => {
    const first = renderHook(() => usePageSize("memories"));
    expect(first.result.current[0]).toBe(50);
    act(() => first.result.current[1](100));
    const again = renderHook(() => usePageSize("memories"));
    expect(again.result.current[0]).toBe(100);
    act(() => again.result.current[1](50));
  });
});
