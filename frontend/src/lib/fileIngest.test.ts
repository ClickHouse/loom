/**
 * A file into the knowledge plane from the dashboard: what the deployment reads it as
 * (by extension, then media type, then the default), the line a person sees for each
 * step of its job, and the upload → ingest → poll sequence.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
}));
import { ingestKnowledge, type KnowledgeFormats, knowledgeIngestStatus, uploadKnowledgeFile } from "../api";
import { fileVerdict, ingestClock, ingestFile, ingestStep } from "./fileIngest";

const realSleep = ingestClock.sleep;

const FORMATS: KnowledgeFormats = {
  formats: [
    { format: "markdown", extensions: ["markdown", "md"], media_types: ["text/markdown"], available: true },
    { format: "pdf", extensions: ["pdf"], media_types: ["application/pdf"], available: true },
    { format: "pptx", extensions: ["pptx"], media_types: [], available: false },
    { format: "xlsx", extensions: ["xlsx"], media_types: [], available: null },
    { format: "text", extensions: ["txt"], media_types: ["text/plain"], available: true },
  ],
  max_upload_bytes: 1000,
  refused_extensions: ["doc", "ppt", "xls"],
  default_format: "text",
};

const file = (name: string, type = "", size = 10) => ({ name, type, size });

describe("fileVerdict", () => {
  it("names the format by extension, then media type, then the default", () => {
    expect(fileVerdict(file("Spec.PDF"), FORMATS)).toEqual({ format: "pdf", available: true, refusal: "" });
    expect(fileVerdict(file("notes", "text/markdown; charset=utf-8"), FORMATS).format).toBe("markdown");
    expect(fileVerdict(file("README", ""), FORMATS).format).toBe("text");
  });

  it("refuses a legacy Office name, a file over the limit and a format no extractor reads", () => {
    expect(fileVerdict(file("old.doc"), FORMATS).refusal).toMatch(/save it as \.docx/);
    expect(fileVerdict(file("big.pdf", "", 5000), FORMATS).refusal).toMatch(/takes files up to/);
    const deck = fileVerdict(file("deck.pptx"), FORMATS);
    expect([deck.format, deck.available]).toEqual(["pptx", false]);
    expect(deck.refusal).toMatch(/No extractor on this deployment reads pptx/);
  });

  it("refuses nothing whose extractor the server could not ask just now", () => {
    expect(fileVerdict(file("orders.xlsx"), FORMATS)).toEqual({ format: "xlsx", available: null, refusal: "" });
  });

  it("says nothing it does not know when the server did not list its formats", () => {
    expect(fileVerdict(file("deck.pptx"), null)).toEqual({ format: "", available: null, refusal: "" });
  });
});

describe("ingestStep", () => {
  const s = (state: string, more: Record<string, unknown> = {}) => ({
    state,
    stage: "",
    revision: 2,
    chunks: 7,
    error_code: "",
    error_message: "",
    ...more,
  });
  it("words each state and stage", () => {
    expect(ingestStep(s("pending"))).toBe("Queued…");
    expect(ingestStep(s("pending", { error_code: "retrying" }))).toBe("Waiting to try again…");
    expect(ingestStep(s("processing", { stage: "parse" }))).toBe("Extracting…");
    expect(ingestStep(s("processing", { stage: "embed" }))).toBe("Embedding…");
    expect(ingestStep(s("ready"))).toBe("Ready: revision 2, 7 chunks");
    expect(ingestStep(s("partial"))).toMatch(/^Published part of it/);
    expect(ingestStep(s("failed", { error_code: "format_mismatch", error_message: "not a PDF" }))).toBe(
      "Failed (format_mismatch): not a PDF",
    );
  });
});

describe("ingestFile", () => {
  afterEach(() => {
    vi.resetAllMocks();
    ingestClock.sleep = realSleep;
  });

  it("uploads the file, ingests it and polls until the job ends, saying each step", async () => {
    vi.mocked(uploadKnowledgeFile).mockResolvedValue({
      upload_id: "up-1",
      sha256: "",
      size: 3,
      filename: "a.md",
      format: "markdown",
      expires_at: "",
    });
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "a" });
    const base = { found: true, ingestion_id: "job-1", source_uri: "a", error_code: "", error_message: "" };
    const rest = { format: "markdown", coverage: null, warnings: [], chunks: 1, revision: 1 };
    vi.mocked(knowledgeIngestStatus)
      .mockResolvedValueOnce({ ...base, ...rest, state: "processing", stage: "parse" })
      .mockResolvedValueOnce({ ...base, ...rest, state: "ready", stage: "" });
    const steps: string[] = [];
    const f = new File(["# a"], "a.md", { type: "text/markdown" });
    const id = { org: "o", namespace: "n" };
    ingestClock.sleep = async () => {};
    const out = await ingestFile(id, f, { source_uri: "a", tags: ["x"] }, (line) => steps.push(line));
    expect(out.state).toBe("ready");
    expect(steps).toEqual(["Uploading…", "Queuing…", "Extracting…", "Ready: revision 1, 1 chunk"]);
    expect(vi.mocked(uploadKnowledgeFile).mock.calls[0].slice(0, 2)).toEqual([id, f]);
    expect(vi.mocked(ingestKnowledge).mock.calls[0]).toEqual([id, { source_uri: "a", tags: ["x"], upload_id: "up-1" }]);
  });

  it("stops following at an abort, the job left to the server", async () => {
    vi.mocked(uploadKnowledgeFile).mockResolvedValue({
      upload_id: "up-1",
      sha256: "",
      size: 1,
      filename: "a.md",
      format: "markdown",
      expires_at: "",
    });
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "a" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue({
      found: true,
      ingestion_id: "job-1",
      source_uri: "a",
      state: "processing",
      stage: "parse",
      error_code: "",
      error_message: "",
      format: "markdown",
      coverage: null,
      warnings: [],
      chunks: 0,
      revision: 0,
    });
    const ctrl = new AbortController();
    ingestClock.sleep = async () => ctrl.abort();
    const run = ingestFile({ org: "o", namespace: "n" }, new File(["a"], "a.md"), { source_uri: "a" }, () => {}, {
      signal: ctrl.signal,
    });
    await expect(run).rejects.toThrow();
    expect(knowledgeIngestStatus).toHaveBeenCalledTimes(1);
    // Each poll keeps the default request bound: no signal of its own.
    expect(vi.mocked(knowledgeIngestStatus).mock.calls[0]).toEqual([{ org: "o", namespace: "n" }, "job-1"]);
  });
});
