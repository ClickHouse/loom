import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "../test/rtl";
import { useRowSelection } from "./useRowSelection";

const NAMES = ["one", "two", "three", "four", "five"];

function List({ names = NAMES }: { names?: string[] }) {
  const sel = useRowSelection(names);
  return (
    <>
      <input type="checkbox" aria-label="Select all on this page" checked={sel.allSelected} onChange={sel.toggleAll} />
      <p data-testid="picked">{[...sel.selected].join(",")}</p>
      {names.map((n) => (
        <input
          key={n}
          type="checkbox"
          aria-label={`Select ${n}`}
          checked={sel.selected.has(n)}
          onClick={(e) => sel.toggle(n, e.shiftKey)}
          onChange={() => {}}
        />
      ))}
    </>
  );
}

const picked = () => screen.getByTestId("picked").textContent;

describe("useRowSelection", () => {
  afterEach(cleanup);

  it("toggles one row at a time", async () => {
    render(<List />);
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Select two"));
    await user.click(screen.getByLabelText("Select four"));
    expect(picked()).toBe("two,four");
    await user.click(screen.getByLabelText("Select two"));
    expect(picked()).toBe("four");
  });

  it("selects the range from the last clicked row on a Shift-click, either direction", async () => {
    render(<List />);
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Select two"));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByLabelText("Select four"));
    await user.keyboard("{/Shift}");
    expect(picked()).toBe("two,three,four");
    await user.click(screen.getByLabelText("Select five"));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByLabelText("Select one"));
    await user.keyboard("{/Shift}");
    expect(new Set(picked()?.split(","))).toEqual(new Set(NAMES));
  });

  it("selects every row on the page, and clears them all once they are", async () => {
    render(<List />);
    const user = userEvent.setup();
    const all = screen.getByLabelText("Select all on this page");
    await user.click(screen.getByLabelText("Select two"));
    await user.click(all);
    expect(picked()).toBe(NAMES.join(","));
    expect(all).toBeChecked();
    await user.click(all);
    expect(picked()).toBe("");
  });

  it("clears the selection on Escape", async () => {
    render(<List />);
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Select two"));
    await user.click(screen.getByLabelText("Select three"));
    (document.activeElement as HTMLElement).blur();
    await user.keyboard("{Escape}");
    expect(picked()).toBe("");
  });

  it("drops rows that left the page", async () => {
    const { rerender } = render(<List />);
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Select two"));
    await user.click(screen.getByLabelText("Select four"));
    rerender(<List names={["four", "five"]} />);
    expect(picked()).toBe("four");
  });
});
