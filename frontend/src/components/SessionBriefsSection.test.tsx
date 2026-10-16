import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionBriefsSection } from "./SessionBriefsSection";

// A brief whose conversation was never recorded is still findable: the
// Conversations page lists the namespace's briefs above its threads.

const listBriefs = vi.fn();
const getBrief = vi.fn();
vi.mock("../api/briefs", () => ({
  listBriefs: (...a: unknown[]) => listBriefs(...a),
  getBrief: (...a: unknown[]) => getBrief(...a),
}));

describe("SessionBriefsSection", () => {
  beforeEach(() => vi.clearAllMocks());
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
    expect(await screen.findByText("Session briefs")).toBeInTheDocument();
    await waitFor(() => expect(getBrief).toHaveBeenCalledTimes(2));
    expect(getBrief.mock.calls.map((c) => c[1]).sort()).toEqual(["auto/loom/main", "handoff/billing"]);
    fireEvent.click(await screen.findByRole("button", { name: /handoff\/billing/ }));
    expect(await screen.findByText("brief handoff/billing")).toBeInTheDocument();
    expect(screen.getByText("Restore the session brief handoff/billing.")).toBeInTheDocument();
  });
});
