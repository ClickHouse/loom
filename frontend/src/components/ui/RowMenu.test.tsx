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

  it("keeps an item with a reason focusable, even one also disabled, and runs nothing", async () => {
    const onEdit = vi.fn();
    const reason = "Needs writer on team. An admin of it can grant that.";
    render(
      <RowMenu
        name="row"
        items={[
          { label: "Open", onSelect: vi.fn() },
          { label: "Edit", onSelect: onEdit, disabled: true, reason },
        ]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Actions for row" }));
    const edit = await screen.findByRole("menuitem", { name: "Edit" });
    expect(edit).toHaveAccessibleDescription(reason);
    // Radix skips a disabled item; one with a reason is reached by the arrows.
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    await waitFor(() => expect(edit).toHaveFocus());
    await userEvent.keyboard("{Enter}");
    expect(onEdit).not.toHaveBeenCalled();
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

describe("RowMenu in a one-tab-stop grid row", () => {
  it("can leave the tab order, its row's keys reaching it", () => {
    render(<RowMenu name="task 1" items={[{ label: "Defer", onSelect: () => {} }]} tabIndex={-1} />);
    expect(screen.getByRole("button", { name: "Actions for task 1" })).toHaveAttribute("tabindex", "-1");
  });
});
