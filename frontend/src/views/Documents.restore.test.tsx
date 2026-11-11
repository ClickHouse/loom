import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { toast, ToastViewport } from "../components/Toast";
import { Documents } from "./Documents";
import { axeViolations } from "../test/axe";
import { ApiError } from "../lib/errors";

// A delete's notice carries Undo, which calls knowledge.restore for the place the document
// was deleted from, and says why when nothing came back.

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  deleteKnowledge: vi.fn(async () => undefined),
  restoreKnowledge: vi.fn(),
  getKnowledge: vi.fn(async () => ({ found: true, content: "text" })),
  fetchKnowledgePreview: vi.fn(async () => null),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

const ROW = {
  source_uri: "docs/refunds.md",
  title: "Refund escalation",
  description: "",
  tags: [],
  ingested_at: "2026-09-28T12:00:00Z",
  chunk_count: 3,
  superseded: false,
  has_blob: false,
  blob_size: 0,
  blob_content_type: "",
  original_filename: "",
};

async function deleteRow() {
  render(
    <>
      <ToastViewport />
      <Documents />
    </>,
  );
  await screen.findByText("Refund escalation");
  await userEvent.click(screen.getByRole("button", { name: "Actions for Refund escalation" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
  return screen.findByRole("button", { name: "Undo" });
}

describe("Library delete Undo", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: [ROW], next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "knowledge" });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.useRealTimers();
    useStore.setState({ identity: null });
  });

  it("restores into the namespace the delete ran in and reads the list again", async () => {
    api.restoreKnowledge.mockResolvedValue({
      restored: true,
      reason: "",
      doc_id: "d1",
      revision: 2,
      chunks_restored: 3,
      original: "none",
    });
    const undo = await deleteRow();
    const reads = api.listKnowledge.mock.calls.length;
    act(() => useStore.setState({ identity: { org: "acme", namespace: "other", agent: "loom-ui" } }));
    await userEvent.click(undo);
    expect(api.restoreKnowledge).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, "docs/refunds.md");
    expect(await screen.findByText("Restored docs/refunds.md")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
    expect(screen.getByLabelText("Search library")).toHaveFocus();
    await waitFor(() => expect(api.listKnowledge.mock.calls.length).toBeGreaterThan(reads));
  });

  it("keeps Undo for a delete that committed with only lists and search behind", async () => {
    api.deleteKnowledge.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: { category: "dependency", retryable: false } }),
    );
    const undo = await deleteRow();
    expect(undo).toBeInTheDocument();
    expect(
      screen.getByText("Deleted docs/refunds.md; the list may show it for a moment, and Undo may need a moment too"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Delete failed/)).toBeNull();
    // Undo while the delete is still being recorded: refused for now, and the notice comes back
    // with its Undo, which works once the server has caught up.
    api.restoreKnowledge.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", detail: { category: "dependency", retryable: true } }),
    );
    await userEvent.click(undo);
    expect(
      await screen.findByText("docs/refunds.md is not restored yet; try Undo again in a moment"),
    ).toBeInTheDocument();
    api.restoreKnowledge.mockResolvedValueOnce({
      restored: true,
      reason: "",
      doc_id: "d1",
      revision: 2,
      chunks_restored: 3,
      original: "none",
    });
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("Restored docs/refunds.md")).toBeInTheDocument();
    expect(api.restoreKnowledge).toHaveBeenCalledTimes(2);
  });

  it("says why a refused restore left the document deleted", async () => {
    api.restoreKnowledge.mockResolvedValue({
      restored: false,
      reason: "not_restorable",
      doc_id: "d1",
      revision: 2,
      chunks_restored: 0,
      original: "none",
    });
    await userEvent.click(await deleteRow());
    expect(
      await screen.findByText("docs/refunds.md was not restored: an erasure reached it, or no delete retired it"),
    ).toBeInTheDocument();
  });

  it("replaces a document's Undo notice when it is deleted again", async () => {
    await deleteRow();
    await userEvent.click(screen.getByRole("button", { name: "Actions for Refund escalation" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(api.deleteKnowledge).toHaveBeenCalledTimes(2));
    expect(screen.getAllByRole("button", { name: "Undo" })).toHaveLength(1);
  });

  it("passes axe with the Undo notice showing", async () => {
    const undo = await deleteRow();
    expect(await axeViolations(undo.closest("[data-toast-viewport]") as HTMLElement)).toEqual([]);
  });

  it("keeps the Undo notice past the clock that takes a plain notice away", async () => {
    // Fake from the first render, so the cards' timers run on the clock advanced here.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await deleteRow();
    act(() => toast("Saved elsewhere"));
    expect(screen.getByText("Saved elsewhere")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.queryByText("Saved elsewhere")).toBeNull();
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
  });
});
