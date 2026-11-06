// The open document is the Library's URL: a preview writes `?doc=` into the entry (no new
// entry), closing drops it, and a `?doc=` link opens the preview on arrival.
import { act, cleanup, fireEvent, render, screen, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  getKnowledge: vi.fn(),
  fetchKnowledgePreview: vi.fn(),
  searchKnowledge: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const toastError = vi.hoisted(() => vi.fn());
const toast = vi.hoisted(() => vi.fn());
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast,
  toastError,
}));

const ROWS = [
  {
    source_uri: "docs/handbook.md",
    title: "Handbook",
    original_filename: "handbook.md",
    description: "",
    tags: [],
    has_blob: false,
  },
];

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

describe("Library keeps the open document in its URL", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: ROWS, next_cursor: null });
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "docs/handbook.md" });
    api.fetchKnowledgePreview.mockResolvedValue(null);
    useStore.setState({
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "knowledge",
      pendingDocSourceUri: null,
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("a preview writes ?doc=, closing drops it, and neither adds an entry", async () => {
    history.replaceState(null, "", "/library");
    const before = history.length;
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Preview Handbook" }));
    await settle();
    expect(new URLSearchParams(location.search).get("doc")).toBe("docs/handbook.md");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await settle();
    expect(location.search).toBe("");
    expect(history.length).toBe(before);
  });

  it("a ?doc= link opens the preview on arrival", async () => {
    history.replaceState(null, "", "/library?doc=docs%2Fhandbook.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(screen.getByRole("dialog", { name: "Handbook" })).toBeInTheDocument();
  });

  it("a ?doc= link to a document past the first page reads it and opens the preview", async () => {
    const late = { ...ROWS[0], source_uri: "docs/late.md", title: "Late doc" };
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) =>
      opts.uriPrefix ? { rows: [late], next_cursor: null } : { rows: ROWS, next_cursor: "c-2" },
    );
    history.replaceState(null, "", "/library?doc=docs%2Flate.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ uriPrefix: ["docs/late.md"] }),
    );
    expect(screen.getByRole("dialog", { name: "Late doc" })).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("doc")).toBe("docs/late.md");
  });

  it("reads a linked document page by page until it finds the exact one", async () => {
    const late = { ...ROWS[0], source_uri: "notes", title: "Notes" };
    const newer = { ...ROWS[0], source_uri: "notes/a.md", title: "A" };
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[]; cursor?: string }) => {
      if (!opts.uriPrefix) return { rows: ROWS, next_cursor: "c-2" };
      return opts.cursor ? { rows: [late], next_cursor: null } : { rows: [newer], next_cursor: "p-2" };
    });
    history.replaceState(null, "", "/library?doc=notes");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(screen.getByRole("dialog", { name: "Notes" })).toBeInTheDocument();
  });

  it("a failed read of a linked document says so and keeps the link", async () => {
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) => {
      if (opts.uriPrefix) throw new Error("down");
      return { rows: ROWS, next_cursor: "c-2" };
    });
    history.replaceState(null, "", "/library?doc=docs%2Flate.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(toastError).toHaveBeenCalledWith("Open failed", expect.any(Error));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(new URLSearchParams(location.search).get("doc")).toBe("docs/late.md");
  });

  it("a linked document's read that lands after the page closed opens nothing and leaves the URL alone", async () => {
    let answer: (v: unknown) => void = () => {};
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) =>
      opts.uriPrefix ? new Promise((r) => (answer = r)) : { rows: ROWS, next_cursor: "c-2" },
    );
    history.replaceState(null, "", "/library?doc=docs%2Flate.md");
    useStore.getState().bootView("knowledge");
    const view = render(<Documents />);
    await settle();
    view.unmount();
    history.replaceState(null, "", "/memories");
    await act(async () =>
      answer({ rows: [{ ...ROWS[0], source_uri: "docs/late.md", title: "Late doc" }], next_cursor: null }),
    );
    expect(api.getKnowledge).not.toHaveBeenCalled();
    expect(location.search).toBe("");
  });

  it("a linked document's read that lands after the reader opened another opens nothing", async () => {
    let answer: (v: unknown) => void = () => {};
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) =>
      opts.uriPrefix ? new Promise((r) => (answer = r)) : { rows: ROWS, next_cursor: "c-2" },
    );
    history.replaceState(null, "", "/library?doc=docs%2Flate.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Preview Handbook" }));
    await settle();
    await act(async () =>
      answer({ rows: [{ ...ROWS[0], source_uri: "docs/late.md", title: "Late doc" }], next_cursor: null }),
    );
    await settle();
    expect(screen.getByRole("dialog", { name: "Handbook" })).toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("doc")).toBe("docs/handbook.md");
  });

  it("a linked document's read that lands after the reader closed the preview opens nothing", async () => {
    let answer: (v: unknown) => void = () => {};
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) =>
      opts.uriPrefix ? new Promise((r) => (answer = r)) : { rows: ROWS, next_cursor: "c-2" },
    );
    history.replaceState(null, "", "/library");
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Preview Handbook" }));
    await settle();
    // Back reaches an entry naming a document past the loaded rows; its read is out.
    act(() => useStore.setState({ pendingDocSourceUri: "docs/late.md" }));
    await settle();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await settle();
    api.getKnowledge.mockClear();
    await act(async () =>
      answer({ rows: [{ ...ROWS[0], source_uri: "docs/late.md", title: "Late doc" }], next_cursor: null }),
    );
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.getKnowledge).not.toHaveBeenCalled();
    expect(location.search).toBe("");
  });

  it("a linked document's read that lands after a namespace switch opens nothing", async () => {
    let answer: (v: unknown) => void = () => {};
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: { uriPrefix?: string[] }) => {
      if (opts.uriPrefix) return new Promise((r) => (answer = r));
      return id.namespace === "team" ? { rows: ROWS, next_cursor: "c-2" } : { rows: [], next_cursor: null };
    });
    history.replaceState(null, "", "/library?doc=docs%2Flate.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    act(() => useStore.setState({ identity: { org: "acme", namespace: "other", agent: "loom-ui" } }));
    await settle();
    await act(async () =>
      answer({ rows: [{ ...ROWS[0], source_uri: "docs/late.md", title: "Late doc" }], next_cursor: null }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.getKnowledge).not.toHaveBeenCalled();
  });

  it("a ?doc= link to a document the namespace does not hold opens nothing and leaves the URL", async () => {
    history.replaceState(null, "", "/library?doc=docs%2Fgone.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(location.search).toBe("?namespace=team");
  });

  it("a passage found inside the documents opens its document, cited by revision and place", async () => {
    history.replaceState(null, "", "/library");
    api.searchKnowledge.mockResolvedValue([
      {
        source_uri: "docs/handbook.md",
        chunk_idx: 2,
        content: "Escalate to the on-call lead.",
        section: "On call",
        score: 1,
        revision: 2,
        format: "markdown",
        block_type: "paragraph",
        locator: { line_start: 12, line_end: 18 },
        keys_matched: [],
      },
    ]);
    render(<Documents />);
    await settle();
    // Folded until its toggle opens it, which then names the panel it controls.
    const toggle = screen.getAllByRole("button", { name: "Find a passage" })[0];
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Passage to find")).toBeNull();
    fireEvent.click(toggle);
    expect(document.getElementById(toggle.getAttribute("aria-controls") ?? "")).toContainElement(
      screen.getByLabelText("Passage to find"),
    );
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "who to escalate to" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    await settle();
    expect(api.searchKnowledge.mock.calls[0].slice(0, 2)).toEqual([
      { org: "acme", namespace: "team" },
      "who to escalate to",
    ]);
    const list = screen.getByRole("list", { name: "Passages found" });
    expect(list).toHaveTextContent("revision 2, lines 12–18");
    fireEvent.click(within(list).getByRole("button"));
    await settle();
    expect(new URLSearchParams(location.search).get("doc")).toBe("docs/handbook.md");
    expect(api.getKnowledge).toHaveBeenCalled();
  });

  async function findPassageIn(uri: string) {
    api.searchKnowledge.mockResolvedValue([
      { source_uri: uri, chunk_idx: 0, content: "A passage.", section: "", score: 1 },
    ]);
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "passage" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    await settle();
    fireEvent.click(within(screen.getByRole("list", { name: "Passages found" })).getByRole("button"));
    await settle();
  }

  it("the arrows move through the list focus is in, the passages or the documents", async () => {
    history.replaceState(null, "", "/library");
    const second = { ...ROWS[0], source_uri: "docs/runbook.md", title: "Runbook" };
    api.listKnowledge.mockResolvedValue({ rows: [...ROWS, second], next_cursor: null });
    api.searchKnowledge.mockResolvedValue([
      { source_uri: "docs/handbook.md", chunk_idx: 0, content: "One.", section: "", score: 1 },
      { source_uri: "docs/runbook.md", chunk_idx: 0, content: "Two.", section: "", score: 1 },
    ]);
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "passage" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    await settle();
    const [hitOne, hitTwo] = within(screen.getByRole("list", { name: "Passages found" })).getAllByRole("button");
    act(() => hitOne.focus());
    fireEvent.keyDown(hitOne, { key: "ArrowDown" });
    expect(document.activeElement).toBe(hitTwo);
    const docOne = screen.getByRole("button", { name: "Preview Handbook" });
    act(() => docOne.focus());
    fireEvent.keyDown(docOne, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Preview Runbook" }));
  });

  it("another namespace folds the passages, and a list that failed to load shows none", async () => {
    history.replaceState(null, "", "/library");
    api.searchKnowledge.mockResolvedValue([]);
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    expect(screen.getByLabelText("Passage to find")).toBeInTheDocument();
    act(() => useStore.setState({ identity: { org: "acme", namespace: "ops", agent: "loom-ui" } }));
    await settle();
    expect(screen.getAllByRole("button", { name: "Find a passage" })[0]).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Passage to find")).toBeNull();
    // A later read of the list that fails keeps the passages and what they found.
    api.searchKnowledge.mockResolvedValue([
      { source_uri: "docs/handbook.md", chunk_idx: 0, content: "One.", section: "", score: 1 },
    ]);
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "passage" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    await settle();
    api.listKnowledge.mockRejectedValueOnce(new Error("boom"));
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "tag:billing" } });
    expect(await screen.findByText(/Couldn.t load documents/)).toBeInTheDocument();
    expect(api.listKnowledge).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ tags: ["billing"] }),
    );
    expect(screen.getByRole("list", { name: "Passages found" })).toBeInTheDocument();
    api.listKnowledge.mockRejectedValue(new Error("boom"));
    act(() => useStore.setState({ identity: { org: "acme", namespace: "broken", agent: "loom-ui" } }));
    await settle();
    expect(screen.queryByRole("button", { name: "Find a passage" })).toBeNull();
  });

  it("a passage in a document past the first page reads that document and opens it", async () => {
    history.replaceState(null, "", "/library");
    const late = { ...ROWS[0], source_uri: "docs/late.md", title: "Late doc" };
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) =>
      opts.uriPrefix ? { rows: [late], next_cursor: null } : { rows: ROWS, next_cursor: "c-2" },
    );
    await findPassageIn("docs/late.md");
    expect(api.listKnowledge).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ uriPrefix: ["docs/late.md"] }),
    );
    expect(screen.getByRole("dialog", { name: "Late doc" })).toBeInTheDocument();
  });

  it("a passage whose document is gone says so and opens nothing", async () => {
    history.replaceState(null, "", "/library");
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: { uriPrefix?: string[] }) =>
      opts.uriPrefix ? { rows: [], next_cursor: null } : { rows: ROWS, next_cursor: null },
    );
    await findPassageIn("docs/gone.md");
    expect(toast).toHaveBeenCalledWith("docs/gone.md is no longer in this library", "info");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(new URLSearchParams(location.search).get("doc")).toBeNull();
  });
});
