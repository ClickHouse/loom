import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../test/rtl";
import { AgentBadge } from "./AgentBadge";

const LONG = "claude-code:laptop-of-a-very-long-hostname";

// An agent name is capped at 180 px with an ellipsis (SPEC-LISTS C8); a cut name has its
// whole self in a tooltip, and one that fits has none.
describe("AgentBadge", () => {
  afterEach(() => vi.restoreAllMocks());

  function overflow(cut: boolean) {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(cut ? 300 : 100);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(cut ? 180 : 100);
  }

  it("is capped, and a name that fits has no tooltip or tab stop", () => {
    overflow(false);
    render(<AgentBadge agent="support-bot" />);
    const name = screen.getByText("support-bot");
    expect(name.closest(".agent-badge")).not.toBeNull();
    expect(name.closest("[tabindex]")).toBeNull();
  });

  it("shows a cut name whole in a tooltip, from the keyboard outside a row", async () => {
    overflow(true);
    render(<AgentBadge agent={LONG} />);
    const target = screen.getByRole("note");
    expect(target).toHaveAttribute("tabindex", "0");
    act(() => target.focus());
    fireEvent.focus(target);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(LONG);
  });

  it("takes no tab stop inside a row's open button", () => {
    overflow(true);
    render(
      <button type="button">
        open <AgentBadge agent={LONG} />
      </button>,
    );
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByText(LONG).closest("[tabindex]")).toBeNull();
  });

  // Review: measuring after mount drew the row twice; a row passes `quiet` instead.
  it("takes no tab stop when the row says it is quiet", () => {
    overflow(true);
    render(<AgentBadge agent={LONG} quiet />);
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.getByText(LONG).closest("[tabindex]")).toBeNull();
  });
});
