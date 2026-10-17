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
    fireEvent.keyDown(opener, { key: "Enter" });
    // Click UI disables a loading button, which drops focus to the body before the dialog opens.
    opener.blur();
    expect(document.activeElement).toBe(document.body);
    act(() => opener.click());
    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("returns focus to the button a click pressed, not to an earlier focus", async () => {
    function Page() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <input aria-label="Search Loom" />
          <button type="button" onClick={() => setOpen(true)}>
            Add memory
          </button>
          <Modal open={open} onOpenChange={setOpen} title="Add memory" />
        </>
      );
    }
    render(<Page />);
    screen.getByRole("textbox", { name: "Search Loom" }).focus();
    screen.getByRole("textbox", { name: "Search Loom" }).blur();
    // Safari: a click presses a button without focusing it.
    const add = screen.getByRole("button", { name: "Add memory" });
    fireEvent.pointerDown(add);
    act(() => add.click());
    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(add).toHaveFocus());
  });
});
