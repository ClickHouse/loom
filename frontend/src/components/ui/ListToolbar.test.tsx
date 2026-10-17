import { fireEvent, render, screen } from "../../test/rtl";
import { describe, expect, it, vi } from "vitest";
import { ListToolbar } from "./ListToolbar";

// A list page's one sticky bar: the search and filters with the count, or, while rows are
// selected, the selection and its actions in the same slot, so the bar never grows over
// the rows beneath it.
describe("ListToolbar", () => {
  it("holds the search and the count", () => {
    render(
      <ListToolbar label="Search and filters" count="1–50 of 1,234">
        <input aria-label="Search memories" />
      </ListToolbar>,
    );
    const bar = screen.getByLabelText("Search and filters");
    expect(bar).toContainElement(screen.getByLabelText("Search memories"));
    expect(bar).toHaveTextContent("1–50 of 1,234");
  });

  it("swaps to the selection and its actions while rows are selected", () => {
    const onClear = vi.fn();
    const onDelete = vi.fn();
    render(
      <ListToolbar
        label="Search and filters"
        count="1–50 of 1,234"
        selection={{
          count: 3,
          onClear,
          actions: [{ label: "Delete", icon: "trash", danger: true, onClick: onDelete }],
        }}
      >
        <input aria-label="Search memories" />
      </ListToolbar>,
    );
    expect(screen.queryByLabelText("Search memories")).toBeNull();
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(onClear).toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    expect(onDelete).toHaveBeenCalled();
  });

  // The Clear button leaves with the selection; focus goes to the search the bar brings back
  // instead of falling to the page (WCAG 2.4.3).
  it("puts focus on the search once the selection empties", () => {
    const sel = (count: number) => ({ count, onClear: () => {}, actions: [] });
    const { rerender } = render(
      <ListToolbar label="Search and filters" selection={sel(2)}>
        <input aria-label="Search memories" />
      </ListToolbar>,
    );
    screen.getByRole("button", { name: "Clear selection" }).focus();
    rerender(
      <ListToolbar label="Search and filters" selection={sel(0)}>
        <input aria-label="Search memories" />
      </ListToolbar>,
    );
    expect(screen.getByLabelText("Search memories")).toHaveFocus();
  });
});
