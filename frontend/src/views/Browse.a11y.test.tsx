import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Browse } from "./Browse";

// The namespace count is a status message (WCAG 4.1.3): the search narrows the list
// without moving focus, so the count is announced as it changes.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    browseNamespaces: vi.fn(async () => [
      { namespace: "alpha", description: "", kind: "memory", requires_approval: false, member_count: 1 },
      { namespace: "beta", description: "", kind: "memory", requires_approval: false, member_count: 2 },
      { namespace: "code-x", description: "", kind: "code", requires_approval: false, member_count: 1 },
    ]),
  };
});
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve([]) }));

beforeEach(() => {
  useStore.setState({ authed: true, identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "browse" });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null });
});

describe("Browse: the namespace count", () => {
  it("is the toolbar's live count, the same element through a search", async () => {
    render(<Browse />);
    expect(await screen.findByText("alpha")).toBeInTheDocument();
    const count = screen.getByText("2 namespaces");
    expect(count).toHaveAttribute("aria-live");
    await userEvent.setup().type(screen.getByRole("textbox", { name: "Search namespaces" }), "alp");
    expect(count).toHaveTextContent("1 namespace");
    expect(count).toBeInTheDocument();
  });
});

describe("Browse: the way to Repositories", () => {
  it("is a Click UI link-styled button at the 12 px of its sentence", async () => {
    render(<Browse />);
    const go = await screen.findByRole("button", { name: "Repositories page" });
    expect(go.className).toMatch(/\blink_size_sm/);
    await userEvent.setup().click(go);
    expect(useStore.getState().view).toBe("code");
  });
});
