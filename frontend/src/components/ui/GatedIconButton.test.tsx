import { render, screen } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GatedIconButton } from "./GatedIconButton";

describe("GatedIconButton", () => {
  it("keeps the focus, and starts working, when its reason clears", async () => {
    const onClick = vi.fn();
    const reason = "Needs admin on team. An admin of it can grant that.";
    const { rerender } = render(<GatedIconButton icon="trash" label="Remove bob" reason={reason} onClick={onClick} />);
    const button = screen.getByRole("button", { name: "Remove bob" });
    expect(button).toHaveAccessibleDescription(reason);
    button.focus();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
    rerender(<GatedIconButton icon="trash" label="Remove bob" reason={null} onClick={onClick} />);
    expect(screen.getByRole("button", { name: "Remove bob" })).toBe(button);
    expect(button).toHaveFocus();
    expect(button).not.toHaveAttribute("aria-disabled");
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("does not read its name again as its description when its tooltip only repeats it", async () => {
    render(<GatedIconButton icon="trash" label="Remove bob" reason={null} />);
    const button = screen.getByRole("button", { name: "Remove bob" });
    await userEvent.tab();
    expect(button).toHaveFocus();
    expect(button).toHaveAccessibleDescription("");
  });
});
