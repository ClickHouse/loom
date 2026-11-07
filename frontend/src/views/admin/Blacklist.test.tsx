import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BlacklistRow } from "../../api";
import { useStore } from "../../lib/state";
import { Blacklist, PrivacyFilterHits, PrivacyFilters } from "./Blacklist";

const listBlacklist = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listBlacklist: (...a: unknown[]) => listBlacklist(...a), isAuthDisabled: () => false };
});

const row = (over: Partial<BlacklistRow>): BlacklistRow => ({
  pattern_id: "p1",
  org: "dev",
  namespace: "ns",
  pattern: "sk-",
  pattern_type: "substring",
  scope: "namespace",
  reason: "",
  created_by: "a@x",
  hit_count: 3,
  last_hit_at: "2025-03-04 10:00:00",
  created_at: "2025-01-01T00:00:00Z",
  ...over,
});

// A pattern's last hit is the list's one time format, a <time> with the absolute UTC time
// behind it.
describe("a privacy filter's hits", () => {
  it("draws the last hit with ListTime", () => {
    render(<PrivacyFilterHits row={row({})} />);
    const time = screen.getByText(/^last/).querySelector("time");
    expect(time).toHaveAttribute("dateTime", "2025-03-04T10:00:00.000Z");
    expect(time).toHaveTextContent("Mar 4, 2025");
    // Quiet in the pattern's row: no tab stop of its own.
    expect(time).not.toHaveAttribute("tabindex");
  });

  it("says when a pattern never matched or its last hit is unknown", () => {
    const { rerender } = render(<PrivacyFilterHits row={row({ hit_count: 0 })} />);
    expect(screen.getByText("never matched")).toBeInTheDocument();
    rerender(<PrivacyFilterHits row={row({ last_hit_at: null })} />);
    expect(screen.getByText("last hit unknown")).toBeInTheDocument();
  });
});

describe("privacy filters, a failed read", () => {
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, whoami: null });
  });

  it("says the read failed, with Retry, never that there are no patterns", async () => {
    useStore.setState({
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "a@x", org: "dev", roles: ["ADMIN"], namespaces: ["ns"] },
    });
    let answer: (r: { rows: BlacklistRow[] }) => void = () => {};
    listBlacklist
      .mockRejectedValueOnce(new Error("down"))
      .mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    render(<PrivacyFilters />);
    expect(await screen.findByText("Couldn't load privacy filters")).toBeInTheDocument();
    expect(screen.queryByText("No patterns")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    // While Retry reads, the list is loading, not empty.
    await waitFor(() => expect(screen.queryByText("Couldn't load privacy filters")).toBeNull());
    expect(screen.queryByText("No patterns")).toBeNull();
    expect(screen.getByLabelText("Loading privacy filters")).toBeInTheDocument();
    answer({ rows: [row({})] });
    expect(await screen.findByText("sk-")).toBeInTheDocument();
  });
});

describe("privacy filters across a namespace switch", () => {
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, whoami: null });
  });

  // The count's live region is mounted before the read, so the count is announced.
  it("announces how many patterns it holds", async () => {
    useStore.setState({
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "a@x", org: "dev", roles: ["ADMIN"], namespaces: ["ns"] },
    });
    listBlacklist.mockResolvedValueOnce({ rows: [row({}), row({ pattern_id: "p2", pattern: "pk-" })] });
    render(<PrivacyFilters />);
    await waitFor(() => expect(screen.getByText("2 patterns")).toHaveAttribute("aria-live", "polite"));
  });

  it("never draws the old namespace's patterns under the new one", async () => {
    useStore.setState({
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      whoami: { authenticated: true, subject: "a@x", org: "dev", roles: ["ADMIN"], namespaces: ["ns", "other"] },
    });
    listBlacklist.mockResolvedValueOnce({ rows: [row({})] }).mockReturnValueOnce(new Promise(() => {}));
    // The page keys the list by namespace, so a switch mounts a fresh one.
    render(<Blacklist />);
    expect(await screen.findByText("sk-")).toBeInTheDocument();
    act(() => useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } }));
    expect(screen.queryByText("sk-")).toBeNull();
    await waitFor(() =>
      expect(listBlacklist).toHaveBeenLastCalledWith(
        expect.objectContaining({ namespace: "other" }),
        expect.anything(),
      ),
    );
  });
});
