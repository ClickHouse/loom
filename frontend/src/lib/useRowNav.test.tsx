import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "../test/rtl";
import { ListRow } from "../components/ui/ListRow";
import { useRowNav } from "./useRowNav";

function List({ names = ["one", "two", "three"], onOpen = (_: string) => {}, controls = false }) {
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const listRef = useRowNav();
  return (
    <>
      <button type="button">Before</button>
      <div ref={listRef}>
        {names.map((n) => (
          <ListRow
            key={n}
            leading={
              <input
                type="checkbox"
                aria-label={`Select ${n}`}
                checked={checked.has(n)}
                onChange={() =>
                  setChecked((s) => {
                    const next = new Set(s);
                    next.has(n) ? next.delete(n) : next.add(n);
                    return next;
                  })
                }
              />
            }
          >
            <button type="button" data-row-open="" onClick={() => onOpen(n)}>
              {n}
            </button>
            {controls && (
              <>
                <button type="button">{`tag ${n}`}</button>
                <button type="button">{`Actions for ${n}`}</button>
              </>
            )}
          </ListRow>
        ))}
      </div>
      <button type="button">After</button>
    </>
  );
}

const open = (n: string) => screen.getByRole("button", { name: n });

describe("useRowNav", () => {
  afterEach(cleanup);

  it("is one tab stop that follows the focused row", async () => {
    render(<List />);
    expect(open("one")).toHaveAttribute("tabindex", "0");
    expect(open("two")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByLabelText("Select one")).toHaveAttribute("tabindex", "-1");
    const user = userEvent.setup();
    await user.tab();
    await user.tab();
    expect(open("one")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(open("two")).toHaveFocus();
    expect(open("two")).toHaveAttribute("tabindex", "0");
    expect(open("one")).toHaveAttribute("tabindex", "-1");
    // Tab leaves the list, Shift+Tab comes back to the row it left from.
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Before" })).toHaveFocus();
    await user.tab();
    expect(open("two")).toHaveFocus();
  });

  it("moves with the arrows, and jumps with Home/End", async () => {
    render(<List />);
    const user = userEvent.setup();
    await user.keyboard("{ArrowDown}");
    expect(open("one")).toHaveFocus();
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(open("three")).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(open("three")).toHaveFocus();
    await user.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
    expect(open("one")).toHaveFocus();
    await user.keyboard("{End}");
    expect(open("three")).toHaveFocus();
    await user.keyboard("{Home}");
    expect(open("one")).toHaveFocus();
  });

  // The ring goes round the whole row, so the row is what must come into view.
  it("scrolls the row into view, not its open button", async () => {
    // jsdom has no scrollIntoView: each element records its own call.
    const scrolled: Element[] = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this);
    };
    try {
      render(<List />);
      const user = userEvent.setup();
      await user.keyboard("{ArrowDown}{ArrowDown}");
      expect(open("two")).toHaveFocus();
      expect(scrolled).toEqual([open("one").closest(".list-row"), open("two").closest(".list-row")]);
    } finally {
      // biome-ignore lint/performance/noDelete: the property must be gone again, not undefined
      delete (Element.prototype as Partial<Element>).scrollIntoView;
    }
  });

  it("leaves the arrows to a control outside the list", async () => {
    render(<List />);
    screen.getByRole("button", { name: "Before" }).focus();
    await userEvent.keyboard("{ArrowDown}{End}");
    expect(screen.getByRole("button", { name: "Before" })).toHaveFocus();
  });

  // The dashboard has no keyboard shortcuts: j and k are keys like any other.
  it("does not move on j or k", async () => {
    render(<List />);
    open("one").focus();
    await userEvent.keyboard("jk");
    expect(open("one")).toHaveFocus();
    await userEvent.keyboard("j");
    expect(open("one")).toHaveFocus();
  });

  it("opens with Enter and toggles the row's checkbox with Space", async () => {
    const opened: string[] = [];
    render(<List onOpen={(n) => opened.push(n)} />);
    const user = userEvent.setup();
    await user.keyboard("{ArrowDown}{ArrowDown} ");
    expect(screen.getByLabelText("Select two")).toBeChecked();
    expect(opened).toEqual([]);
    expect(open("two")).toHaveFocus();
    await user.keyboard(" ");
    expect(screen.getByLabelText("Select two")).not.toBeChecked();
    await user.keyboard("{Enter}");
    expect(opened).toEqual(["two"]);
  });

  it("keeps one tab stop as rows change", () => {
    const { rerender } = render(<List names={["one", "two"]} />);
    rerender(<List names={["zero", "one", "two"]} />);
    return Promise.resolve().then(() => {
      const stops = screen.getAllByRole("button").filter((b) => b.hasAttribute("data-row-open") && b.tabIndex === 0);
      expect(stops.map((b) => b.textContent)).toEqual(["one"]);
      expect(open("zero")).toHaveAttribute("tabindex", "-1");
    });
  });

  // Only the current row's controls are reachable with Tab, after its open button: every
  // other row's tag chips and menu triggers are out of the tab order too.
  it("keeps the controls of every row but the current one out of the tab order", async () => {
    render(<List controls />);
    const user = userEvent.setup();
    screen.getByRole("button", { name: "Before" }).focus();
    const stops: string[] = [];
    for (let i = 0; i < 4; i++) {
      await user.tab();
      stops.push(document.activeElement?.textContent ?? "");
    }
    expect(stops).toEqual(["one", "tag one", "Actions for one", "After"]);
    expect(screen.getByRole("button", { name: "tag two" })).toHaveAttribute("tabindex", "-1");
    // The current row moves with the arrows, and its controls with it.
    open("one").focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("button", { name: "tag one" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("button", { name: "tag two" })).not.toHaveAttribute("tabindex");
    await user.tab();
    expect(screen.getByRole("button", { name: "tag two" })).toHaveFocus();
  });

  it("makes a row current when one of its controls takes the focus", () => {
    render(<List controls />);
    screen.getByRole("button", { name: "Actions for three" }).focus();
    expect(open("three")).toHaveAttribute("tabindex", "0");
    expect(open("one")).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("button", { name: "Actions for one" })).toHaveAttribute("tabindex", "-1");
  });
});
