import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../test/axe";
import { ChoiceGroup } from "./ChoiceGroup";

type Size = "s" | "m" | "l";

function Harness({ onChange }: { onChange?: (v: Size) => void }) {
  const [size, setSize] = useState<Size>("s");
  return (
    <ChoiceGroup
      label="Size"
      value={size}
      onChange={(v) => {
        setSize(v);
        onChange?.(v);
      }}
      options={[
        { value: "s", label: "Small", hint: "Fits a laptop." },
        { value: "m", label: "Medium" },
        { value: "l", label: "Large", hint: "Needs a desk." },
      ]}
    />
  );
}

describe("ChoiceGroup", () => {
  it("names the group by its label and each radio by its label string", () => {
    render(<Harness />);
    expect(screen.getByRole("radiogroup", { name: "Size" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio").map((r) => r.getAttribute("aria-label"))).toEqual(["Small", "Medium", "Large"]);
    expect(screen.getByRole("radio", { name: "Small" })).toBeChecked();
  });

  it("makes the hint the radio's description", () => {
    render(<Harness />);
    expect(screen.getByRole("radio", { name: "Small" })).toHaveAccessibleDescription("Fits a laptop.");
    expect(screen.getByRole("radio", { name: "Large" })).toHaveAccessibleDescription("Needs a desk.");
    expect(screen.getByRole("radio", { name: "Medium" })).not.toHaveAttribute("aria-describedby");
  });

  it("moves the choice with the arrow keys and reports the typed value", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("radio", { name: "Small" }));
    // Radix moves focus a tick after keydown and selects only while the key is held.
    const press = async (name: string) => {
      await user.keyboard("{ArrowDown>}");
      await waitFor(() => expect(screen.getByRole("radio", { name })).toBeChecked());
      await user.keyboard("{/ArrowDown}");
    };
    await press("Medium");
    expect(onChange).toHaveBeenLastCalledWith("m");
    await press("Large");
    expect(onChange).toHaveBeenLastCalledWith("l");
  });

  it("passes axe", async () => {
    const { container } = render(<Harness />);
    expect(await axeViolations(container)).toEqual([]);
  });
});
