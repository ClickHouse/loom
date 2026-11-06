import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { invalidateShared } from "../lib/sharedReads";
import { MemoryInspector } from "./MemoryInspector";

// The drawer reads what is open. Opening a card issues memory.get alone; history,
// related, provenance and lineage are disclosures that read once when opened; the tag
// vocabulary is read when the add-tag input is focused. Which sections a person keeps
// open is remembered for the session.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => ({
      found: true,
      memory_key: "k1",
      value: "hello",
      memory_type: "semantic",
      pinned: false,
      tags: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      version: 1,
    })),
    memoryHistory: vi.fn(async () => ({
      rows: [
        { version: 2, value: "hello", created_at: new Date().toISOString() },
        { version: 1, value: "hallo", created_at: new Date().toISOString() },
      ],
    })),
    memoryRelated: vi.fn(async () => ({
      results: [
        { memory_key: "k2", memory_type: "semantic", content_excerpt: "x", overlap_count: 1, shared_entities: ["a"] },
      ],
    })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    getTaxonomy: vi.fn(async () => ({
      tags: [{ value: "ops", count: 1 }],
      entities: [],
      memory_types: [],
      memory_count: 1,
    })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    setMemory: vi.fn(async () => ({ version: 2 })),
  };
});
import {
  getMemory,
  getProvenance,
  getTaxonomy,
  listMemories,
  memoryHistory,
  memoryLineage,
  memoryRelated,
} from "../api";

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["ns"],
};
const identity = { org: "dev", namespace: "ns", agent: "loom-ui" };

function open(memoryKey = "k1") {
  return render(<MemoryInspector identity={identity} memoryKey={memoryKey} onClose={() => {}} onChanged={() => {}} />);
}

describe("MemoryInspector reads what is open", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    invalidateShared();
    try {
      sessionStorage.clear();
    } catch {
      /* jsdom */
    }
    useStore.setState({ authed: true, whoami: viewer, myNamespaces: [row("ns", "viewer")], identity });
  });
  afterEach(() => cleanup());

  it("opens with memory.get alone; the secondary reads wait for their section", async () => {
    open();
    await waitFor(() => expect(getMemory).toHaveBeenCalledTimes(1));
    await screen.findByText("hello");
    expect(memoryHistory).not.toHaveBeenCalled();
    expect(memoryRelated).not.toHaveBeenCalled();
    expect(memoryLineage).not.toHaveBeenCalled();
    expect(getProvenance).not.toHaveBeenCalled();
    expect(getTaxonomy).not.toHaveBeenCalled();
    expect(listMemories).not.toHaveBeenCalled();
    for (const name of ["Provenance", "Related", "History", "Lineage"]) {
      expect(screen.getByRole("button", { name: new RegExp(name) })).toHaveAttribute("aria-expanded", "false");
    }
  });

  it("shows the code the memory is linked to and whether it still matches", async () => {
    vi.mocked(getMemory).mockResolvedValueOnce({
      found: true,
      value: "hello",
      memory_type: "semantic",
      confidence: 1,
      pinned: false,
      tags: [],
      created_at: new Date().toISOString(),
      version: 1,
      code: [
        {
          code_namespace: "code-a",
          path: "src/a.py",
          symbol: "a::Reader::read",
          state: "changed",
          changed_since: "abc1234def",
          suggestion: "Reader.read returns bytes now",
        },
        { code_namespace: "code-a", path: "docs/b.md", symbol: "", state: "fresh", changed_since: "" },
      ],
    } as MemoryDetail);
    open();
    await screen.findByText("Linked code");
    expect(screen.getByText("a::Reader::read")).toBeInTheDocument();
    expect(screen.getByText("Code changed since abc1234")).toBeInTheDocument();
    expect(screen.getByText("Suggested: Reader.read returns bytes now")).toBeInTheDocument();
    expect(screen.getByText("docs/b.md")).toBeInTheDocument();
  });

  it("Last recalled counts every agent read that served the key, apart from the ranking credit", async () => {
    const base = {
      found: true,
      value: "hello",
      memory_type: "semantic",
      confidence: 1,
      pinned: false,
      tags: [],
      created_at: new Date().toISOString(),
      version: 1,
    };
    // A context pack served it twice: no ranking credit, yet recalled.
    vi.mocked(getMemory).mockResolvedValueOnce({
      ...base,
      recall_count: 0,
      last_recalled_at: null,
      served_count: 2,
      last_served_at: new Date().toISOString(),
    } as MemoryDetail);
    open();
    await screen.findByText("hello");
    expect(screen.getByText(/^2× · /)).toBeInTheDocument();
    expect(screen.getByText("Recall credit")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
    expect(screen.queryByText("No agent read recorded")).not.toBeInTheDocument();
    cleanup();
    // Nothing served it.
    vi.mocked(getMemory).mockResolvedValueOnce({ ...base, recall_count: 0, served_count: 0 } as MemoryDetail);
    open();
    await screen.findByText("hello");
    expect(screen.getByText("No agent read recorded")).toBeInTheDocument();
    cleanup();
    // A bulk import seeded a count with no time and no events.
    vi.mocked(getMemory).mockResolvedValueOnce({
      ...base,
      recall_count: 9,
      last_recalled_at: null,
      served_count: 9,
      last_served_at: null,
    } as MemoryDetail);
    open();
    await screen.findByText("hello");
    expect(screen.getAllByText("9×")).toHaveLength(2); // Last recalled and Recall credit
  });

  it("a saved edit re-reads the served figures, so Last recalled keeps its count", async () => {
    useStore.setState({ myNamespaces: [row("ns", "writer")], whoami: { ...viewer, roles: ["WRITER"] } });
    const served = {
      found: true,
      value: "hello",
      memory_type: "semantic",
      confidence: 1,
      pinned: false,
      tags: [],
      created_at: new Date().toISOString(),
      version: 1,
      recall_count: 0,
      served_count: 2,
      last_served_at: new Date().toISOString(),
    } as MemoryDetail;
    vi.mocked(getMemory).mockResolvedValueOnce(served).mockResolvedValueOnce(served);
    open();
    await screen.findByText(/^2× · /);
    fireEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Save new version/ }));
    await waitFor(() => expect(getMemory).toHaveBeenCalledTimes(2));
    for (const call of vi.mocked(getMemory).mock.calls) {
      expect(call[2]).toMatchObject({ include_recall_breakdown: true });
    }
    expect(await screen.findByText(/^2× · /)).toBeInTheDocument();
  });

  it("opening History reads it once and shows the versions; a second open reads nothing", async () => {
    open();
    await screen.findByText("hello");
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    await waitFor(() => expect(memoryHistory).toHaveBeenCalledTimes(1));
    await screen.findByText(/2 versions/);
    fireEvent.click(screen.getByRole("button", { name: /History/ })); // close
    fireEvent.click(screen.getByRole("button", { name: /History/ })); // open again
    await screen.findByText(/2 versions/);
    expect(memoryHistory).toHaveBeenCalledTimes(1);
    expect(memoryRelated).not.toHaveBeenCalled();
  });

  it("an open section is remembered for the session and reads on the next card", async () => {
    const first = open();
    await screen.findByText("hello");
    fireEvent.click(screen.getByRole("button", { name: /Related/ }));
    await waitFor(() => expect(memoryRelated).toHaveBeenCalledTimes(1));
    first.unmount();
    open("k9");
    await waitFor(() => expect(memoryRelated).toHaveBeenCalledTimes(2));
    expect(memoryRelated).toHaveBeenLastCalledWith(identity, "k9", 5);
    expect(memoryHistory).not.toHaveBeenCalled();
  });

  it("the tag vocabulary is read when the add-tag input is focused, not on open", async () => {
    // The vocabulary's source is sharedTagVocabulary's business (the taxonomy, not a
    // 500-row memory.list); this drawer's rule is when it is asked for.
    const vocabularyReads = () => vi.mocked(getTaxonomy).mock.calls.length + vi.mocked(listMemories).mock.calls.length;
    open();
    await screen.findByText("hello");
    expect(vocabularyReads()).toBe(0);
    fireEvent.focusIn(screen.getByLabelText("Add a tag"));
    await waitFor(() => expect(vocabularyReads()).toBe(1));
  });

  it("a read for the key it moved off never lands under the new key", async () => {
    // k1's memory.get resolves after k2's: the drawer keeps k2's value.
    const memory = (value: string): MemoryDetail => ({
      found: true,
      value,
      memory_type: "semantic",
      confidence: null,
      pinned: false,
      tags: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      version: 1,
    });
    let resolveK1: (m: MemoryDetail) => void = () => {};
    vi.mocked(getMemory)
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            resolveK1 = r;
          }),
      )
      .mockImplementationOnce(async () => memory("value of k2"));
    const drawer = open("k1");
    drawer.rerender(<MemoryInspector identity={identity} memoryKey="k2" onClose={() => {}} onChanged={() => {}} />);
    await screen.findByText("value of k2");
    await act(async () => resolveK1(memory("value of k1")));
    expect(screen.queryByText("value of k1")).not.toBeInTheDocument();
    expect(screen.getByText("value of k2")).toBeInTheDocument();
  });
});
