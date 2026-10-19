import { cleanup, render } from "../../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PriorityBar, copyTaskId } from "./TaskBits";

const toast = vi.hoisted(() => vi.fn());
vi.mock("../../components/Toast", () => ({ toast }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  toast.mockClear();
});

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

describe("copyTaskId", () => {
  it("shows the id to copy by hand when the page has no clipboard", () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    copyTaskId("t-1");
    expect(toast).toHaveBeenCalledWith("t-1", "ok");
  });

  it("copies the id and says so", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    copyTaskId("t-1");
    await vi.waitFor(() => expect(toast).toHaveBeenCalledWith("Task id copied", "ok"));
    expect(writeText).toHaveBeenCalledWith("t-1");
  });
});
