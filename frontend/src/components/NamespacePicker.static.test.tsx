import { act, cleanup, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { NamespacePicker } from "./NamespacePicker";

// With auth disabled the namespace is a label, not a control: a cut name takes a tab stop
// and shows whole in a tooltip; a name that fits takes neither.

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  isAuthDisabled: () => true,
}));

// jsdom lays nothing out: a name is "cut" when its scroll width passes its box.
function layOut(scrollWidth: number) {
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(scrollWidth);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(100);
}

describe("NamespacePicker with auth disabled", () => {
  beforeEach(() => useStore.setState({ identity: { org: "dev", namespace: "a-long-namespace", agent: "loom-ui" } }));
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("makes a cut name focusable, whole in a tooltip", async () => {
    layOut(400);
    render(<NamespacePicker />);
    const name = screen.getByRole("note");
    expect(name).toHaveTextContent("a-long-namespace");
    expect(name).toHaveAttribute("tabindex", "0");
    act(() => name.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("a-long-namespace");
  });

  it("says Namespace before the name: shown from xl, read by a screen reader below it", () => {
    layOut(80);
    render(<NamespacePicker />);
    const word = screen.getByText("Namespace");
    expect(word).toHaveClass("sr-only", "xl:not-sr-only");
    expect(word).not.toHaveAttribute("aria-hidden");
  });

  it("leaves a name that fits as plain text", () => {
    layOut(80);
    render(<NamespacePicker />);
    expect(screen.getByText("a-long-namespace")).not.toHaveAttribute("tabindex");
    expect(screen.queryByRole("note")).toBeNull();
  });
});
