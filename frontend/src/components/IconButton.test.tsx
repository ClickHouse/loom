import { render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { IconButton } from "./IconButton";

describe("IconButton", () => {
  it("names the button and reads a longer tooltip as its description", async () => {
    render(
      <IconButton icon="info-in-circle" label="About usage audit" tooltip="Reads the ledger for this namespace." />,
    );
    const button = screen.getByRole("button", { name: "About usage audit" });
    await userEvent.setup().tab();
    expect(button).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Reads the ledger for this namespace.");
    expect(button).toHaveAccessibleDescription("Reads the ledger for this namespace.");
  });

  it("does not read a tooltip that repeats the name a second time", async () => {
    render(<IconButton icon="cross" label="Close" />);
    const button = screen.getByRole("button", { name: "Close" });
    await userEvent.setup().tab();
    await screen.findByRole("tooltip");
    expect(button).not.toHaveAttribute("aria-describedby");
  });

  it("keeps a caller's own description when it has no tooltip of its own", () => {
    render(
      <>
        <span id="the-label">Couldn't confirm</span>
        <IconButton icon="info-in-circle" label="Why this label" aria-describedby="the-label" />
      </>,
    );
    expect(screen.getByRole("button", { name: "Why this label" })).toHaveAccessibleDescription("Couldn't confirm");
  });
});
