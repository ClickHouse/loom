import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { keepFocusThroughLoading } from "../../lib/clickUiFixes";
import { act, fireEvent, render, screen, waitFor } from "../../test/rtl";
import { Modal } from "./Modal";

describe("Modal", () => {
  // main.tsx starts the Click UI focus correction the opener fallback reads.
  let stop = () => {};
  beforeEach(() => {
    stop = keepFocusThroughLoading();
  });
  afterEach(() => stop());

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

  // styles.css pads the title clear of the close button by Click UI's test id; a Click UI
  // release that drops the id would let a long title run under the button.
  it("draws its title on the element styles.css pads", () => {
    render(<Modal open onOpenChange={() => {}} title="A long title that would reach the close button" />);
    expect(
      screen.getByText("A long title that would reach the close button").closest("[data-testid='click-dialog-title']"),
    ).not.toBeNull();
  });
});

describe("Modal opened from the assistant panel", () => {
  it("sends the focus to the page's h1 when the panel that opened it has closed", async () => {
    function Page() {
      const [open, setOpen] = useState(false);
      const [panelOpen, setPanelOpen] = useState(true);
      return (
        <>
          <main id="main">
            <h1>Library</h1>
          </main>
          <section data-assistant-panel="" data-open={panelOpen ? "" : undefined}>
            <button type="button" onClick={() => setOpen(true)}>
              spec.pdf
            </button>
          </section>
          <button type="button" onClick={() => setPanelOpen(false)}>
            close panel
          </button>
          <Modal
            open={open}
            onOpenChange={(v) => {
              setOpen(v);
              if (!v) setPanelOpen(false);
            }}
            title="Preview"
          />
        </>
      );
    }
    render(<Page />);
    const opener = screen.getByRole("button", { name: "spec.pdf" });
    opener.focus();
    fireEvent.click(opener);
    await screen.findByRole("dialog");
    fireEvent.keyDown(screen.getByRole("button", { name: "Close" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(screen.getByRole("heading", { name: "Library" })).toHaveFocus());
  });
});
