import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProfileActivityEntry } from "../api";
import { useStore } from "../lib/state";
import { Profile } from "./Profile";

// The view stays mounted across a namespace switch, so a profile.list or the chip's
// activity read that answers for the namespace the reader left is dropped: B never shows
// A's sections or A's readers.

const listProfile = vi.fn();
const getProfileActivity = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listProfile: (...args: unknown[]) => listProfile(...args),
    getProfileActivity: (...args: unknown[]) => getProfileActivity(...args),
    isAuthDisabled: () => false,
  };
});

type Sections = { entries: { label: string; value: string; char_limit: number }[] };
type Activity = { entries: ProfileActivityEntry[] };
function deferred<T>() {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const reader = (agent: string): ProfileActivityEntry => ({
  agent,
  last_activated_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  event_count: 1,
  operations: ["context.pack"],
});

describe("Profile: a namespace switch mid-read", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "profile",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("drops namespace A's sections when they land after B's", async () => {
    const a = deferred<Sections>();
    const b = deferred<Sections>();
    listProfile.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    getProfileActivity.mockResolvedValue({ entries: [] });
    render(<Profile />);
    await waitFor(() => expect(listProfile).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ identity: { org: "dev", namespace: "ns-b", agent: "loom-ui" } }));
    await waitFor(() => expect(listProfile).toHaveBeenCalledTimes(2));
    expect(listProfile.mock.calls[1][0]).toMatchObject({ namespace: "ns-b" });

    await act(async () => b.resolve({ entries: [{ label: "persona", value: "Written in B.", char_limit: 8192 }] }));
    expect(await screen.findByText("Written in B.")).toBeInTheDocument();
    await act(async () => a.resolve({ entries: [{ label: "persona", value: "Written in A.", char_limit: 8192 }] }));
    expect(screen.queryByText("Written in A.")).toBeNull();
    expect(screen.getByText("Written in B.")).toBeInTheDocument();
  });

  it("drops namespace A's readers from the chip when they land after B's", async () => {
    const a = deferred<Activity>();
    const b = deferred<Activity>();
    listProfile.mockResolvedValue({ entries: [{ label: "persona", value: "Terse.", char_limit: 8192 }] });
    getProfileActivity.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    render(<Profile />);
    await waitFor(() => expect(getProfileActivity).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ identity: { org: "dev", namespace: "ns-b", agent: "loom-ui" } }));
    await waitFor(() => expect(getProfileActivity).toHaveBeenCalledTimes(2));

    await act(async () => b.resolve({ entries: [] }));
    expect(await screen.findByText("No AI client has read this in the last 7 days")).toBeInTheDocument();
    await act(async () => a.resolve({ entries: [reader("claude-code"), reader("codex")] }));
    expect(screen.queryByText(/Read by 2 clients/)).toBeNull();
    expect(screen.getByText("No AI client has read this in the last 7 days")).toBeInTheDocument();
  });
});
