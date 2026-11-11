import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
import { useDock } from "../lib/assistantDock";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";

// What the namespace's usage says as a whole (the audit, the mentor, the inventory) is the
// assistant's starters: the Memories header keeps one Insights button where its tab was,
// which opens the panel on them, nothing asked.

const { openStarters } = vi.hoisted(() => ({ openStarters: vi.fn() }));
vi.mock("../lib/assistantDock", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/assistantDock")>()),
  loadDockRuntime: async () => ({ openStarters }),
}));

const entry = (memory_key: string): MemoryListEntry => ({
  memory_key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent: "claude-desktop",
  value: `value of ${memory_key}`,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [entry("coffee_pref")], total: 1, today: 1, truncated: false })),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

const row: MyNamespaceEntry = {
  namespace: "team",
  role: "viewer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};
const viewer: WhoAmI = { authenticated: true, subject: "v@example.com", org: "dev", roles: ["USER"], namespaces: [] };

describe("Memories: the Insights button", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useDock.setState({ working: false });
    useStore.setState({
      authed: true,
      whoami: viewer,
      myNamespaces: [row],
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useDock.setState({ working: false });
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("opens the assistant on its starters, with no Insights tab", async () => {
    render(<Recent />);
    await screen.findByText("value of coffee_pref");
    expect(screen.queryByRole("tab", { name: "Insights" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Insights" }));
    await vi.waitFor(() => expect(openStarters).toHaveBeenCalledWith(false));
  });

  it("says, while an answer streams, when the starters show", async () => {
    useDock.setState({ working: true });
    render(<Recent />);
    expect(await screen.findByRole("button", { name: "Insights" })).toHaveAccessibleDescription(
      "Starters show once this answer ends: choose New conversation.",
    );
  });
});
