/**
 * The Library's add dialog: a picked file is badged with the format this deployment
 * reads it as, uploaded as its bytes and ingested, and its job followed until it
 * publishes; a format no extractor reads is refused before anything is sent; a job
 * that fails says why; pasted text is a text write.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  knowledgeFormats: vi.fn(),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
  addKnowledge: vi.fn(),
  cancelIngestion: vi.fn(),
  listKnowledge: vi.fn(),
}));
vi.mock("../lib/errorWords", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/errorWords")>()),
  toastCommitted: vi.fn(),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
  toastError: vi.fn(),
}));
import { toast, toastError } from "../components/Toast";
import { toastCommitted } from "../lib/errorWords";
import {
  type KnowledgeUpload,
  addKnowledge,
  cancelIngestion,
  ingestKnowledge,
  type KnowledgeIngestStatus,
  knowledgeFormats,
  knowledgeIngestStatus,
  listKnowledge,
  uploadKnowledgeFile,
} from "../api";
import { ingestClock } from "../lib/fileIngest";
import { ApiError } from "../lib/errors";
import { AddDocumentDialog } from "./AddDocumentDialog";

const realSleep = ingestClock.sleep;

const identity = { org: "o", namespace: "n" };

function status(state: string, more: Partial<KnowledgeIngestStatus> = {}): KnowledgeIngestStatus {
  return {
    found: true,
    ingestion_id: "job-1",
    source_uri: "spec.pdf",
    state,
    stage: "",
    error_code: "",
    error_message: "",
    format: "pdf",
    coverage: null,
    warnings: [],
    chunks: 4,
    revision: 1,
    ...more,
  };
}

function mount(onAdded = vi.fn(), onOpenChange = vi.fn(), replaceMode?: { sourceUri: string; title?: string }) {
  render(
    <AddDocumentDialog
      open
      onOpenChange={onOpenChange}
      identity={identity}
      onAdded={onAdded}
      replaceMode={replaceMode}
    />,
  );
  return { dialog: screen.getByRole("dialog"), onAdded, onOpenChange };
}

async function pick(name: string, type = "") {
  const user = userEvent.setup({ applyAccept: false });
  await user.upload(screen.getByLabelText("Choose a file to upload"), new File(["%PDF-1.7"], name, { type }));
  return user;
}

describe("AddDocumentDialog", () => {
  beforeEach(() => {
    ingestClock.sleep = async () => {}; // follow a job without waiting out its polls
    vi.mocked(knowledgeFormats).mockResolvedValue({
      formats: [
        { format: "pdf", extensions: ["pdf"], media_types: ["application/pdf"], available: true },
        { format: "pptx", extensions: ["pptx"], media_types: [], available: false },
        { format: "text", extensions: ["txt"], media_types: ["text/plain"], available: true },
      ],
      max_upload_bytes: 1_000_000,
      refused_extensions: ["doc", "ppt", "xls"],
      default_format: "text",
    });
    vi.mocked(uploadKnowledgeFile).mockResolvedValue({
      upload_id: "up-1",
      sha256: "",
      size: 8,
      filename: "spec.pdf",
      format: "pdf",
      expires_at: "",
    });
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "spec.pdf" });
  });
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    ingestClock.sleep = realSleep;
  });

  it("badges the file, uploads its bytes, ingests and follows the job until it publishes", async () => {
    vi.mocked(knowledgeIngestStatus)
      .mockResolvedValueOnce(status("processing", { stage: "parse" }))
      .mockResolvedValueOnce(status("ready"));
    const { dialog, onAdded } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf", "application/pdf");
    expect(await within(dialog).findByText("pdf")).toBeInTheDocument();
    // The browser reads nothing: no extracted text is offered for review.
    expect(within(dialog).queryByLabelText(/^Content/)).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    const [id, sent] = vi.mocked(uploadKnowledgeFile).mock.calls[0];
    expect([id, sent.name]).toEqual([identity, "spec.pdf"]);
    expect(vi.mocked(ingestKnowledge).mock.calls[0][1]).toMatchObject({
      upload_id: "up-1",
      source_uri: "spec.pdf",
      title: "spec",
      allow_partial: false,
    });
    expect(addKnowledge).not.toHaveBeenCalled();
  });

  it("closes on pasted text that committed with only lists and search behind", async () => {
    vi.mocked(addKnowledge).mockRejectedValue(
      new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: { retryable: false } }),
    );
    const { onAdded, onOpenChange } = mount();
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Paste text" }));
    await user.type(screen.getByLabelText("Content"), "Refunds go to the lead.");
    await user.type(screen.getByLabelText(/^Source URI/), "docs/refunds.md");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(addKnowledge).toHaveBeenCalledTimes(1);
    // One line, the committed write's, and no success toast beside it.
    expect(toastCommitted).toHaveBeenCalledTimes(1);
    expect(toast).not.toHaveBeenCalled();
  });

  it("refuses a format no extractor reads before anything is sent", async () => {
    const { dialog } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    await pick("deck.pptx");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("No extractor on this deployment reads pptx");
    expect(within(dialog).getByText("pptx: not read here")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Upload" })).toBeDisabled();
    expect(uploadKnowledgeFile).not.toHaveBeenCalled();
  });

  it("says why a job failed and keeps the dialog open", async () => {
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(
      status("failed", { error_code: "format_mismatch", error_message: "the content is not a PDF" }),
    );
    const { dialog, onAdded } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "spec.pdf: Failed (format_mismatch): the content is not a PDF",
    );
    expect(within(dialog).queryByRole("status")).toBeNull(); // said once, in the alert
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("says a partial publish is partial", async () => {
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("partial", { coverage: { lost: ["formula_values"] } }));
    const { dialog, onAdded } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(vi.mocked(toast).mock.calls.at(-1)).toEqual([
      "Added spec.pdf: revision 1, partial: part of the file could not be read (lost: formula_values)",
      "info",
    ]);
  });

  it("can close while following: the job goes on, and no poll follows it", async () => {
    // Each pause waits until the test releases it.
    let release = () => {};
    ingestClock.sleep = () => new Promise<void>((r) => (release = r));
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("processing", { stage: "embed" }));
    const { dialog, onAdded, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent("Embedding…");
    // The footer's Close and the modal's own close both close it the same way.
    await user.click(within(dialog).getAllByRole("button", { name: "Close" })[0]);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(vi.mocked(toast).mock.calls.at(-1)?.[0]).toMatch(/spec\.pdf goes on extracting/);
    const polls = vi.mocked(knowledgeIngestStatus).mock.calls.length;
    release(); // the pause ends after the close: the loop sees the abort and polls no more
    await new Promise((r) => setTimeout(r, 20));
    expect(vi.mocked(knowledgeIngestStatus).mock.calls.length).toBe(polls);
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("stops the job it follows with knowledge.ingest_cancel", async () => {
    ingestClock.sleep = () => new Promise<void>(() => {});
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("processing", { stage: "embed" }));
    vi.mocked(cancelIngestion).mockResolvedValue({ found: true, state: "cancelled", cancelled: true });
    const { dialog, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await user.click(await within(dialog).findByRole("button", { name: "Stop ingestion" }));
    await waitFor(() => expect(cancelIngestion).toHaveBeenCalledWith(identity, "job-1"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(vi.mocked(toast).mock.calls.at(-1)?.[0]).toBe("Stopped spec.pdf");
  });

  it("a Stop that finds the job already published adds it like any other", async () => {
    ingestClock.sleep = () => new Promise<void>(() => {});
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("processing", { stage: "embed" }));
    vi.mocked(cancelIngestion).mockResolvedValue({ found: true, state: "ready", cancelled: false });
    const { dialog, onAdded } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await user.click(await within(dialog).findByRole("button", { name: "Stop ingestion" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(vi.mocked(toast).mock.calls.at(-1)?.[0]).toBe("spec.pdf was already added");
  });

  it("holds the dialog while a Stop runs", async () => {
    ingestClock.sleep = () => new Promise<void>(() => {});
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("processing", { stage: "embed" }));
    vi.mocked(cancelIngestion).mockImplementation(() => new Promise(() => {}));
    const { dialog, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await user.click(await within(dialog).findByRole("button", { name: "Stop ingestion" }));
    await waitFor(() => expect(cancelIngestion).toHaveBeenCalled());
    // The footer's Close is off and every other close goes through the one guard, held until the Stop answers.
    for (const close of within(dialog).getAllByRole("button", { name: "Close" })) await user.click(close);
    await user.keyboard("{Escape}");
    fireEvent.pointerDown(document.body);
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("a job that publishes while its Stop runs is added once, by the Stop's answer", async () => {
    let poll = 0;
    let answer: (r: { found: boolean; state: string; cancelled: boolean }) => void = () => {};
    let tick: () => void = () => {};
    ingestClock.sleep = () => new Promise<void>((r) => (tick = r));
    vi.mocked(knowledgeIngestStatus).mockImplementation(async () =>
      ++poll === 1 ? status("processing", { stage: "embed" }) : status("ready", { revision: 2 }),
    );
    vi.mocked(cancelIngestion).mockImplementation(() => new Promise((r) => (answer = r)));
    const { dialog, onAdded, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await user.click(await within(dialog).findByRole("button", { name: "Stop ingestion" }));
    // The Stop stopped the follow: its pause ends and it polls no more.
    await act(async () => tick());
    expect(knowledgeIngestStatus).toHaveBeenCalledTimes(1);
    expect(onAdded).not.toHaveBeenCalled();
    await act(async () => answer({ found: true, state: "ready", cancelled: false }));
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(vi.mocked(toast).mock.calls.at(-1)?.[0]).toBe("spec.pdf was already added");
    expect(onOpenChange.mock.calls.filter(([v]) => v === false)).toHaveLength(1);
  });

  it("a Stop that fails says the job may still run, adds nothing and closes", async () => {
    ingestClock.sleep = () => new Promise<void>(() => {});
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("processing", { stage: "embed" }));
    vi.mocked(cancelIngestion).mockRejectedValue(new Error("down"));
    const { dialog, onAdded, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await user.click(await within(dialog).findByRole("button", { name: "Stop ingestion" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(vi.mocked(toastError).mock.calls.at(-1)).toEqual([
      "Stop failed (spec.pdf may still be extracting)",
      expect.objectContaining({ message: "down" }),
    ]);
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("a Stop of a running job says it stops before it publishes", async () => {
    ingestClock.sleep = () => new Promise<void>(() => {});
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("processing", { stage: "embed" }));
    vi.mocked(cancelIngestion).mockResolvedValue({ found: true, state: "processing", cancelled: true });
    const { dialog, onAdded } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await user.click(await within(dialog).findByRole("button", { name: "Stop ingestion" }));
    await waitFor(() => expect(vi.mocked(toast).mock.calls.at(-1)?.[0]).toBe("Stopping spec.pdf before it publishes"));
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("can cancel a pending upload, which queues nothing, and holds through the ingest call", async () => {
    let landed: (u: KnowledgeUpload) => void = () => {};
    vi.mocked(uploadKnowledgeFile).mockImplementation(() => new Promise((r) => (landed = r)));
    const { dialog, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await within(dialog).findByText("Uploading…")).toBeInTheDocument();
    // The address and options are frozen with the file on its way.
    expect(within(dialog).getByLabelText(/^Source URI/)).toBeDisabled();
    expect(within(dialog).getByLabelText(/^Title/)).toBeDisabled();
    expect(within(dialog).getByRole("checkbox")).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    landed({ upload_id: "up-1", sha256: "", size: 8, filename: "spec.pdf", format: "pdf", expires_at: "" });
    await new Promise((r) => setTimeout(r, 20));
    expect(ingestKnowledge).not.toHaveBeenCalled();
  });

  it("holds the dialog while the ingest call may queue the job", async () => {
    vi.mocked(ingestKnowledge).mockImplementation(() => new Promise(() => {}));
    const { dialog, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    expect(await within(dialog).findByText("Queuing…")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("says a queued job goes on when its polls keep failing, and sends nothing again", async () => {
    vi.mocked(knowledgeIngestStatus).mockRejectedValue(new TypeError("Failed to fetch"));
    const { dialog, onAdded, onOpenChange } = mount();
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(vi.mocked(toast).mock.calls.at(-1)).toEqual([
      "spec.pdf goes on extracting; it is in the Library once it publishes and the list is reloaded",
      "info",
    ]);
    expect(onAdded).not.toHaveBeenCalled(); // nothing has published yet
    expect(ingestKnowledge).toHaveBeenCalledTimes(1);
  });

  it("in replace mode publishes the file as the document's next revision, under its locked address", async () => {
    vi.mocked(knowledgeIngestStatus).mockResolvedValueOnce(status("ready", { revision: 4 }));
    const { dialog, onAdded } = mount(vi.fn(), vi.fn(), { sourceUri: "kb://spec", title: "Spec" });
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec-v2.pdf");
    expect(within(dialog).queryByLabelText(/^Source URI/)).toBeNull(); // locked
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(vi.mocked(ingestKnowledge).mock.calls[0][1]).toMatchObject({ source_uri: "kb://spec", title: "Spec" });
    expect(vi.mocked(toast).mock.calls.at(-1)).toEqual(["Replaced kb://spec: revision 4", "ok"]);
  });

  it("in replace mode says the new revision shows when it stops following", async () => {
    vi.mocked(knowledgeIngestStatus).mockRejectedValue(new TypeError("Failed to fetch"));
    const { dialog, onOpenChange } = mount(vi.fn(), vi.fn(), { sourceUri: "kb://spec", title: "Spec" });
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = await pick("spec-v2.pdf");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(vi.mocked(toast).mock.calls.at(-1)).toEqual([
      "kb://spec goes on extracting; its new revision shows once it publishes and the list is reloaded",
      "info",
    ]);
  });

  it("writes pasted text as text", async () => {
    vi.mocked(addKnowledge).mockResolvedValue({
      doc_id: "d",
      chunks_created: 1,
      embedding_model: "m",
      embedding_dim: 1,
    });
    const { dialog, onAdded } = mount();
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole("tab", { name: "Paste text" }));
    await user.type(within(dialog).getByLabelText("Content"), "Step 1: page the owner.");
    await user.type(within(dialog).getByLabelText(/^Source URI/), "kb://runbook");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(vi.mocked(addKnowledge).mock.calls[0][1]).toEqual({
      source_uri: "kb://runbook",
      title: "",
      description: "",
      content: "Step 1: page the owner.",
      tags: [],
    });
    expect(uploadKnowledgeFile).not.toHaveBeenCalled();
  });

  describe("many files", () => {
    async function pickMany(names: string[]) {
      const user = userEvent.setup({ applyAccept: false });
      await user.upload(
        screen.getByLabelText("Choose a file to upload"),
        names.map((n) => new File(["%PDF-1.7"], n)),
      );
      return user;
    }

    it("sends each file as its own document, saying where each is, and skips what is already there", async () => {
      vi.mocked(listKnowledge).mockResolvedValue({ rows: [{ source_uri: "b.pdf" }], next_cursor: null } as never);
      vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => ({
        ingestion_id: o.source_uri,
        state: "pending",
        source_uri: o.source_uri,
      }));
      vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("ready"));
      const { dialog, onAdded } = mount();
      await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
      const user = await pickMany(["a.pdf", "b.pdf", "photo.png"]);
      expect(within(dialog).getByRole("heading", { name: "Add documents to n" })).toBeInTheDocument();
      const files = within(dialog).getByRole("list", { name: "Files" });
      expect(within(files).getByText("Not a format this deployment reads")).toBeInTheDocument();
      expect(await within(files).findByText("Already in n: replaced")).toBeInTheDocument();
      await user.click(within(dialog).getByRole("radio", { name: "Skip them" }));
      await user.click(within(dialog).getByRole("button", { name: "Upload 1 file to n" }));
      await waitFor(() => expect(onAdded).toHaveBeenCalled());
      expect(within(files).getByText("Revision 1")).toBeInTheDocument();
      expect(within(files).getByText("Already here")).toBeInTheDocument();
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([, o]) => [o.source_uri, o.expected_revision])).toEqual([
        ["a.pdf", 0],
      ]);
      expect(toast).toHaveBeenCalledWith("1 added, 2 skipped", "ok");
    });

    it("sends again only the files that failed", async () => {
      vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null } as never);
      vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => ({
        ingestion_id: o.source_uri,
        state: "pending",
        source_uri: o.source_uri,
      }));
      vi.mocked(knowledgeIngestStatus)
        .mockImplementationOnce(async () => status("failed", { error_code: "no_text", error_message: "no text" }))
        .mockResolvedValue(status("ready"));
      const { dialog } = mount();
      await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
      const user = await pickMany(["a.pdf", "b.pdf"]);
      await user.click(await within(dialog).findByRole("button", { name: "Upload 2 files to n" }));
      const retry = await within(dialog).findByRole("button", { name: "Retry 1 failed" });
      await waitFor(() => expect(retry).toHaveFocus());
      // The modal's own close asks first while a file failed (a test has no confirm
      // viewport, so the browser's confirm is asked).
      const asked = vi.spyOn(window, "confirm").mockReturnValue(false);
      await user.keyboard("{Escape}");
      await waitFor(() =>
        expect(asked).toHaveBeenCalledWith(
          expect.stringMatching(/^Close with files not added\?\n\n1 file failed\. Close anyway\?/),
        ),
      );
      asked.mockRestore();
      await user.click(retry);
      await waitFor(() => expect(within(dialog).getAllByText("Revision 1")).toHaveLength(2));
      expect(ingestKnowledge).toHaveBeenCalledTimes(3);
    });

    it("takes dropped files, reading them while the drop lasts, and says a folder is not taken", async () => {
      const { dialog } = mount();
      await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
      vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null } as never);
      let dropping = true;
      const item = (f: File | null) => ({
        kind: "file",
        webkitGetAsEntry: () => {
          if (!dropping) throw new Error("read after the drop");
          return { isDirectory: f === null };
        },
        getAsFile: () => {
          if (!dropping) throw new Error("read after the drop");
          return f;
        },
      });
      const zone = within(dialog).getByTestId("add-doc-drop");
      fireEvent.drop(zone, { dataTransfer: { types: ["Files"], items: [item(null)], files: [] } });
      dropping = false;
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Folders are not taken here");
      dropping = true;
      fireEvent.drop(zone, {
        dataTransfer: {
          types: ["Files"],
          items: [item(new File(["x"], "a.pdf")), item(new File(["y"], "b.pdf"))],
          files: [],
        },
      });
      dropping = false;
      // Once the check of what is already there answers, Upload is offered.
      await waitFor(
        () => expect(screen.getByRole("button", { name: "Upload 2 files to n" })).not.toHaveAttribute("aria-disabled"),
        { timeout: 5000 },
      );
    });
  });

  it("hands the focus to Upload when the list replaces the form", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null } as never);
    render(<AddDocumentDialog open onOpenChange={() => {}} onAdded={() => {}} identity={identity} place="Team" />);
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(screen.getByLabelText("Choose a file to upload"), [
      new File(["a"], "a.pdf"),
      new File(["b"], "b.pdf"),
    ]);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: /^Upload 2 files/ })));
  });

  it("keeps naming where a batch's files go when the page shows another place", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null } as never);
    const props = { open: true, onOpenChange: () => {}, onAdded: () => {} };
    const { rerender } = render(<AddDocumentDialog {...props} identity={identity} place="Team" />);
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(screen.getByLabelText("Choose a file to upload"), [
      new File(["a"], "a.pdf"),
      new File(["b"], "b.pdf"),
    ]);
    expect(await screen.findByRole("heading", { name: "Add documents to Team" })).toBeInTheDocument();
    rerender(<AddDocumentDialog {...props} identity={{ org: "o", namespace: "other" }} place="Other" />);
    expect(screen.getByRole("heading", { name: "Add documents to Team" })).toBeInTheDocument();
  });

  it("keeps a file dropped beside the drop zone from opening in the tab, and leaves text drops alone", () => {
    // jsdom has no DataTransfer: the drag's types are all the guard reads.
    const drag = (types: string[], type = "drop") => {
      const e = new Event(type, { cancelable: true, bubbles: true });
      Object.defineProperty(e, "dataTransfer", { value: { types } });
      document.body.dispatchEvent(e);
      return e.defaultPrevented;
    };
    const { unmount } = render(
      <AddDocumentDialog open onOpenChange={() => {}} onAdded={() => {}} identity={identity} />,
    );
    // The dragover cancel is what stops the browser handling the drop itself.
    for (const type of ["dragover", "drop"]) {
      expect(drag(["Files"], type)).toBe(true);
      expect(drag(["text/plain"], type)).toBe(false);
    }
    unmount();
    expect(drag(["Files"], "dragover")).toBe(false);
    expect(drag(["Files"])).toBe(false);
  });

  it("says no drop beside the zone, and leaves a drag the zone took as it set it", () => {
    render(<AddDocumentDialog open onOpenChange={() => {}} onAdded={() => {}} identity={identity} />);
    const over = (taken: boolean) => {
      const e = new Event("dragover", { cancelable: true, bubbles: true });
      const dataTransfer = { types: ["Files"], dropEffect: "copy" };
      Object.defineProperty(e, "dataTransfer", { value: dataTransfer });
      // A zone cancels the drag over it before it bubbles to the window.
      const zone = document.createElement("div");
      if (taken) zone.addEventListener("dragover", (ev) => ev.preventDefault());
      document.body.append(zone);
      zone.dispatchEvent(e);
      zone.remove();
      return dataTransfer.dropEffect;
    };
    expect(over(false)).toBe("none");
    expect(over(true)).toBe("copy");
  });

  it("starts a batch with the tags and the partial choice the one-file form held", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null } as never);
    render(<AddDocumentDialog open onOpenChange={() => {}} onAdded={() => {}} identity={identity} />);
    await waitFor(() => expect(knowledgeFormats).toHaveBeenCalled());
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(screen.getByLabelText("Choose a file to upload"), new File(["a"], "a.pdf"));
    await user.type(screen.getByLabelText("Tags (comma-separated, optional)"), "q3, plans");
    await user.click(screen.getByLabelText(/Publish what could be read/));
    await user.upload(screen.getByLabelText("Choose a file to upload"), [
      new File(["a"], "a.pdf"),
      new File(["b"], "b.pdf"),
    ]);
    expect(await screen.findByLabelText("Tags for every file (comma-separated, optional)")).toHaveValue("q3, plans");
    expect(screen.getByLabelText(/Publish what could be read/)).toBeChecked();
  });
});
