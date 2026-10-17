import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../../test/rtl";
import { Tip } from "./Tooltip";

describe("Tip", () => {
  // A goal or label gains or loses its tooltip as it is cut off or fits again (a resize, a
  // zoom); the control must stay mounted through that, or its focus falls to the page.
  it("keeps its child mounted, focus included, as it turns on and off", () => {
    const { rerender } = render(
      <Tip content="the whole goal" disabled>
        <button type="button">Triage</button>
      </Tip>,
    );
    const button = screen.getByRole("button", { name: "Triage" });
    button.focus();
    rerender(
      <Tip content="the whole goal">
        <button type="button">Triage</button>
      </Tip>,
    );
    expect(screen.getByRole("button", { name: "Triage" })).toBe(button);
    expect(button).toHaveFocus();
  });
});

describe("Tip, turned off while open", () => {
  // Turning the tip off while it is open and back on later must not reopen it on its own:
  // the tooltip stays controlled for its whole life (React warns on a switch).
  it("does not reopen by itself once turned back on", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(
      <Tip content="the whole goal">
        <button type="button">Triage</button>
      </Tip>,
    );
    const button = screen.getByRole("button", { name: "Triage" });
    act(() => button.focus());
    fireEvent.focus(button);
    expect(await screen.findAllByText("the whole goal")).not.toHaveLength(0);
    rerender(
      <Tip content="the whole goal" disabled>
        <button type="button">Triage</button>
      </Tip>,
    );
    act(() => button.blur());
    fireEvent.blur(button);
    rerender(
      <Tip content="the whole goal">
        <button type="button">Triage</button>
      </Tip>,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/controlled to uncontrolled|uncontrolled to controlled/);
    errors.mockRestore();
  });
});

describe("Tip, hovered or focused while off", () => {
  // Radix reports the open but never the close while the tip is off (the close equals the
  // controlled false), so a stored open would show the tip by itself once turned back on.
  it("does not open by itself once turned on", async () => {
    const { rerender } = render(
      <Tip content="Memories" disabled>
        <button type="button">Memories</button>
      </Tip>,
    );
    const button = screen.getByRole("button", { name: "Memories" });
    act(() => button.focus());
    fireEvent.focus(button);
    act(() => button.blur());
    fireEvent.blur(button);
    fireEvent.pointerMove(button);
    fireEvent.pointerLeave(button);
    rerender(
      <Tip content="Memories">
        <button type="button">Memories</button>
      </Tip>,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("Tip, opened and closed in one go", () => {
  // A click right after the tip opened (Radix opens at once just after another tip closed)
  // closes it before React has rendered the open; the close must still land.
  it("stays closed after a focus and a click in the same tick", () => {
    render(
      <Tip content="Collapse sidebar">
        <button type="button">Collapse</button>
      </Tip>,
    );
    const button = screen.getByRole("button", { name: "Collapse" });
    act(() => {
      fireEvent.focus(button);
      fireEvent.click(button);
    });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("Tip, while off", () => {
  // A tip that is off must not count as an open tooltip, or the next tip skips its delay.
  it("leaves the next tip its delay", () => {
    render(
      <>
        <Tip content="Memories" disabled>
          <button type="button">Memories</button>
        </Tip>
        <Tip content="Collapse sidebar">
          <button type="button">Collapse</button>
        </Tip>
      </>,
    );
    fireEvent.focus(screen.getByRole("button", { name: "Memories" }));
    fireEvent.pointerMove(screen.getByRole("button", { name: "Collapse" }));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("TipTarget", () => {
  it("stays one element as its tooltip turns on and off, a stop only while it has one", async () => {
    const { TipTarget } = await import("./TipTarget");
    const { rerender } = render(
      <TipTarget tip="the whole label" disabled>
        label
      </TipTarget>,
    );
    const el = screen.getByText("label");
    expect(el).not.toHaveAttribute("tabindex");
    expect(el).not.toHaveAttribute("role");
    rerender(<TipTarget tip="the whole label">label</TipTarget>);
    expect(screen.getByText("label")).toBe(el);
    expect(el).toHaveAttribute("tabindex", "0");
    expect(el).toHaveAttribute("role", "note");
  });
});
