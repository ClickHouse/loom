import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowseEntry } from "../api";
import { useStore } from "../lib/state";
import { Browse } from "./Browse";

// The rows are memoised and filter by the text a render behind the box: a keystroke that
// leaves the rows as they were renders none of them again. Each row renders its More menu,
// so RowMenu counts the rows rendered.

const renders = { count: 0 };
vi.mock("../components/ui/RowMenu", () => ({
  RowMenu: ({ name }: { name: string }) => {
    renders.count += 1;
    return <button type="button">{`Actions for ${name}`}</button>;
  },
}));

const space = (namespace: string): BrowseEntry => ({
  namespace,
  description: "",
  kind: "memory",
  owner_email: "",
  member_count: 1,
  created_at: "",
});
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  browseNamespaces: vi.fn(async () => [space("alpha"), space("beta"), space("gamma")]),
}));
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve([]) }));

describe("Namespaces in your org row renders", () => {
  beforeEach(() => {
    renders.count = 0;
    useStore.setState({ authed: true, identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "browse" });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("a keystroke whose search every row still matches renders no row again", async () => {
    render(<Browse />);
    await screen.findByText("gamma");
    renders.count = 0;
    await act(async () => {
      fireEvent.change(screen.getByRole("textbox", { name: "Search namespaces" }), { target: { value: "a" } });
    });
    expect(screen.getByText("gamma")).toBeInTheDocument();
    expect(renders.count).toBe(0);
  });
});
