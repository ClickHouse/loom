// The open document is the Library's URL: a preview writes `?doc=` into the entry (no new
// entry), closing drops it, and a `?doc=` link opens the preview on arrival.
import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  getKnowledge: vi.fn(),
  fetchKnowledgeInlineUrl: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));

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
    api.fetchKnowledgeInlineUrl.mockResolvedValue(null);
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

  it("a ?doc= link to a document the page does not hold opens nothing and leaves the URL", async () => {
    history.replaceState(null, "", "/library?doc=docs%2Fgone.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(location.search).toBe("");
  });
});
