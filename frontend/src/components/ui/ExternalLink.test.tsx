import { describe, expect, it } from "vitest";
import { render, screen } from "../../test/rtl";
import { ExternalLink } from "./ExternalLink";

describe("ExternalLink", () => {
  it("opens in a new tab and is named by its text, not its icon", () => {
    render(<ExternalLink href="https://example.com/runbook">example.com/runbook</ExternalLink>);
    const link = screen.getByRole("link", { name: "example.com/runbook (opens in a new tab)" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link.querySelector("svg")).not.toBeNull();
  });
});
