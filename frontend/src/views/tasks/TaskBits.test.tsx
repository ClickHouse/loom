import { cleanup, render } from "../../test/rtl";
import { afterEach, describe, expect, it } from "vitest";
import { PriorityBar } from "./TaskBits";

afterEach(() => cleanup());

// A task's priority is written, not told by a bar's colour alone.
describe("PriorityBar", () => {
  it("writes the priority in a neutral Click UI badge, and names it in full", () => {
    const { container } = render(<PriorityBar priority={1} />);
    const badge = container.querySelector('[data-testid="ellipsed-badge-content"]');
    expect(badge?.textContent).toBe("P1");
    expect(container.textContent).toContain("P1 urgent");
    expect(container.querySelector(".tq-pri")).toBeNull();
  });
});
