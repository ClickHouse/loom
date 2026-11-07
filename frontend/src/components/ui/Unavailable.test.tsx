import { Button } from "@clickhouse/click-ui/Button";
import { render, screen } from "../../test/rtl";
import { describe, expect, it } from "vitest";
import { Unavailable } from "./Unavailable";

// styles.css keys the disabled look on `data-unavailable`, Click UI's class names never.
describe("Unavailable", () => {
  it("marks the button with its type and leaves it focusable", () => {
    render(
      <Unavailable reason="Read-only here">
        <Button type="secondary" htmlType="button" label="New" />
      </Unavailable>,
    );
    const button = screen.getByRole("button", { name: "New" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAttribute("data-unavailable", "secondary");
    expect(button).not.toBeDisabled();
  });

  it("defaults to Click UI's primary type and is not marked without a reason", () => {
    const { rerender } = render(
      <Unavailable reason="Read-only here">
        <Button htmlType="button" label="Add" />
      </Unavailable>,
    );
    expect(screen.getByRole("button", { name: "Add" })).toHaveAttribute("data-unavailable", "primary");
    rerender(
      <Unavailable reason={null}>
        <Button htmlType="button" label="Add" />
      </Unavailable>,
    );
    expect(screen.getByRole("button", { name: "Add" })).not.toHaveAttribute("data-unavailable");
  });

  it("keeps a focused child focused as its reason comes and goes", () => {
    const { rerender } = render(
      <Unavailable reason="Loading">
        <Button htmlType="button" label="Draft" />
      </Unavailable>,
    );
    const button = screen.getByRole("button", { name: "Draft" });
    button.focus();
    rerender(
      <Unavailable reason={null}>
        <Button htmlType="button" label="Draft" />
      </Unavailable>,
    );
    expect(screen.getByRole("button", { name: "Draft" })).toBe(button);
    expect(button).toHaveFocus();
    expect(button).not.toHaveAttribute("aria-disabled");
  });
});
