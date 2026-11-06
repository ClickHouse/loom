/**
 * The Library's add dialog: a picked file is badged with the format this deployment
 * reads it as, uploaded as its bytes and ingested, and its job followed until it
 * publishes; a format no extractor reads is refused before anything is sent; a job
 * that fails says why; pasted text is a text write.
 */
import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  knowledgeFormats: vi.fn(),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
  addKnowledge: vi.fn(),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
}));
import { toast } from "../components/Toast";
import {
  addKnowledge,
  ingestKnowledge,
  type KnowledgeIngestStatus,
  knowledgeFormats,
  knowledgeIngestStatus,
  uploadKnowledgeFile,
} from "../api";
import { ingestClock } from "../lib/fileIngest";
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

function mount(onAdded = vi.fn(), onOpenChange = vi.fn()) {
  render(<AddDocumentDialog open onOpenChange={onOpenChange} identity={identity} onAdded={onAdded} />);
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
});
