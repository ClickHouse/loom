import { render, screen } from "../../../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../../test/axe";
import { Board } from "./Board";
import { buildBoard } from "./model";
import { must } from "../../../test/must";

// The board is a `grid` with roving focus — one tab stop, the arrow keys move between
// cells, ↵ selects — and every stated result (empty, unavailable, restricted) is a result
// in the box, never a blank.

const ROWS = [
  { component: "MergeTree", severity: "medium", open: 27, untested: 20, flaky: 1, bisected: 8 },
  { component: "MergeTree", severity: "low", open: 206, untested: 178, flaky: 0, bisected: 0 },
  { component: "Keeper", severity: "critical", open: 2, untested: 2, flaky: 0, bisected: 0 },
  { component: "Keeper", severity: "low", open: 40, untested: 30, flaky: 0, bisected: 3 },
];
const GAPS = [
  {
    component: "MergeTree",
    total_bugs: 191,
    bugs_without_test: 137,
    gap_ratio: 0.7173,
    risk_score: 396.7,
    trend_delta: 0.27,
    open_bugs: 59,
    critical_bugs: 141,
    snapshot_date: "2026-09-16",
  },
];

function mount(onSelect = vi.fn(), selected: { component: string; severity: string } | null = null) {
  const board = buildBoard(ROWS, GAPS);
  const r = render(
    <Board
      board={board}
      status="served"
      selected={selected}
      onSelect={onSelect}
      trackerNamespace="tracker-x"
      daysLabel="all open"
    />,
  );
  return { ...r, onSelect };
}

describe("Board", () => {
  it("renders a labelled grid of component rows and severity columns with one tab stop, and passes axe", async () => {
    const { container } = mount();
    const grid = screen.getByRole("grid", { name: /2 components, 275 open/ });
    expect(grid).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "component · open · gap, trend",
      "critical",
      "medium",
      "low",
    ]);
    expect(screen.getAllByRole("rowheader").map((h) => h.getAttribute("aria-label"))).toEqual([
      "MergeTree, 233 open, gap ratio 72%, trend worse",
      "Keeper, 42 open",
    ]);
    const cells = container.querySelectorAll<HTMLElement>("[data-cell]");
    expect(cells).toHaveLength(4);
    expect([...cells].filter((c) => c.tabIndex === 0)).toHaveLength(1);
    expect(
      screen.getByRole("gridcell", { name: "MergeTree medium: 27 open, 20 untested, 1 flaky, 8 bisected" }),
    ).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("roves focus with the arrow keys and selects with Enter", async () => {
    const user = userEvent.setup();
    const { container, onSelect } = mount();
    const cells = () => [...container.querySelectorAll<HTMLElement>("[data-cell]")];
    const first = must(
      cells().find((c) => c.tabIndex === 0),
      "tabbable cell",
    );
    expect(first.getAttribute("aria-label")).toMatch(/^MergeTree medium/);
    first.focus();
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement?.getAttribute("aria-label")).toMatch(/^MergeTree low/);
    expect(cells().filter((c) => c.tabIndex === 0)).toHaveLength(1);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement?.getAttribute("aria-label")).toMatch(/^Keeper low/);
    await user.keyboard("{ArrowLeft}");
    expect(document.activeElement?.getAttribute("aria-label")).toMatch(/^Keeper critical/);
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith({ component: "Keeper", severity: "critical" });
  });

  it("a click selects, a second click on the selected cell clears", async () => {
    const user = userEvent.setup();
    const { onSelect } = mount(vi.fn(), { component: "MergeTree", severity: "low" });
    await user.click(screen.getByRole("gridcell", { name: /^MergeTree low/ }));
    expect(onSelect).toHaveBeenCalledWith(null);
    await user.click(screen.getByRole("gridcell", { name: /^Keeper low/ }));
    expect(onSelect).toHaveBeenCalledWith({ component: "Keeper", severity: "low" });
    expect(screen.getByRole("gridcell", { name: /^MergeTree low/ })).toHaveAttribute("aria-selected", "true");
  });

  it("states empty, unavailable and restricted as results in the box", async () => {
    const empty = render(
      <Board
        board={buildBoard([], [])}
        status="served"
        selected={null}
        onSelect={vi.fn()}
        trackerNamespace="tracker-x"
        daysLabel="90 days"
      />,
    );
    expect(screen.getByText("No open issues in the last 90 days")).toBeInTheDocument();
    expect(await axeViolations(empty.container)).toEqual([]);
    empty.unmount();
    render(
      <Board
        board={null}
        status="tracker_unavailable"
        selected={null}
        onSelect={vi.fn()}
        trackerNamespace="tracker-x"
        daysLabel="all open"
      />,
    );
    expect(screen.getByText("The tracker is not answering")).toBeInTheDocument();
    render(
      <Board
        board={null}
        status="restricted_required"
        selected={null}
        onSelect={vi.fn()}
        trackerNamespace="tracker-x"
        daysLabel="all open"
      />,
    );
    expect(screen.getByText("Restricted — not available with this token")).toBeInTheDocument();
    render(
      <Board
        board={buildBoard([], [])}
        status="served"
        totalRows={3}
        selected={null}
        onSelect={vi.fn()}
        trackerNamespace="tracker-x"
        daysLabel="all open"
      />,
    );
    expect(screen.getByText("Every cell is filtered out")).toBeInTheDocument();
  });

  it("shows a labelled skeleton on the first load, and keeps a loaded board, busy, while a new window loads", () => {
    const props = { selected: null, onSelect: vi.fn(), trackerNamespace: "tracker-x", daysLabel: "all open" };
    const { rerender } = render(<Board board={null} status="loading" {...props} />);
    expect(screen.getByRole("status", { name: "Loading the bug board" })).toHaveAttribute("aria-busy", "true");
    rerender(<Board board={buildBoard(ROWS, GAPS)} status="loading" {...props} />);
    const grid = screen.getByRole("grid", { name: /2 components/ });
    expect(grid.closest("[aria-busy]")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("status", { name: "Loading the bug board" })).toBeNull();
  });
});
