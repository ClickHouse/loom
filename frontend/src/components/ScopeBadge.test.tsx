import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScopeBadge } from "./ScopeBadge";

describe("ScopeBadge", () => {
  it("names the namespace the view reads from", () => {
    render(<ScopeBadge kind="namespace" value="team-a" />);
    expect(screen.getByText("team-a")).toBeInTheDocument();
    expect(screen.getByText(/namespace/)).toBeInTheDocument();
    expect(screen.queryByText("No namespace selected")).not.toBeInTheDocument();
  });

  it("says so when no namespace is selected", () => {
    render(<ScopeBadge kind="namespace" value="" />);
    expect(screen.getByText("No namespace selected")).toBeInTheDocument();
  });

  it("labels a Loom-wide view", () => {
    render(<ScopeBadge kind="loom-wide" />);
    expect(screen.getByText("Loom-wide")).toBeInTheDocument();
  });

  it("hides its icon from assistive technology", () => {
    const { container } = render(<ScopeBadge kind="namespace" value="team-a" />);
    const icons = container.querySelectorAll("svg");
    expect(icons).toHaveLength(1);
    expect(icons[0]).toHaveAttribute("aria-hidden", "true");
  });
});
