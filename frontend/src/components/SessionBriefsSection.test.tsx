import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFoldOpen } from "../lib/folds";
import { SessionBriefsSection } from "./SessionBriefsSection";

// A brief whose conversation was never recorded is still findable: the
// Conversations page holds the namespace's briefs in one bucket above its threads,
// folded to its title so it never pushes the threads down, and read only once opened.

const listBriefs = vi.fn();
const getBrief = vi.fn();
vi.mock("../api/briefs", () => ({
  listBriefs: (...a: unknown[]) => listBriefs(...a),
  getBrief: (...a: unknown[]) => getBrief(...a),
}));

describe("SessionBriefsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setFoldOpen("session-briefs", false);
  });
  afterEach(cleanup);

  it("renders nothing when the namespace holds no brief", async () => {
    listBriefs.mockResolvedValue([]);
    const { container } = render(<SessionBriefsSection org="o" namespace="ns" />);
    await waitFor(() => expect(listBriefs).toHaveBeenCalledWith({ org: "o", namespace: "ns" }, { limit: 20 }));
    expect(container).toBeEmptyDOMElement();
  });

  it("lists each brief, looked up by its key, and opens it in place", async () => {
    listBriefs.mockResolvedValue([{ brief_key: "auto/loom/main" }, { brief_key: "handoff/billing" }]);
    getBrief.mockImplementation(async (_id: unknown, key: string) => ({
      found: true,
      brief_key: key,
      revision: 1,
      revisions: 1,
      rendered: `brief ${key}`,
      updated_at: null,
    }));
    render(<SessionBriefsSection org="o" namespace="ns" />);
    const toggle = await screen.findByRole("button", { name: "Session briefs" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(getBrief).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    await waitFor(() => expect(getBrief).toHaveBeenCalledTimes(2));
    expect(getBrief.mock.calls.map((c) => c[1]).sort()).toEqual(["auto/loom/main", "handoff/billing"]);
    fireEvent.click(await screen.findByRole("button", { name: /handoff\/billing/ }));
    expect(await screen.findByText("brief handoff/billing")).toBeInTheDocument();
    expect(screen.getByText("Restore the session brief handoff/billing.")).toBeInTheDocument();
  });
});
