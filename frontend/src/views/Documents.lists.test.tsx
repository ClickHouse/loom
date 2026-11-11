import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { ApiError } from "../lib/errors";
import { Documents } from "./Documents";

// The Library against the list rules: a row's actions in one always-visible More menu,
// Superseded as a neutral version state, the namespace's figures in the description
// line instead of a footer, the toolbar in every state with the one state pattern
// under it, the `tag:` filter said once, in the field, and the selection.

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  downloadKnowledge: vi.fn(async () => undefined),
  deleteKnowledge: vi.fn(async () => ({})),
  getKnowledge: vi.fn(async () => ({ found: true, content: "text" })),
  fetchKnowledgePreview: vi.fn(async () => null),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

const doc = (over: Record<string, unknown>) => ({
  description: "",
  tags: [],
  ingested_at: "2026-09-28T12:00:00Z",
  chunk_count: 3,
  superseded: false,
  has_blob: false,
  blob_size: 0,
  blob_content_type: "",
  original_filename: "",
  ...over,
});
const ROWS = [
  doc({
    source_uri: "docs/refunds.md",
    title: "Refund escalation",
    description: "Who approves a refund.",
    tags: ["billing"],
    chunk_count: 48,
    has_blob: true,
    blob_size: 1_900_000,
    blob_content_type: "application/pdf",
    original_filename: "Refund escalation v7.pdf",
  }),
  doc({ source_uri: "https://wiki.example/oncall", title: "", chunk_count: 3 }),
  doc({ source_uri: "docs/ingest-v2.md", title: "Ingest v2 spec", chunk_count: 12, superseded: true }),
];

const toolbar = () => screen.getByLabelText("Search and filters");

describe("Library list", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: ROWS, next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "knowledge" });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null });
  });

  it("gives every row one More menu, visible without a hover, holding its actions", async () => {
    render(<Documents />);
    await screen.findByText("Refund escalation");
    expect(screen.getAllByRole("button", { name: /^Actions for / })).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "Actions for Refund escalation" }));
    const menu = await screen.findByRole("menu", { name: "Actions for Refund escalation" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Preview", "Download PDF, 1.8 MB", "Replace content", "Edit details", "Copy URI", "Delete"]);
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Download PDF, 1.8 MB" }));
    expect(api.downloadKnowledge).toHaveBeenCalledWith(
      { org: "acme", namespace: "team" },
      "docs/refunds.md",
      "Refund escalation v7.pdf",
    );
  });

  it("names a text-only document's download for the Markdown it saves", async () => {
    render(<Documents />);
    await screen.findByText("Ingest v2 spec");
    await userEvent.click(screen.getByRole("button", { name: "Actions for Ingest v2 spec" }));
    expect(await screen.findByRole("menuitem", { name: "Download text (.md)" })).toBeInTheDocument();
  });

  it("draws Superseded as a neutral badge and the row's chunks and time on its first line", async () => {
    render(<Documents />);
    const badge = await screen.findByText("Superseded");
    expect(badge.closest('[class*="state_neutral"]')).not.toBeNull();
    const row = screen.getByText("Refund escalation").closest("[data-doc-row]") as HTMLElement;
    expect(row.querySelector("time")).not.toBeNull();
    expect(row).toHaveTextContent("48 chunks");
    // An untitled document falls back to its URI, as code.
    expect(screen.getByText("https://wiki.example/oncall").tagName).toBe("CODE");
  });

  it("says the library's figures in the description line, with no footer", async () => {
    render(<Documents />);
    expect(await screen.findByText("3 documents · 63 chunks · 1.8 MB")).toBeInTheDocument();
    expect(screen.queryByText(/^Showing/)).toBeNull();
    expect(toolbar()).toHaveTextContent("3 shown");
  });

  it("keeps the toolbar up while the first page loads", () => {
    api.listKnowledge.mockImplementation(() => new Promise(() => {}));
    render(<Documents />);
    expect(screen.getByLabelText("Search library")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading documents" })).toBeInTheDocument();
  });

  it("says a failed read with its request id and a Retry, never the empty library or a count", async () => {
    api.listKnowledge.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "INTERNAL", message: "ClickHouse timed out", requestId: "rid-7" }),
    );
    render(<Documents />);
    expect(await screen.findByText("Couldn't load documents")).toBeInTheDocument();
    expect(screen.getByText("rid-7").tagName).toBe("CODE");
    expect(screen.queryByText("Your library is empty")).toBeNull();
    expect(toolbar()).not.toHaveTextContent(/\d/);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Refund escalation")).toBeInTheDocument();
  });

  it("shows one Add document while the library is empty: the empty state's", async () => {
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    render(<Documents />);
    expect(await screen.findByText("Your library is empty")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Add document/ })).toHaveLength(1);
    expect(screen.getByText(/your agents search it, and you can download the original/)).toBeInTheDocument();
    expect(screen.queryByText(/knowledge\.search/)).toBeNull();
  });

  it("says a tag filter once, in the field, and a filter that matches nothing on one line", async () => {
    render(<Documents />);
    await screen.findByText("Refund escalation");
    await userEvent.type(screen.getByLabelText("Search library"), "tag:billing");
    await waitFor(() => expect(screen.queryByText("Ingest v2 spec")).toBeNull());
    expect(toolbar()).toHaveTextContent("1 match");
    expect(within(toolbar()).queryByText("tag:billing")).toBeNull();
    expect(screen.queryByText(/Filtering:/)).toBeNull();
    await userEvent.type(screen.getByLabelText("Search library"), " zzz");
    expect(await screen.findByText((_, el) => el?.textContent === "No documents match tag:billing zzz")).toBeVisible();
    await userEvent.click(screen.getAllByRole("button", { name: "Clear filters" }).at(-1) as HTMLElement);
    expect(await screen.findByText("Ingest v2 spec")).toBeInTheDocument();
  });

  it("selects a Shift-click range and every row on the page, and clears on Escape", async () => {
    render(<Documents />);
    await screen.findByText("Refund escalation");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "Select Refund escalation" }));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByRole("checkbox", { name: "Select https://wiki.example/oncall" }));
    await user.keyboard("{/Shift}");
    expect(toolbar()).toHaveTextContent("2 selected");
    await user.click(within(toolbar()).getByRole("checkbox", { name: "Select all 3 on this page" }));
    expect(toolbar()).toHaveTextContent("3 selected");
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("{Escape}");
    expect(toolbar()).not.toHaveTextContent("selected");
  });

  // The free-text filter and the sort run over the documents loaded so far: while the cursor
  // holds more, "No documents match" and the count say they cover the loaded ones.
  it("says a search covers only the loaded documents while more are unread", async () => {
    api.listKnowledge.mockResolvedValue({ rows: ROWS, next_cursor: "c-2" });
    render(<Documents />);
    await screen.findByText("Refund escalation");
    await userEvent.type(screen.getByLabelText("Search library"), "refund");
    await waitFor(() => expect(toolbar()).toHaveTextContent("1 match in the first 3"));
    await userEvent.type(screen.getByLabelText("Search library"), " zzz");
    expect(
      await screen.findByText((_, el) => el?.textContent === "No documents match refund zzz in the first 3 loaded"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  // The last page takes Load more with it: focus goes to the document that page added,
  // wherever the sort puts it, not to the row at the old length.
  it("moves focus to the document the last page added when Load more goes", async () => {
    api.listKnowledge.mockResolvedValueOnce({ rows: ROWS, next_cursor: "c-2" }).mockResolvedValueOnce({
      rows: [doc({ source_uri: "docs/new.md", title: "Newest note", ingested_at: "2026-10-01T12:00:00Z" })],
      next_cursor: null,
    });
    render(<Documents />);
    await screen.findByText("Refund escalation");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Newest note");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    const added = screen.getByText("Newest note").closest("[data-doc-row]") as HTMLElement;
    await waitFor(() => expect(added.querySelector("[data-row-open]")).toHaveFocus());
  });

  // The reload after a delete is dropped when the namespace changes while it is in flight,
  // so the old namespace's documents never draw under the new one.
  it("drops a reload after a delete that lands once the namespace has changed", async () => {
    let finishOld: (v: unknown) => void = () => {};
    let teamReads = 0;
    api.listKnowledge.mockImplementation(async (id: { namespace: string }) => {
      if (id.namespace === "other") return { rows: [doc({ source_uri: "docs/other.md", title: "Other doc" })] };
      teamReads += 1;
      if (teamReads === 1) return { rows: ROWS, next_cursor: null };
      return new Promise((resolve) => {
        finishOld = resolve;
      });
    });
    render(<Documents />);
    await screen.findByText("Refund escalation");
    await userEvent.click(screen.getByRole("button", { name: "Actions for Refund escalation" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(teamReads).toBe(2));
    act(() => useStore.setState({ identity: { org: "acme", namespace: "other", agent: "loom-ui" } }));
    expect(await screen.findByText("Other doc")).toBeInTheDocument();
    await act(async () => finishOld({ rows: ROWS, next_cursor: null }));
    expect(screen.queryByText("Refund escalation")).toBeNull();
    expect(screen.getByText("Other doc")).toBeInTheDocument();
  });
});
