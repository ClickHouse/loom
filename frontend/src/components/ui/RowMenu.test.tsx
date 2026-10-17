import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "../../test/rtl";
import { RowMenu } from "./RowMenu";

// A row's per-row actions live in one always-visible "More" menu named after the row.
describe("RowMenu", () => {
  const items = (onRename = vi.fn(), onDelete = vi.fn()) => [
    { label: "Rename", icon: "pencil" as const, onSelect: onRename },
    { label: "Archive", onSelect: vi.fn(), disabled: true },
    { label: "Delete", icon: "trash" as const, onSelect: onDelete, danger: true },
  ];

  it("is a dots button named after the row, and runs the item picked", async () => {
    const onRename = vi.fn();
    render(<RowMenu name="handbook.pdf" items={items(onRename)} />);
    const trigger = screen.getByRole("button", { name: "Actions for handbook.pdf" });
    expect(trigger).toBeVisible();
    await userEvent.click(trigger);
    const menu = await screen.findByRole("menu", { name: "Actions for handbook.pdf" });
    expect(menu).toBeInTheDocument();
    // The item's name is its label alone, not the icon's.
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    expect(onRename).toHaveBeenCalled();
  });

  it("marks a disabled item and draws a danger item as Click UI's danger", async () => {
    render(<RowMenu name="row" items={items()} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions for row" }));
    expect(await screen.findByRole("menuitem", { name: "Archive" })).toHaveAttribute("data-disabled");
    expect(screen.getByRole("menuitem", { name: "Delete" }).className).toMatch(/danger/);
  });

  it("opens from the keyboard and gives focus back to its trigger on Escape", async () => {
    const user = userEvent.setup();
    render(<RowMenu name="row" items={items()} />);
    const trigger = screen.getByRole("button", { name: "Actions for row" });
    trigger.focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("menu");
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "Rename" })).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(trigger).toHaveFocus();
  });
});
