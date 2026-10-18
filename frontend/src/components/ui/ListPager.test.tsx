import { fireEvent, render, screen } from "../../test/rtl";
import { describe, expect, it, vi } from "vitest";
import { ListPager, usePageSize } from "./ListPager";
import { renderHook, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

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

  // A pager that first renders nothing and then appears (more rows read) names its arrows
  // too, not only one whose page, size or draft moved.
  it("names its arrows when it appears after rendering nothing", () => {
    const props = { label: "Memories pages", page: 0, pageSize: 50, onPage: () => {} };
    const { rerender } = render(<ListPager {...props} shown={12} />);
    expect(screen.queryByRole("navigation")).toBeNull();
    rerender(<ListPager {...props} shown={50} />);
    expect(screen.getByRole("button", { name: "Next page" })).toBeInTheDocument();
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

// Typing a page number goes there on Enter or blur and leaves focus in the
// field; one short page without a total shows no pager; the range never reads "0–30"; a page
// past the end (the last rows deleted) steps back to the last page.
describe("ListPager, typed and edge pages", () => {
  it("goes to a typed page on Enter, keeping focus in the field", () => {
    const onPage = vi.fn();
    render(<ListPager label="Memories pages" page={2} pageSize={50} total={1300} shown={50} onPage={onPage} />);
    const field = screen.getByRole("spinbutton", { name: "Page" });
    field.focus();
    fireEvent.change(field, { target: { value: "1" } });
    fireEvent.change(field, { target: { value: "15" } });
    expect(onPage).not.toHaveBeenCalled();
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onPage).toHaveBeenCalledWith(14);
    expect(field).toHaveFocus();
  });

  it("shows no pager for one short page when there is no total", () => {
    const { container } = render(<ListPager label="x" page={0} pageSize={30} shown={12} onPage={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("offers no page that starts past the server's largest offset, and says so", () => {
    const onPage = vi.fn();
    render(
      <ListPager
        label="Runs pages"
        page={0}
        pageSize={50}
        total={1_000_000}
        shown={50}
        maxOffset={100_000}
        onPage={onPage}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Runs pages" });
    expect(nav).toHaveTextContent("1–50 of 1,000,000 · pages 1–2,001 can be opened");
    const field = screen.getByRole("spinbutton", { name: "Page" });
    // Click UI's field takes no number above its last page.
    fireEvent.change(field, { target: { value: "5000" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onPage).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "2001" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onPage).toHaveBeenCalledWith(2000);
  });

  it("without a total, stops at the server's largest offset", () => {
    const onPage = vi.fn();
    render(<ListPager label="Thread pages" page={3333} pageSize={30} shown={30} maxOffset={100_000} onPage={onPage} />);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    const field = screen.getByRole("spinbutton", { name: "Page" });
    fireEvent.change(field, { target: { value: "9999" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onPage).not.toHaveBeenCalled();
  });

  it("announces the last page when the view sends the reader back to it", () => {
    const props = { label: "Thread pages", pageSize: 30, onPage: vi.fn() };
    const { rerender } = render(<ListPager {...props} page={1} shown={15} />);
    const field = screen.getByRole("spinbutton", { name: "Page" });
    fireEvent.change(field, { target: { value: "6" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(screen.getByRole("status")).toHaveTextContent("Page 6");
    rerender(<ListPager {...props} page={5} shown={15} />);
    rerender(<ListPager {...props} page={1} shown={15} total={45} />);
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 2");
  });

  it("says nothing of a page when a filter takes the list back to its first page", () => {
    const props = { label: "Runs pages", pageSize: 50, shown: 50, onPage: vi.fn() };
    const { rerender } = render(<ListPager {...props} page={0} total={1000} />);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    rerender(<ListPager {...props} page={1} total={1000} />);
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 20");
    // A filter: the first page, then the new total.
    rerender(<ListPager {...props} page={0} total={1000} />);
    rerender(<ListPager {...props} page={0} total={30} shown={30} onPageSize={() => {}} />);
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 20");
  });

  it("steps back to the last page when its page is past the end", () => {
    const onPage = vi.fn();
    render(<ListPager label="x" page={2} pageSize={50} total={98} shown={0} onPage={onPage} />);
    expect(onPage).toHaveBeenCalledWith(1);
  });
});

// With a page size picked, the choice stays reachable even when
// one page holds everything; the size select is named for what it sets, and a size change
// says where the reader now is.
describe("ListPager, page size", () => {
  it("keeps the page-size choice when one page holds everything", () => {
    render(
      <ListPager
        label="Memories pages"
        page={0}
        pageSize={100}
        total={60}
        shown={60}
        onPage={() => {}}
        onPageSize={() => {}}
      />,
    );
    expect(screen.getByRole("navigation", { name: "Memories pages" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rows per page" })).toBeInTheDocument();
  });

  it("says nothing of a page when a filter leaves one page in the same render", () => {
    const props = { label: "Users", pageSize: 50, onPage: vi.fn(), onPageSize: () => {} };
    const { rerender } = render(<ListPager {...props} page={0} total={120} shown={50} />);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    rerender(<ListPager {...props} page={1} total={120} shown={50} />);
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 3");
    rerender(<ListPager {...props} page={0} total={30} shown={30} />);
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 3");
  });

  it("says the new page size after paging, not only the page it starts again from", async () => {
    function Paged() {
      const [page, setPage] = useState(0);
      const [size, setSize] = useState(50);
      return (
        <ListPager
          label="Runs pages"
          page={page}
          pageSize={size}
          total={1000}
          shown={size}
          onPage={setPage}
          onPageSize={(n) => {
            setSize(n);
            setPage(0);
          }}
        />
      );
    }
    render(<Paged />);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByRole("status")).toHaveTextContent("Page 2 of 20");
    await userEvent.click(screen.getByRole("button", { name: "Rows per page" }));
    await userEvent.click(await screen.findByText(/^100/));
    expect(screen.getByRole("status")).toHaveTextContent("100 rows per page, page 1");
  });

  it("hides when the list fits the smallest page", () => {
    const { container } = render(
      <ListPager label="x" page={0} pageSize={50} total={12} shown={12} onPage={() => {}} onPageSize={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

// A server that answers only its newest N (a release behind): the total is a cap, not a count.
describe("ListPager, a capped total", () => {
  it("says the range of the newest N", () => {
    render(<ListPager label="Runs pages" page={0} pageSize={50} total={200} capped shown={50} onPage={() => {}} />);
    expect(screen.getByRole("navigation", { name: "Runs pages" })).toHaveTextContent("1–50 of the newest 200");
  });
});
