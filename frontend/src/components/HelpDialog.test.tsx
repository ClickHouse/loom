import { fireEvent, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it } from "vitest";
import { singleKeyShortcutsOn } from "../lib/shortcuts";
import { HelpDialog } from "./HelpDialog";

// The shortcuts name each page as the sidebar does.
describe("HelpDialog", () => {
  afterEach(() => localStorage.clear());

  it("names the Settings page", () => {
    render(<HelpDialog open onOpenChange={() => {}} />);
    expect(screen.getByText("Go to Settings")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).not.toHaveTextContent(/Setup/);
  });

  // WCAG 2.1.4: a shortcut of one character key can be turned off.
  it("turns the single-key shortcuts off and on", () => {
    render(<HelpDialog open onOpenChange={() => {}} />);
    const toggle = screen.getByRole("switch", { name: "Single-key shortcuts" });
    expect(toggle).toBeChecked();
    // Its description names every key it turns off: the global ones, the Tasks keys and the
    // memory drawer's.
    expect(toggle).toHaveAccessibleDescription(/Tasks/);
    expect(toggle).toHaveAccessibleDescription(/e, d and p/);
    fireEvent.click(toggle);
    expect(toggle).not.toBeChecked();
    expect(singleKeyShortcutsOn()).toBe(false);
    fireEvent.click(toggle);
    expect(singleKeyShortcutsOn()).toBe(true);
  });
});
