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
    expect(screen.getByLabelText("Search and filters")).toHaveTextContent("3 selected");
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

  // Click UI Table's header checkbox: select every row on the page, mixed while some are.
  it("leads the selection with a select-all checkbox, mixed while some rows are selected", () => {
    const onToggleAll = vi.fn();
    const { rerender } = render(
      <ListToolbar
        label="Search and filters"
        selection={{
          count: 2,
          onClear: () => {},
          actions: [],
          all: { total: 5, checked: false, onToggle: onToggleAll },
        }}
      >
        <input aria-label="Search memories" />
      </ListToolbar>,
    );
    const all = screen.getByRole("checkbox", { name: "Select all 5 on this page" });
    expect(all).toHaveAttribute("aria-checked", "mixed");
    fireEvent.click(all);
    expect(onToggleAll).toHaveBeenCalled();
    rerender(
      <ListToolbar
        label="Search and filters"
        selection={{
          count: 5,
          onClear: () => {},
          actions: [],
          all: { total: 5, checked: true, onToggle: onToggleAll },
        }}
      >
        <input aria-label="Search memories" />
      </ListToolbar>,
    );
    expect(screen.getByRole("checkbox", { name: "Select all 5 on this page" })).toHaveAttribute("aria-checked", "true");
  });

  // One status region stays mounted from the start, so its first text is said; its text is
  // the count, or the selection ("N selected") while rows are selected.
  it("says the count and the selection from one status region that stays mounted", () => {
    const bar = (count?: string, selected = 0) => (
      <ListToolbar
        label="Search and filters"
        count={count}
        selection={selected ? { count: selected, onClear: () => {}, actions: [] } : null}
      >
        <input aria-label="Search memories" />
      </ListToolbar>
    );
    const { container, rerender } = render(bar());
    const live = () => container.querySelectorAll("[aria-live]");
    const [region] = live();
    expect(region).toHaveTextContent("");
    rerender(bar("12 shown"));
    expect(region).toHaveTextContent("12 shown");
    rerender(bar("12 shown", 3));
    expect(live()).toHaveLength(1);
    expect(live()[0]).toBe(region);
    expect(region).toHaveTextContent("3 selected");
  });
});
