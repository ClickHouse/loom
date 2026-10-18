import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";

// The Memories list against the list rules: one count, in the toolbar; the toolbar in
// every state, with the one state pattern under it; an Agent and a Status select in
// place of the chip row and the header toggle; the select-all and Shift-click range
// selection, cleared by Escape.

const entry = (memory_key: string, over: Partial<MemoryListEntry> = {}): MemoryListEntry => ({
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
  ...over,
});

const ROWS = [
  entry("a/pinned", { pinned: true }),
  entry("b/verified", { verification: "human", verified_by: "human:o@example.com", agent: "support-bot" }),
  entry("c/conflicted", { agent: "support-bot" }),
  entry("d/plain"),
];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => ["c/conflicted"]),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { listMemories } from "../api";

const owner: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team"],
};
const row: MyNamespaceEntry = {
  namespace: "team",
  role: "admin",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};

// Click UI's Select items carry no option role; they are addressed by value.
const item = (value: string) => document.querySelector<HTMLElement>(`[cui-select-item][data-value="${value}"]`);
const toolbar = () => screen.getByLabelText("Search and filters");
const shown = () => ROWS.map((r) => r.memory_key).filter((k) => screen.queryByText(k));

async function pick(select: string, value: string) {
  await userEvent.click(within(toolbar()).getByRole("button", { name: select }));
  await waitFor(() => expect(item(value)).not.toBeNull());
  await userEvent.click(item(value) as HTMLElement);
}

describe("Memories list chrome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    vi.mocked(listMemories).mockResolvedValue({ entries: ROWS, total: 1234, today: 2, truncated: false });
    useStore.setState({
      authed: true,
      whoami: owner,
      myNamespaces: [row],
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      view: "memories",
      inspectedMemoryKey: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("says where the list stands once, at the end of the toolbar", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    expect(toolbar()).toHaveTextContent("1–4 of 1,234");
    // No "1,234 total" / "2 today" badges beside it.
    expect(screen.queryByText(/total$/)).toBeNull();
    expect(screen.queryByText(/today$/)).toBeNull();
  });

  it("keeps the toolbar up while the first page loads", async () => {
    vi.mocked(listMemories).mockImplementation(() => new Promise(() => {}));
    render(<Recent />);
    expect(screen.getByLabelText("Search memories")).toBeInTheDocument();
    expect(within(toolbar()).getByRole("button", { name: "Status" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading memories" })).toBeInTheDocument();
  });

  it("says a failed read with a Retry, never the empty state or a zero count", async () => {
    vi.mocked(listMemories).mockRejectedValueOnce(
      new ApiError({ status: 503, code: "INTERNAL", message: "ClickHouse timed out after 30 s", requestId: "req-42" }),
    );
    render(<Recent />);
    expect(await screen.findByText("Couldn't load memories")).toBeInTheDocument();
    expect(screen.getByText("request id req-42")).toBeInTheDocument();
    expect(screen.queryByText("No memories yet")).toBeNull();
    expect(toolbar()).not.toHaveTextContent(/\d/);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("d/plain")).toBeInTheDocument();
  });

  it("shows one Add memory while the namespace is empty: the empty state's", async () => {
    vi.mocked(listMemories).mockResolvedValue({ entries: [], total: 0, today: 0, truncated: false });
    render(<Recent />);
    expect(await screen.findByText("No memories yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Add memory/ })).toHaveLength(1);
    expect(screen.getByLabelText("Search memories")).toBeInTheDocument();
  });

  it("filters by status: pinned, conflicted, verified", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    // No chip row under the toolbar.
    expect(screen.queryByRole("button", { name: /^Pinned/ })).toBeNull();
    await pick("Status", "pinned");
    await waitFor(() => expect(shown()).toEqual(["a/pinned"]));
    expect(toolbar()).toHaveTextContent("1 match");
    await pick("Status", "conflicted");
    await waitFor(() => expect(shown()).toEqual(["c/conflicted"]));
    await pick("Status", "verified");
    await waitFor(() => expect(shown()).toEqual(["b/verified"]));
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(shown()).toEqual(ROWS.map((r) => r.memory_key)));
  });

  it("filters by agent, and keeps every agent it has seen in the select", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    expect(within(toolbar()).getByRole("button", { name: "Agent" })).toHaveTextContent("Every agent");
    vi.mocked(listMemories).mockResolvedValue({
      entries: ROWS.filter((r) => r.agent === "support-bot"),
      total: 2,
      today: 0,
      truncated: false,
    });
    await pick("Agent", "support-bot");
    await waitFor(() => expect(shown()).toEqual(["b/verified", "c/conflicted"]));
    expect(vi.mocked(listMemories).mock.calls.at(-1)?.[2]).toMatchObject({ agent: "support-bot" });
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Agent" }));
    await waitFor(() => expect(item("claude-desktop")).not.toBeNull());
  });

  it("selects a Shift-click range, every row on the page, and clears on Escape", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "Select a/pinned" }));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByRole("checkbox", { name: "Select c/conflicted" }));
    await user.keyboard("{/Shift}");
    expect(toolbar()).toHaveTextContent("3 selected");
    const all = within(toolbar()).getByRole("checkbox", { name: "Select all 4 on this page" });
    expect(all).toHaveAttribute("aria-checked", "mixed");
    await user.click(all);
    expect(toolbar()).toHaveTextContent("4 selected");
    expect(within(toolbar()).getByRole("checkbox", { name: "Select all 4 on this page" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("{Escape}");
    expect(toolbar()).not.toHaveTextContent("selected");
  });
});
