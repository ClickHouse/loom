import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfirmViewport, confirmAction } from "./ConfirmDialog";

afterEach(cleanup);

/** Runs `action` only when the confirm resolves true, the way every caller does. */
async function guarded(title: string, action: () => void): Promise<boolean> {
  const ok = await confirmAction({ title, danger: true });
  if (ok) action();
  return ok;
}

/** Opens a confirm inside act so the viewport renders it, and hands back its promise. */
async function open(start: () => Promise<boolean>): Promise<{ result: Promise<boolean> }> {
  let result: Promise<boolean> = Promise.resolve(false);
  await act(async () => {
    result = start();
  });
  return { result };
}

function renderViewport() {
  render(
    <StrictMode>
      <ConfirmViewport />
    </StrictMode>,
  );
}

describe("confirmAction", () => {
  it("settles a superseded confirm as cancelled and never runs its action", async () => {
    renderViewport();
    const actionA = vi.fn();
    const actionB = vi.fn();
    const settledA = vi.fn();
    const settledB = vi.fn();

    const { result: a } = await open(() => guarded("Delete A?", actionA));
    void a.then(settledA);
    expect(screen.getByText("Delete A?")).toBeTruthy();

    const { result: b } = await open(() => guarded("Delete B?", actionB));
    void b.then(settledB);

    await expect(a).resolves.toBe(false);
    expect(actionA).not.toHaveBeenCalled();
    expect(settledA).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Delete A?")).toBeNull();
    expect(screen.getByText("Delete B?")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    });
    await expect(b).resolves.toBe(true);
    expect(actionB).toHaveBeenCalledTimes(1);
    expect(settledB).toHaveBeenCalledTimes(1);
    expect(actionA).not.toHaveBeenCalled();
  });

  it("resolves false exactly once on Escape", async () => {
    renderViewport();
    const onResolve = vi.fn();
    const { result: p } = await open(() => confirmAction({ title: "Revoke token?" }));
    void p.then(onResolve);
    await act(async () => {
      fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    });
    await expect(p).resolves.toBe(false);
    expect(onResolve).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resolves true exactly once when confirmed", async () => {
    renderViewport();
    const action = vi.fn();
    const { result: p } = await open(() => guarded("Delete doc?", action));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    });
    await expect(p).resolves.toBe(true);
    expect(action).toHaveBeenCalledTimes(1);
  });
});

describe("confirm focus", () => {
  it("returns focus to the control that asked, also after a newer confirm replaced it", async () => {
    render(
      <StrictMode>
        <button type="button">Delete key</button>
        <ConfirmViewport />
      </StrictMode>,
    );
    const opener = screen.getByRole("button", { name: "Delete key" });
    opener.focus();
    const first = await open(() => confirmAction({ title: "Delete key?", danger: true }));
    // Cancel takes focus, so a stray Enter cannot confirm.
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    const second = await open(() => confirmAction({ title: "Delete two keys?", danger: true }));
    expect(await first.result).toBe(false);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(await second.result).toBe(false);
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
