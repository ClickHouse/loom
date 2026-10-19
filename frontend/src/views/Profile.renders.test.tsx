import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Profile } from "./Profile";

// The sections are memoised and filter by the text a render behind the box: a keystroke
// that leaves the sections as they were renders none of them again. Each section renders
// its edit time, so ListTime counts the sections rendered.

const renders = { count: 0 };
vi.mock("../components/ui/ListTime", () => ({
  ListTime: ({ iso }: { iso: string }) => {
    renders.count += 1;
    return <time>{iso}</time>;
  },
}));

const section = (label: string) => ({
  label,
  value: `The ${label} section.`,
  char_limit: 8192,
  content_sha256: "x",
  version: 1,
  created_at: "2026-09-20T00:00:00Z",
  updated_at: "2026-09-27T12:00:00Z",
  last_edited_by: "loom-ui",
});
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listProfile: vi.fn(async () => ({ entries: [section("about-me"), section("persona"), section("tools")] })),
  getProfileActivity: vi.fn(async () => ({ entries: [] })),
  isAuthDisabled: () => false,
}));

describe("Profile section renders", () => {
  beforeEach(() => {
    renders.count = 0;
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "ns", agent: "loom-ui" }, view: "profile" });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("a keystroke whose search every section still matches renders no section again", async () => {
    render(<Profile />);
    await screen.findByText("tools");
    renders.count = 0;
    await act(async () => {
      fireEvent.change(screen.getByLabelText("Search profile sections"), { target: { value: "section" } });
    });
    expect(screen.getByText("tools")).toBeInTheDocument();
    expect(renders.count).toBe(0);
  });
});
