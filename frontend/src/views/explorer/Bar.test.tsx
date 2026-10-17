import { cleanup, render, screen } from "../../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Bar, type BarProps } from "./Bar";

const props = (over: Partial<BarProps> = {}): BarProps => ({
  identity: { org: "dev", namespace: "team", agent: "loom-ui" },
  entities: [],
  draft: "",
  onDraft: vi.fn(),
  onWalk: vi.fn(),
  onPath: vi.fn(),
  trail: [],
  currentSeed: null,
  busy: false,
  patternsAvailable: true,
  patterns: false,
  onPatterns: vi.fn(),
  ...over,
});

afterEach(cleanup);

describe("Graph bar", () => {
  it("keeps Path focusable while it is unavailable, with the reason as its description", () => {
    const onPath = vi.fn();
    render(<Bar {...props({ onPath })} />);
    const path = screen.getByRole("button", { name: "Path" });
    expect(path).toBeEnabled();
    expect(path).toHaveAttribute("aria-disabled", "true");
    expect(path).toHaveAccessibleDescription(/Type a second entity while a walk is open/);
    path.click();
    expect(onPath).not.toHaveBeenCalled();
  });

  it("names the path once a walk is open and a second entity is typed", () => {
    const onPath = vi.fn();
    render(<Bar {...props({ onPath, currentSeed: "alice", draft: "bob" })} />);
    const path = screen.getByRole("button", { name: "Path from alice to bob" });
    expect(path).not.toHaveAttribute("aria-disabled");
    path.click();
    expect(onPath).toHaveBeenCalledWith("alice", "bob");
  });

  it("titles the document after the mode it shows", () => {
    const { rerender } = render(<Bar {...props()} />);
    expect(document.title).toBe("Graph · Loom");
    rerender(<Bar {...props({ patterns: true })} />);
    expect(document.title).toBe("Patterns · Graph · Loom");
  });
});
