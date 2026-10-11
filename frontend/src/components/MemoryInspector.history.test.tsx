import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MemoryHistoryEntry } from "../api";
import { useStore } from "../lib/state";
import { MemoryInspector } from "./MemoryInspector";

// Each history version is labelled against the previous version of the same memory:
// a namespace-wide read interleaves another writer's rows of the key.

const LIVE: MemoryDetail = {
  found: true,
  value: "shared text",
  memory_type: "semantic",
  confidence: 1,
  version: 3,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  tags: ["owner:new"],
  status: "active",
  agent: "a",
};

const row = (patch: Partial<MemoryHistoryEntry>): MemoryHistoryEntry => ({
  memory_id: "m-a",
  version: 1,
  value: "shared text",
  content_sha256: "s1",
  created_at: "2026-09-18T09:00:00Z",
  tombstoned_at: null,
  redacted_at: null,
  source_trace: "",
  confidence: 1,
  tags: [],
  ...patch,
});

const HISTORY = [
  row({ version: 3, tags: ["owner:new"] }),
  row({ memory_id: "m-b", version: 2, verified_at: "2026-09-18T09:00:00Z", verified_by: "b", lock_mode: "frozen" }),
  row({ version: 1, tags: ["owner:old"] }),
];

vi.mock("./Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => LIVE),
    memoryHistory: vi.fn(async () => ({ rows: HISTORY })),
    memoryRelated: vi.fn(async () => ({ rows: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
import { memoryHistory } from "../api";

const identity = { org: "dev", namespace: "team", agent: "*" };

describe("MemoryInspector history labels", () => {
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, inspectedMemoryKey: null });
  });

  it("compares a version with its own memory's previous version, not the adjacent row", async () => {
    useStore.setState({ identity, inspectedMemoryKey: "k" });
    render(<MemoryInspector identity={identity} memoryKey="k" onClose={() => {}} onChanged={() => {}} />);
    await screen.findAllByText("shared text");
    await userEvent.click(screen.getByRole("button", { name: /History/ }));
    await waitFor(() => expect(memoryHistory).toHaveBeenCalled());
    expect(await screen.findByText("tags +owner:new −owner:old")).toBeInTheDocument();
    expect(screen.queryByText(/verification revoked|unlocked/)).toBeNull();
  });
});
