import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "../../test/rtl";
import { Modal } from "./Modal";

describe("Modal", () => {
  it("closes on one Escape, even with focus on its close button", async () => {
    const onOpenChange = vi.fn();
    render(
      <Modal open onOpenChange={onOpenChange} title="Help">
        <p>Body</p>
      </Modal>,
    );
    const close = screen.getByRole("button", { name: "Close" });
    close.focus();
    fireEvent.focus(close);
    // Click UI's own close has no tooltip, so nothing else answers the first Escape.
    expect(close).not.toHaveAttribute("data-state");
    fireEvent.keyDown(close, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
