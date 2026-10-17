import { render, screen } from "../test/rtl";
import { describe, expect, it } from "vitest";
import { HelpDialog } from "./HelpDialog";

// The shortcuts name each page as the sidebar does.
describe("HelpDialog", () => {
  it("names the Settings page", () => {
    render(<HelpDialog open onOpenChange={() => {}} />);
    expect(screen.getByText("Go to Settings")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).not.toHaveTextContent(/Setup/);
  });
});
