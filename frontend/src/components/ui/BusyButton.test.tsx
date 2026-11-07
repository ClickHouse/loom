import { fireEvent, render, screen } from "../../test/rtl";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { BusyButton } from "./BusyButton";

// A button whose work is in flight keeps focus (Click UI's `loading` disables it, and a
// disabled button drops focus to the page) and ignores a second press.
describe("BusyButton", () => {
  it("stays focusable and ignores presses while busy", () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <BusyButton type="secondary" htmlType="button" busy={false} onClick={onClick}>
        Refresh
      </BusyButton>,
    );
    const button = screen.getByRole("button", { name: "Refresh" });
    button.focus();
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
    rerender(
      <BusyButton type="secondary" htmlType="button" busy onClick={onClick}>
        Refresh
      </BusyButton>,
    );
    expect(button).not.toBeDisabled();
    expect(button).toHaveFocus();
    expect(button).toHaveAttribute("aria-busy", "true");
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("forwards its ref, and keeps an aria-disabled given to it while idle", () => {
    const ref = createRef<HTMLButtonElement>();
    const onClick = vi.fn();
    render(
      <BusyButton ref={ref} busy={false} type="secondary" htmlType="button" aria-disabled="true" onClick={onClick}>
        Save
      </BusyButton>,
    );
    const button = screen.getByRole("button", { name: "Save" });
    expect(ref.current).toBe(button);
    expect(button).toHaveAttribute("aria-disabled", "true");
  });
});
