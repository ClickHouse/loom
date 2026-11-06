/**
 * A repository's documents on its card — listed from the docs namespace with how often
 * agents opened each this week, a title opens the document (as the dashboard, never
 * counted as an agent), a writer adds one without typing an address and removes one
 * after a confirm; a refusal says "join".
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/errors";
import { slugify } from "../lib/docFiles";
import { axeViolations } from "../test/axe";
import { repoDocUri } from "../flows/RepoDocDialogs";
import { readsPhrase, RepoDocsList, REPO_DOCS_HINT } from "./RepoDocs";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listKnowledge: vi.fn(),
    deleteKnowledge: vi.fn(),
    knowledgeReads: vi.fn(),
    getKnowledge: vi.fn(),
    addKnowledge: vi.fn(),
    downloadKnowledge: vi.fn(),
    knowledgeFormats: vi.fn(async () => null),
    uploadKnowledgeFile: vi.fn(),
    ingestKnowledge: vi.fn(),
    knowledgeIngestStatus: vi.fn(),
  };
});
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
// `toast` is silenced; `toastError` and the viewport stay real, so a failure's
// rendered text (with its request id) is what a test reads.
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
}));
import { toast, ToastViewport } from "../components/Toast";
import { ingestClock } from "../lib/fileIngest";
import {
  addKnowledge,
  deleteKnowledge,
  downloadKnowledge,
  getKnowledge,
  ingestKnowledge,
  knowledgeIngestStatus,
  knowledgeReads,
  listKnowledge,
  uploadKnowledgeFile,
} from "../api";

const DOC = {
  source_uri: "repo-doc://Acme/kj/on-call-runbook",
  title: "On-call runbook",
  description: "Read before paging",
  tags: [],
  ingested_at: "2026-09-20T10:00:00Z",
  chunk_count: 3,
  superseded: false,
};
const OTHER = { ...DOC, source_uri: "repo-doc://Acme/kj/design", title: "Design note", description: "" };

function mount(writable = false) {
  return render(
    <main>
      <RepoDocsList org="dev" docsNamespace="code-kj" repo="Acme/kj" writable={writable} busy={false} />
    </main>,
  );
}

describe("the pure part", () => {
  it("says opened, never found, and makes a free address from the title", () => {
    expect(readsPhrase(undefined)).toBe("");
    expect(readsPhrase(0)).toBe("not opened by agents this week");
    expect(readsPhrase(12)).toBe("opened by agents 12× this week");
    expect(slugify("MergeTree — merge scheduling!")).toBe("mergetree-merge-scheduling");
    const taken = new Set(["repo-doc://Acme/kj/runbook", "repo-doc://Acme/kj/runbook-2"]);
    expect(repoDocUri("Acme/kj", "Runbook", taken)).toBe("repo-doc://Acme/kj/runbook-3");
    expect(repoDocUri("Acme/kj", "Design", taken)).toBe("repo-doc://Acme/kj/design");
  });
});

function repoStatus(state: string, more: Record<string, unknown> = {}) {
  return {
    found: true,
    ingestion_id: "job-1",
    source_uri: "repo-doc://Acme/kj/on-call",
    state,
    stage: "",
    error_code: "",
    error_message: "",
    format: "markdown",
    coverage: null,
    warnings: [],
    chunks: 2,
    revision: 1,
    ...more,
  };
}

async function addFile(name: string) {
  vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null });
  vi.mocked(uploadKnowledgeFile).mockResolvedValue({
    upload_id: "up-1",
    sha256: "",
    size: 9,
    filename: name,
    format: "markdown",
    expires_at: "",
  });
  vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "x" });
  mount(true);
  fireEvent.click(await screen.findByRole("button", { name: "Add a document" }));
  const dialog = await screen.findByRole("dialog");
  const user = userEvent.setup();
  await user.upload(within(dialog).getByLabelText("Choose a file"), new File(["# R"], name));
  return { dialog, user };
}

describe("RepoDocsList", () => {
  beforeEach(() => {
    for (const f of [listKnowledge, deleteKnowledge, knowledgeReads, getKnowledge, addKnowledge, downloadKnowledge])
      vi.mocked(f).mockReset();
    vi.mocked(knowledgeReads).mockResolvedValue({
      window_days: 7,
      reads: [{ source_uri: DOC.source_uri, reads: 5, agents: 2, last_read_at: "" }],
    });
  });
  afterEach(cleanup);

  it("lists the documents with the fields it renders and each one's agent opens this week", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [DOC, OTHER], next_cursor: null });
    mount();
    expect(await screen.findByRole("button", { name: "On-call runbook" })).toBeInTheDocument();
    expect(await screen.findByText("opened by agents 5× this week")).toBeInTheDocument();
    expect(screen.getByText("not opened by agents this week")).toBeInTheDocument();
    expect(screen.getByText(REPO_DOCS_HINT)).toBeInTheDocument();
    const [id, opts] = vi.mocked(listKnowledge).mock.calls[0];
    expect(id).toEqual({ org: "dev", namespace: "code-kj" });
    expect(opts).toMatchObject({
      limit: 21,
      fields: ["source_uri", "title", "description", "ingested_at", "has_blob", "original_filename"],
    });
    expect(vi.mocked(knowledgeReads).mock.calls[0][1]).toMatchObject({
      days: 7,
      source_uris: [DOC.source_uri, OTHER.source_uri],
    });
    expect(screen.queryByRole("button", { name: /Add a document|Remove/ })).toBeNull(); // a reader gets no verb
    expect(await axeViolations(document.body)).toEqual([]);
  });

  // A document's "added" time is the list's one time format, quiet in the row (no tab stop of
  // its own).
  it("draws each document's added time with ListTime, without a tab stop", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [DOC], next_cursor: null });
    mount();
    await screen.findByRole("button", { name: "On-call runbook" });
    const time = within(screen.getByRole("list", { name: "Repository documents" })).getByText(
      (_, el) => el?.tagName === "TIME",
    );
    expect(time).toHaveAttribute("dateTime", "2026-09-20T10:00:00.000Z");
    expect(time).not.toHaveAttribute("tabindex");
  });

  it("a failed read-count call shows the list without them", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [DOC], next_cursor: null });
    vi.mocked(knowledgeReads).mockRejectedValue(new ApiError({ status: 500, code: "INTERNAL", message: "no" }));
    mount();
    expect(await screen.findByRole("button", { name: "On-call runbook" })).toBeInTheDocument();
    await waitFor(() => expect(knowledgeReads).toHaveBeenCalled());
    expect(screen.queryByText(/opened by agents/)).toBeNull();
  });

  it("a title opens the document as the dashboard — never counted as an agent read", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [DOC], next_cursor: null });
    vi.mocked(getKnowledge).mockResolvedValue({
      found: true,
      source_uri: DOC.source_uri,
      content: "Page the owner first.",
    });
    mount();
    const title = await screen.findByRole("button", { name: "On-call runbook" });
    // A Click UI link at the list's 14 px.
    expect(title.className).toMatch(/\blink_size_md/);
    fireEvent.click(title);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Page the owner first.")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("opened by agents 5 times in 7 days");
    // The request path names every dashboard call `loom-ui`, so a preview is not an agent read.
    expect(vi.mocked(getKnowledge).mock.calls[0]).toEqual([{ org: "dev", namespace: "code-kj" }, DOC.source_uri]);
    expect(within(dialog).getByRole("button", { name: /Download/ })).toBeInTheDocument();
  });

  it("a failed download names its request id and keeps the preview on screen", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [DOC], next_cursor: null });
    vi.mocked(getKnowledge).mockResolvedValue({
      found: true,
      source_uri: DOC.source_uri,
      content: "Page the owner first.",
    });
    vi.mocked(downloadKnowledge).mockRejectedValue(
      new ApiError({ status: 500, code: "INTERNAL", message: "blob store down", requestId: "r-1" }),
    );
    render(<ToastViewport />);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "On-call runbook" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Page the owner first.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /Download/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/^Download failed: /);
    expect(alert).toHaveTextContent("request id r-1");
    expect(within(dialog).getByText("Page the owner first.")).toBeInTheDocument();
  });

  it("a caller the repository refuses is told to join", async () => {
    vi.mocked(listKnowledge).mockRejectedValue(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" }));
    mount(true);
    expect(await screen.findByText("Join the repository to read its documents.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a document" })).toBeNull();
  });

  it("a writer removes a document after confirming, and the list re-reads", async () => {
    vi.mocked(listKnowledge)
      .mockResolvedValueOnce({ rows: [DOC], next_cursor: null })
      .mockResolvedValueOnce({ rows: [], next_cursor: null });
    vi.mocked(deleteKnowledge).mockResolvedValue(undefined);
    mount(true);
    fireEvent.click(await screen.findByRole("button", { name: "Remove On-call runbook" }));
    await waitFor(() =>
      expect(deleteKnowledge).toHaveBeenCalledWith({ org: "dev", namespace: "code-kj" }, DOC.source_uri),
    );
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument();
  });

  it("a writer adds pasted text under an address made from the title, never replacing one", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({
      rows: [{ ...DOC, source_uri: "repo-doc://Acme/kj/runbook" }],
      next_cursor: null,
    });
    vi.mocked(addKnowledge).mockResolvedValue({
      doc_id: "d",
      chunks_created: 1,
      embedding_model: "m",
      embedding_dim: 1,
    });
    mount(true);
    fireEvent.click(await screen.findByRole("button", { name: "Add a document" }));
    const dialog = await screen.findByRole("dialog");
    expect(await axeViolations(document.body)).toEqual([]);
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole("tab", { name: "Paste text" }));
    await user.type(within(dialog).getByLabelText(/^Text/), "Step 1: page the owner.");
    await user.type(within(dialog).getByLabelText("Title"), "Runbook");
    expect(dialog).toHaveTextContent("Saved as repo-doc://Acme/kj/runbook-2");
    fireEvent.click(within(dialog).getByRole("button", { name: "Add document" }));
    await waitFor(() => expect(addKnowledge).toHaveBeenCalled());
    expect(vi.mocked(addKnowledge).mock.calls[0]).toEqual([
      { org: "dev", namespace: "code-kj" },
      {
        source_uri: "repo-doc://Acme/kj/runbook-2",
        content: "Step 1: page the owner.",
        title: "Runbook",
        description: "",
        upsert: false,
      },
    ]);
  });

  it("a writer adds a file as its bytes, ingested under an address made from its name", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null });
    vi.mocked(uploadKnowledgeFile).mockResolvedValue({
      upload_id: "up-1",
      sha256: "",
      size: 9,
      filename: "on_call.md",
      format: "markdown",
      expires_at: "",
    });
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "x" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue({
      found: true,
      ingestion_id: "job-1",
      source_uri: "repo-doc://Acme/kj/on-call",
      state: "ready",
      stage: "",
      error_code: "",
      error_message: "",
      format: "markdown",
      coverage: null,
      warnings: [],
      chunks: 2,
      revision: 1,
    });
    mount(true);
    fireEvent.click(await screen.findByRole("button", { name: "Add a document" }));
    const dialog = await screen.findByRole("dialog");
    const user = userEvent.setup();
    await user.upload(within(dialog).getByLabelText("Choose a file"), new File(["# On call"], "on_call.md"));
    expect(within(dialog).getByLabelText("Title")).toHaveValue("on call");
    fireEvent.click(within(dialog).getByRole("button", { name: "Add document" }));
    await waitFor(() => expect(knowledgeIngestStatus).toHaveBeenCalled());
    expect(vi.mocked(uploadKnowledgeFile).mock.calls[0][1].name).toBe("on_call.md");
    expect(vi.mocked(ingestKnowledge).mock.calls[0]).toEqual([
      { org: "dev", namespace: "code-kj" },
      {
        source_uri: "repo-doc://Acme/kj/on-call",
        title: "on call",
        description: "",
        allow_partial: false,
        expected_revision: 0,
        upload_id: "up-1",
      },
    ]);
    expect(addKnowledge).not.toHaveBeenCalled();
  });

  it("a file whose address another document took past the first page is refused, not published over it", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null });
    vi.mocked(uploadKnowledgeFile).mockResolvedValue({
      upload_id: "up-1",
      sha256: "",
      size: 9,
      filename: "runbook.md",
      format: "markdown",
      expires_at: "",
    });
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "x" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue({
      found: true,
      ingestion_id: "job-1",
      source_uri: "repo-doc://Acme/kj/runbook",
      state: "failed",
      stage: "",
      error_code: "revision_conflict",
      error_message: "another revision of the source is live",
      format: "markdown",
      coverage: null,
      warnings: [],
      chunks: 0,
      revision: 0,
    });
    mount(true);
    fireEvent.click(await screen.findByRole("button", { name: "Add a document" }));
    const dialog = await screen.findByRole("dialog");
    const user = userEvent.setup();
    await user.upload(within(dialog).getByLabelText("Choose a file"), new File(["# R"], "runbook.md"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add document" }));
    await waitFor(() =>
      expect(screen.getByRole("dialog")).toHaveTextContent(
        "runbook.md: Failed (revision_conflict): another revision of the source is live",
      ),
    );
    expect(vi.mocked(ingestKnowledge).mock.calls[0][1].expected_revision).toBe(0);
  });

  it("says a partial file publish is partial, naming what it lost", async () => {
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(
      repoStatus("partial", { coverage: { lost: ["formula_values"] } }),
    );
    const { dialog } = await addFile("on_call.md");
    fireEvent.click(within(dialog).getByRole("button", { name: "Add document" }));
    await waitFor(() =>
      expect(vi.mocked(toast).mock.calls.at(-1)).toEqual([
        "Added on call, partial: part of the file could not be read (lost: formula_values)",
        "info",
      ]),
    );
  });

  it("holds the dialog through the upload; closing while it follows says the job goes on", async () => {
    let release = () => {};
    ingestClock.sleep = () => new Promise<void>((r) => (release = r));
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(repoStatus("processing", { stage: "parse" }));
    try {
      const { dialog, user } = await addFile("on_call.md");
      fireEvent.click(within(dialog).getByRole("button", { name: "Add document" }));
      expect(await within(dialog).findByText("Extracting…")).toBeInTheDocument();
      const polls = vi.mocked(knowledgeIngestStatus).mock.calls.length;
      // The footer's Close and the dialog's own close both close it the same way.
      await user.click(screen.getAllByRole("button", { name: "Close" })[0]);
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(vi.mocked(toast).mock.calls.at(-1)?.[0]).toMatch(/on call goes on extracting/);
      release();
      await new Promise((r) => setTimeout(r, 20));
      expect(vi.mocked(knowledgeIngestStatus).mock.calls.length).toBe(polls);
    } finally {
      ingestClock.sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    }
  });

  it("says a dropped connection may have committed, in file and paste mode alike", async () => {
    const { dialog } = await addFile("on_call.md");
    vi.mocked(uploadKnowledgeFile).mockRejectedValue(new TypeError("Failed to fetch"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Add document" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent(/Lost connection to Loom mid-request/));

    cleanup();
    vi.mocked(addKnowledge).mockRejectedValue(new TypeError("Failed to fetch"));
    mount(true);
    fireEvent.click(await screen.findByRole("button", { name: "Add a document" }));
    const pasting = await screen.findByRole("dialog");
    const user = userEvent.setup();
    await user.click(within(pasting).getByRole("tab", { name: "Paste text" }));
    await user.type(within(pasting).getByLabelText(/^Text/), "Step 1.");
    await user.type(within(pasting).getByLabelText("Title"), "Runbook");
    fireEvent.click(within(pasting).getByRole("button", { name: "Add document" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent(/Lost connection to Loom mid-request/));
  });
});
