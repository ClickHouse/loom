import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { CollectionEntry } from "../api";
import { MoveCollectionDialog } from "./MoveCollectionDialog";

vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, moveCollection: vi.fn(async () => ({ namespace: "q3", parent: "", moved: true })) };
});

const entry = (namespace: string, parent = ""): CollectionEntry => ({
  namespace,
  parent,
  personal: false,
  role: "admin",
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MoveCollectionDialog", () => {
  it("moves a collection to the top level", async () => {
    const onMoved = vi.fn();
    const onClose = vi.fn();
    render(
      <MoveCollectionDialog
        org="acme"
        collection={entry("q3", "finance")}
        parents={[entry("finance"), entry("q3", "finance")]}
        onMoved={onMoved}
        onClose={onClose}
      />,
    );
    await userEvent.click(screen.getByLabelText("Inside"));
    const top = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[cui-select-item][data-value="__top__"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    // A collection is never offered as its own parent.
    expect(document.querySelector('[cui-select-item][data-value="q3"]')).toBeNull();
    await userEvent.click(top);
    await userEvent.click(screen.getByRole("button", { name: "Move" }));
    await waitFor(() => expect(api.moveCollection).toHaveBeenCalledWith("acme", "q3", ""));
    expect(onMoved).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
