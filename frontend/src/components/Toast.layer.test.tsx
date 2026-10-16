// A toast raised while a modal dialog is open paints above the dialog's overlay (and under
// tooltips), stays out of the modal's aria-hidden, and still pauses on hover even though the
// modal turns pointer events off on <body>.
import * as Dialog from "@radix-ui/react-dialog";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfirmViewport, confirmAction } from "./ConfirmDialog";
import { POPOVER_LAYER } from "./ui/Tooltip";
import { ToastViewport, toast } from "./Toast";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** The Tailwind z-index utility on an element's class list (`z-50` or `z-[65]`), ignoring
 *  variant-prefixed ones such as `focus:z-[60]`. */
function zOf(cls: string): number {
  const m = /(?:^|\s)z-(?:\[(\d+)\]|(\d+))(?=\s|$)/.exec(cls);
  if (!m) throw new Error(`no z-index utility in "${cls}"`);
  return Number(m[1] ?? m[2]);
}

function viewport(): HTMLElement {
  const el = document.querySelector("[data-toast-viewport]");
  if (!(el instanceof HTMLElement)) throw new Error("toast viewport not rendered");
  return el;
}

/** The app's shape: the viewport lives in the React tree, dialogs portal after it. */
function renderWithDialog() {
  render(
    <>
      <ToastViewport />
      <ConfirmViewport />
      <Dialog.Root open>
        <Dialog.Portal>
          <Dialog.Overlay data-testid="overlay" className="fixed inset-0 z-50 bg-overlay" />
          <Dialog.Content className="fixed z-50" aria-describedby={undefined}>
            <Dialog.Title>Namespace settings</Dialog.Title>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>,
  );
}

describe("toast layer over dialogs", () => {
  it("stacks above every open dialog overlay and below tooltips", async () => {
    renderWithDialog();
    await act(async () => {
      void confirmAction({ title: "Delete?", danger: true });
    });
    const overlays = [...document.querySelectorAll("[data-state='open']")].filter((el) =>
      (el.getAttribute("class") ?? "").includes("bg-overlay"),
    );
    // Both the plain dialog (z-50) and the confirm (z-[60]) are open.
    expect(overlays.map((el) => zOf(el.getAttribute("class") ?? "")).sort()).toEqual([50, 60]);

    const toastZ = zOf(viewport().className);
    for (const el of overlays) expect(toastZ).toBeGreaterThan(zOf(el.getAttribute("class") ?? ""));
    expect(toastZ).toBeLessThan(zOf(POPOVER_LAYER));
  });

  it("is not hidden from assistive tech by the modal", () => {
    renderWithDialog();
    act(() => toast("Save failed", "error"));
    expect(viewport().closest("[aria-hidden='true']")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Save failed");
  });

  it("pauses on hover while the modal has pointer events off on <body>", () => {
    vi.useFakeTimers();
    renderWithDialog();
    expect(document.body.style.pointerEvents).toBe("none");
    act(() => toast("Save failed", "error"));
    const card = screen.getByRole("alert");
    expect(card).toHaveClass("pointer-events-auto");

    fireEvent.mouseEnter(card);
    act(() => vi.advanceTimersByTime(20_000));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    fireEvent.mouseLeave(card);
    act(() => vi.advanceTimersByTime(8_000));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
