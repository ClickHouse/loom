import { render, screen } from "../../test/rtl";
import { describe, expect, it } from "vitest";
import { SplitButton } from "./SplitButton";

// Click UI 0.13 draws a SplitButton's menu half as an unnamed div a keyboard cannot reach;
// Loom's wrapper makes it a named button in the tab order, and quiet while disabled.
describe("SplitButton", () => {
  it("names its menu half and puts it in the tab order", () => {
    render(
      <SplitButton
        menuLabel="More actions for acme/loom"
        onClick={() => {}}
        menu={[{ label: "Leave", onClick: () => {} }]}
      >
        Open
      </SplitButton>,
    );
    const menu = screen.getByRole("button", { name: "More actions for acme/loom" });
    expect(menu).toHaveAttribute("tabindex", "0");
    expect(menu.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("takes the menu half out of the tab order while disabled", () => {
    render(
      <SplitButton menuLabel="More actions" disabled onClick={() => {}} menu={[{ label: "Leave", onClick: () => {} }]}>
        Open
      </SplitButton>,
    );
    const menu = screen.getByRole("button", { name: "More actions" });
    expect(menu).toHaveAttribute("tabindex", "-1");
    expect(menu).toHaveAttribute("aria-disabled", "true");
  });
});
