import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "../../test/rtl";
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

  it("returns focus to a busy button that lost focus before the dialog opened", async () => {
    function Generate() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Generate token
          </button>
          <Modal open={open} onOpenChange={setOpen} title="Token generated" />
        </>
      );
    }
    render(<Generate />);
    const opener = screen.getByRole("button", { name: "Generate token" });
    opener.focus();
    // Click UI disables a loading button, which drops focus to the body before the dialog opens.
    opener.blur();
    expect(document.activeElement).toBe(document.body);
    act(() => opener.click());
    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
