import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry } from "../api";
import { SENSITIVE_LABEL } from "../lib/format";
import { useStore } from "../lib/state";
import { Recent } from "./Recent";

// A high row's value is readable by key only, through an audited read: the page shows a
// listed row the list labels `sensitivity: high` as sensitive, and reads nothing while it
// scrolls.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(),
    getMemory: vi.fn(),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    searchMemory: vi.fn(async () => ({ results: [], embedding_model: "fake" })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { bulkGetMemories, getMemory, listMemories } from "../api";

const entry = (key: string, over: Partial<MemoryListEntry> = {}): MemoryListEntry => ({
  memory_key: key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: new Date().toISOString(),
  expires_at: null,
  pinned: false,
  tags: [],
  value: `value of ${key}`,
  ...over,
});

describe("a high row on the Memories page", () => {
  beforeEach(() => {
    vi.mocked(listMemories).mockResolvedValue({
      entries: [entry("plain/key"), entry("vault/key", { value: null, sensitivity: "high" })],
      total: 2,
      truncated: false,
      today: 2,
    });
    useStore.setState({
      authed: true,
      whoami: { authenticated: false },
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ whoami: null, identity: null });
  });

  it("is shown as sensitive and its value is never read", async () => {
    render(<Recent />);
    expect(await screen.findByText("Sensitive: open it to read")).toBeTruthy();
    expect(screen.getByText("value of plain/key")).toBeTruthy();
    // The list serves `sensitivity` with every entry; asking for it by name would 422 on an
    // older replica during a rolling upgrade.
    const fields = vi.mocked(listMemories).mock.calls[0]?.[2]?.fields ?? [];
    expect(fields).not.toContain("sensitivity");
    expect(getMemory).not.toHaveBeenCalled();
    const asked = vi.mocked(bulkGetMemories).mock.calls.flatMap((c) => c[1] as string[]);
    expect(asked).not.toContain("vault/key");
  });

  it("found by a search is shown as sensitive and its value is never read", async () => {
    // The page holds the plain row only; the keyword half of the search finds the high one.
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? {
            entries: [entry("vault/key", { value: null, sensitivity: "high" })],
            total: 1,
            truncated: false,
            today: 1,
          }
        : { entries: [entry("plain/key")], total: 1, truncated: false, today: 1 },
    );
    render(<Recent />);
    await screen.findByText("plain/key");
    await userEvent.type(screen.getByLabelText("Search memories"), "vault");
    expect(await screen.findByText("vault/key")).toBeTruthy();
    expect(screen.getByText(SENSITIVE_LABEL)).toBeTruthy();
    expect(getMemory).not.toHaveBeenCalled();
    const asked = vi.mocked(bulkGetMemories).mock.calls.flatMap((c) => c[1] as string[]);
    expect(asked).not.toContain("vault/key");
  });
});
