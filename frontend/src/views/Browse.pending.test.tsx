import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Browse } from "./Browse";

// A pending join is written to the cache the App bootstrap diffs on the next visit
// (the "approved by …" toast). The key is persisted in users' browsers, so the
// literal here is the format as shipped.

const browseNamespaces = vi.fn();
const joinNamespace = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    browseNamespaces: (...args: unknown[]) => browseNamespaces(...args),
    joinNamespace: (...args: unknown[]) => joinNamespace(...args),
  };
});
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve([]) }));

async function requestWriter() {
  render(<Browse />);
  fireEvent.click(await screen.findByRole("button", { name: "Join" }));
  fireEvent.click(await screen.findByRole("radio", { name: "Writer" }));
  fireEvent.click(screen.getByRole("button", { name: "Request writer" }));
  await waitFor(() => expect(joinNamespace).toHaveBeenCalledTimes(1));
}

describe("Browse: pending join cache", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    browseNamespaces.mockResolvedValue([
      { namespace: "shared", description: "", kind: "memory", requires_approval: false, member_count: 1 },
    ]);
    joinNamespace.mockResolvedValue({ status: "pending", requested_role: "writer", pending_admins: ["bob"] });
    useStore.setState({ authed: true, identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "browse" });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, identity: null });
  });

  it("keys the cache on the whoami subject", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme" } });
    await requestWriter();
    await waitFor(() => expect(localStorage.getItem("loom_pending_v1_alice")).toBe(JSON.stringify(["shared::writer"])));
  });

  it("keys the cache on the org when whoami has no subject", async () => {
    useStore.setState({ whoami: { authenticated: true, org: "acme" } });
    await requestWriter();
    await waitFor(() => expect(localStorage.getItem("loom_pending_v1_acme")).toBe(JSON.stringify(["shared::writer"])));
  });
});
