/**
 * A batch of files: what is left out before anything is sent, the limits counted over
 * what is sent, at most BATCH_OPEN jobs open at once, a step sent again only when the
 * server says it may be, and a source added meanwhile skipped rather than published twice.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
  listKnowledge: vi.fn(),
  cancelIngestion: vi.fn(),
}));
import {
  cancelIngestion,
  type KnowledgeFormats,
  type KnowledgeIngestStatus,
  ingestKnowledge,
  knowledgeIngestStatus,
  listKnowledge,
  uploadKnowledgeFile,
} from "../api";
import {
  BATCH_OPEN,
  type BatchRow,
  capBatch,
  existingSources,
  MAX_BATCH_FILES,
  MAX_CHECK_PAGES,
  planBatch,
  retryFailed,
  runBatch,
} from "./batchIngest";
import { ApiError } from "./errors";
import { ingestClock } from "./fileIngest";

const FORMATS: KnowledgeFormats = {
  formats: [
    { format: "pdf", extensions: ["pdf"], media_types: ["application/pdf"], available: true },
    { format: "markdown", extensions: ["md"], media_types: [], available: true },
  ],
  max_upload_bytes: 1_000_000,
  refused_extensions: ["doc"],
  default_format: "text",
};
const ID = { org: "o", namespace: "n" };
const realSleep = ingestClock.sleep;

const file = (name: string, size = 4) => new File(["x".repeat(size)], name);
const plan = (names: string[]) =>
  planBatch(
    names.map((n) => ({ file: file(n), path: n.includes("/") ? n : "" })),
    FORMATS,
    () => ID,
  );

function status(state: string, more: Partial<KnowledgeIngestStatus> = {}): KnowledgeIngestStatus {
  return {
    found: true,
    ingestion_id: "j",
    source_uri: "",
    state,
    stage: "",
    error_code: "",
    error_message: "",
    format: "pdf",
    coverage: null,
    warnings: [],
    chunks: 1,
    revision: 1,
    ...more,
  };
}

const run = async (rows: BatchRow[], signal = new AbortController().signal, cancel = true) => {
  const seen = new Map(rows.map((r) => [r.id, r]));
  await runBatch(rows, {
    fields: { tags: [], allow_partial: false },
    signal,
    cancelling: () => cancel,
    onRow: (r) => seen.set(r.id, r),
  });
  return [...seen.values()];
};

describe("planBatch", () => {
  it("leaves out hidden, system, unread and repeated files, saying why", () => {
    const rows = plan([
      "a.pdf",
      ".env",
      "x/.git/config.md",
      "Thumbs.db",
      "~$draft.md",
      "photo.png",
      "old.doc",
      "a.pdf",
    ]);
    expect(rows.map((r) => [r.key, r.state, r.line])).toEqual([
      ["a.pdf", "waiting", "Not sent yet"],
      [".env", "skipped", "Hidden"],
      ["x/.git/config.md", "skipped", "Hidden"],
      ["Thumbs.db", "skipped", "Left by the operating system or an editor"],
      ["~$draft.md", "skipped", "Left by the operating system or an editor"],
      ["photo.png", "skipped", "Not a format this deployment reads"],
      ["old.doc", "skipped", expect.stringMatching(/^\.doc files are not read/)],
      ["a.pdf", "skipped", "Same name as another file here"],
    ]);
    expect(rows[0]).toMatchObject({ sourceUri: "a.pdf", title: "a", target: ID });
  });

  it("keeps a file's own name in the line that refuses it", () => {
    const [row] = planBatch([{ file: new File([], "notes.md"), path: "" }], FORMATS, () => ID);
    expect(row).toMatchObject({ state: "skipped", line: "notes.md is empty." });
  });

  it("holds back what is past one upload's limits, counted over the files it will send", () => {
    const many = plan(Array.from({ length: MAX_BATCH_FILES + 2 }, (_, i) => `f${String(i).padStart(3, "0")}.md`));
    const rows = capBatch(many, false);
    expect(rows.filter((r) => r.state === "next")).toHaveLength(2);
    expect(rows.filter((r) => r.state === "waiting")).toHaveLength(MAX_BATCH_FILES);
    expect(rows.at(-1)).toMatchObject({ state: "next", line: "Sent by Upload the next files" });
    const mostlySkipped = plan([...Array.from({ length: MAX_BATCH_FILES + 1 }, (_, i) => `.h${i}`), "a.md"]);
    expect(capBatch(mostlySkipped, false).filter((r) => r.state === "next")).toHaveLength(0);
  });

  it("holds back a prefix in path order, and sends one file however large", () => {
    const big = (name: string, mb: number) => ({ file: new File([new Uint8Array(0)], name), path: "", mb });
    const rows = planBatch(
      [big("a.md", 400), big("b.md", 400), big("c.md", 400), big("d.md", 1)].map((e) => {
        Object.defineProperty(e.file, "size", { value: e.mb * 1024 * 1024 });
        return { file: e.file, path: e.path };
      }),
      { ...FORMATS, max_upload_bytes: 2 * 1024 ** 3 },
      () => ID,
    );
    const capped = capBatch(rows, false);
    // a and b fit in 1 GB; c is past it, and d after it waits too, though it is small.
    expect(capped.map((r) => r.state)).toEqual(["waiting", "waiting", "next", "next"]);
    const huge = planBatch(
      [{ file: Object.defineProperty(new File([], "h.md"), "size", { value: 1.5 * 1024 ** 3 }), path: "" }],
      { ...FORMATS, max_upload_bytes: 2 * 1024 ** 3 },
      () => ID,
    );
    expect(capBatch(huge, false).filter((r) => r.state === "next")).toHaveLength(0);
  });

  it("sends the rest with Upload the next files: the files already there are skipped, not counted", () => {
    // 250 picked again, the first 200 of them already uploaded.
    const rows = plan(Array.from({ length: 250 }, (_, i) => `f${String(i).padStart(3, "0")}.md`)).map((r, i) =>
      i < 200 ? { ...r, replaces: true } : r,
    );
    const held = (rs: BatchRow[]) => rs.filter((r) => r.state === "next").length;
    const skipping = capBatch(rows, true);
    expect(held(skipping)).toBe(0);
    const replacing = capBatch(skipping, false);
    expect(held(replacing)).toBe(50);
    expect(held(capBatch(replacing, true))).toBe(0);
  });
});

describe("retryFailed", () => {
  it("sends the failed ones again within one upload's limits, leaving the held ones held", () => {
    const rows = plan(Array.from({ length: MAX_BATCH_FILES + 3 }, (_, i) => `f${String(i).padStart(3, "0")}.md`)).map(
      (r, i) => (i < MAX_BATCH_FILES + 1 ? { ...r, state: "failed" as const } : { ...r, state: "next" as const }),
    );
    const again = retryFailed(rows, false);
    expect(again.filter((r) => r.state === "waiting")).toHaveLength(MAX_BATCH_FILES);
    // The one failed file past the limit waits with the two that were already held.
    expect(again.filter((r) => r.state === "next")).toHaveLength(3);
    expect(again.filter((r) => r.state === "failed")).toHaveLength(0);
  });
});

describe("runBatch", () => {
  beforeEach(() => {
    ingestClock.sleep = async () => {};
    vi.mocked(uploadKnowledgeFile).mockImplementation(async (_id, f) => ({
      upload_id: `up-${f.name}`,
      sha256: "",
      size: f.size,
      filename: f.name,
      format: "pdf",
      expires_at: "",
    }));
  });
  afterEach(() => {
    vi.resetAllMocks();
    ingestClock.sleep = realSleep;
  });

  it("keeps at most BATCH_OPEN jobs open, and a new source publishes only while none is live", async () => {
    let open = 0;
    let most = 0;
    vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => {
      open++;
      most = Math.max(most, open);
      return { ingestion_id: o.source_uri, state: "pending", source_uri: o.source_uri };
    });
    vi.mocked(knowledgeIngestStatus).mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 1));
      open--;
      return status("ready");
    });
    const rows = await run(plan(Array.from({ length: 8 }, (_, i) => `f${i}.md`)));
    expect(rows.every((r) => r.state === "done")).toBe(true);
    expect(most).toBe(BATCH_OPEN);
    expect(vi.mocked(ingestKnowledge).mock.calls.every(([, o]) => o.expected_revision === 0)).toBe(true);
  });

  it("waits out a rate limit and sends the step again", async () => {
    vi.mocked(ingestKnowledge)
      .mockRejectedValueOnce(
        new ApiError({ status: 429, code: "RATE_LIMITED", detail: { retryable: true, retry_after_s: 1 } }),
      )
      .mockResolvedValue({ ingestion_id: "j", state: "pending", source_uri: "a.md" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("ready"));
    const [row] = await run(plan(["a.md"]));
    expect(row.state).toBe("done");
    expect(ingestKnowledge).toHaveBeenCalledTimes(2);
  });

  it("waits out a rate limit on the upload and sends the bytes again", async () => {
    vi.mocked(uploadKnowledgeFile)
      .mockRejectedValueOnce(
        new ApiError({ status: 429, code: "RATE_LIMITED", detail: { retryable: true, retry_after_s: 1 } }),
      )
      .mockImplementation(async (_id, f) => ({ upload_id: `u-${f.name}` }) as never);
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "j", state: "pending", source_uri: "a.md" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("ready"));
    const [row] = await run(plan(["a.md"]));
    expect(row.state).toBe("done");
    expect(uploadKnowledgeFile).toHaveBeenCalledTimes(2);
  });

  it("does not say a file may be saved when the server said its step may be sent again", async () => {
    let calls = 0;
    vi.mocked(ingestKnowledge).mockImplementation(async () => {
      calls++;
      throw new ApiError({ status: 503, code: "OVERLOADED", detail: { retryable: true, retry_after_s: 100 } });
    });
    const [row] = await run(plan(["a.md"]));
    expect(calls).toBe(1); // the wait asked for runs past the minute: given up at once
    expect(row.state).toBe("failed");
    expect(row.line).not.toMatch(/may have been saved/);
  });

  it("does not send an ingest again that the server did not say may be, and says it may be saved", async () => {
    vi.mocked(ingestKnowledge).mockRejectedValue(
      new ApiError({ status: 503, code: "OVERLOADED", detail: { retryable: false } }),
    );
    const [row] = await run(plan(["a.md"]));
    expect(ingestKnowledge).toHaveBeenCalledTimes(1);
    expect(row.state).toBe("failed");
    expect(row.line).toMatch(/may have been saved; check before trying again/);
  });

  it("skips a source already there instead of publishing it twice", async () => {
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "j", state: "pending", source_uri: "a.md" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(
      status("failed", { error_code: "revision_conflict", error_message: "the source has revision 1, not 0" }),
    );
    const [row] = await run(plan(["a.md"]));
    expect(row).toMatchObject({ state: "skipped", line: "A document with this source is already here" });
  });

  it("replaces a document already there unconditionally", async () => {
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "j", state: "pending", source_uri: "a.md" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("ready", { revision: 2 }));
    const there = plan(["a.md"]).map((r) => ({ ...r, replaces: true }));
    const [replaced] = await run(there);
    expect(replaced).toMatchObject({ state: "done", line: "Revision 2" });
    expect(vi.mocked(ingestKnowledge).mock.calls[0][1]).not.toHaveProperty("expected_revision");
  });

  it("keeps a replaced document's title, description and tags, adding the batch's", async () => {
    vi.mocked(ingestKnowledge).mockResolvedValue({ ingestion_id: "j", state: "pending", source_uri: "a.md" });
    vi.mocked(knowledgeIngestStatus).mockResolvedValue(status("ready", { revision: 2 }));
    const kept = { title: "Onboarding", description: "How we start", tags: ["guide"] };
    const there: BatchRow[] = plan(["a.md"]).map((r) => ({ ...r, replaces: true, kept }));
    const seen = new Map(there.map((r) => [r.id, r]));
    await runBatch(there, {
      fields: { tags: ["q3"], allow_partial: false },
      signal: new AbortController().signal,
      onRow: (r) => seen.set(r.id, r),
    });
    expect(vi.mocked(ingestKnowledge).mock.calls[0][1]).toMatchObject({
      title: "Onboarding",
      description: "How we start",
      tags: ["guide", "q3"],
    });
  });

  it("does not send again a step stopped while it waited out a rate limit", async () => {
    const ctrl = new AbortController();
    ingestClock.sleep = async () => ctrl.abort(); // Stop lands during the wait
    vi.mocked(ingestKnowledge).mockRejectedValueOnce(
      new ApiError({ status: 429, code: "RATE_LIMITED", detail: { retryable: true, retry_after_s: 1 } }),
    );
    const [row] = await run(plan(["a.md"]), ctrl.signal);
    expect(ingestKnowledge).toHaveBeenCalledTimes(1);
    expect(row.state).toBe("not_sent");
  });

  it("ends a long rate-limit wait at once when stopped", async () => {
    ingestClock.sleep = realSleep; // a real wait, which Stop must cut short
    const ctrl = new AbortController();
    vi.mocked(ingestKnowledge).mockRejectedValueOnce(
      new ApiError({ status: 429, code: "RATE_LIMITED", detail: { retryable: true, retry_after_s: 50 } }),
    );
    const started = Date.now();
    const done = run(plan(["a.md"]), ctrl.signal);
    await new Promise((r) => setTimeout(r, 20));
    ctrl.abort();
    const [row] = await done;
    expect(Date.now() - started).toBeLessThan(2000);
    expect(row.state).toBe("not_sent");
  });

  it("cancels a job whose ingest answered after Stop", async () => {
    const ctrl = new AbortController();
    vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => {
      ctrl.abort(); // Stop lands while the ingest call is on its way
      return { ingestion_id: "late", state: "pending", source_uri: o.source_uri };
    });
    vi.mocked(cancelIngestion).mockResolvedValue({ found: true, cancelled: true, state: "pending" });
    const [row] = await run(plan(["a.md"]), ctrl.signal);
    expect(cancelIngestion).toHaveBeenCalledWith(ID, "late");
    expect(row).toMatchObject({ state: "not_sent", line: "Stopped" });
  });

  it("says a file may be saved when its ingest call fails after Stop", async () => {
    const ctrl = new AbortController();
    vi.mocked(ingestKnowledge).mockImplementation(async () => {
      ctrl.abort(); // Stop lands while the ingest call is on its way
      throw new ApiError({ status: 502, code: "", detail: {} });
    });
    const [row] = await run(plan(["a.md"]), ctrl.signal);
    expect(row.state).toBe("failed");
    expect(row.line).toMatch(/may have been saved; check before trying again/);
  });

  it("says Not sent for a row sent again and stopped during its upload, cancelling no old job", async () => {
    const ctrl = new AbortController();
    vi.mocked(uploadKnowledgeFile).mockImplementation(async () => {
      ctrl.abort(); // Stop lands while the bytes are on their way
      throw new DOMException("aborted", "AbortError");
    });
    const again = plan(["a.md"]).map((r) => ({ ...r, ingestionId: "last-run" }));
    const [row] = await run(again, ctrl.signal);
    expect(row.state).toBe("not_sent");
    expect(cancelIngestion).not.toHaveBeenCalled();
  });

  it("does not call a running job stopped: it may still publish", async () => {
    const ctrl = new AbortController();
    vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => {
      ctrl.abort(); // Stop lands while the ingest call is on its way
      return { ingestion_id: "running", state: "pending", source_uri: o.source_uri };
    });
    vi.mocked(cancelIngestion).mockResolvedValue({ found: true, cancelled: true, state: "processing" });
    const [row] = await run(plan(["a.md"]), ctrl.signal);
    expect(row).toMatchObject({
      state: "goes_on",
      line: "Stop asked; if it was already publishing, it appears once the list is reloaded",
    });
  });

  it("leaves a queued job going when the batch is closed rather than stopped", async () => {
    const ctrl = new AbortController();
    vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => {
      ctrl.abort();
      return { ingestion_id: "late", state: "pending", source_uri: o.source_uri };
    });
    const [row] = await run(plan(["a.md"]), ctrl.signal, false);
    expect(cancelIngestion).not.toHaveBeenCalled();
    expect(row.state).toBe("goes_on");
  });

  it("sends nothing more once stopped", async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    const rows = await run(plan(["a.md", "b.md"]), ctrl.signal);
    expect(rows.map((r) => r.state)).toEqual(["not_sent", "not_sent"]);
    expect(uploadKnowledgeFile).not.toHaveBeenCalled();
  });
});

describe("existingSources", () => {
  afterEach(() => vi.resetAllMocks());

  it("stops after MAX_CHECK_PAGES pages and says so", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [{ source_uri: "a" }], next_cursor: "more" } as never);
    const found = await existingSources(ID);
    expect(found.complete).toBe(false);
    expect(listKnowledge).toHaveBeenCalledTimes(MAX_CHECK_PAGES);
  });

  it("reads every page of source URIs", async () => {
    vi.mocked(listKnowledge)
      .mockResolvedValueOnce({ rows: [{ source_uri: "a" }], next_cursor: "c" } as never)
      .mockResolvedValueOnce({ rows: [{ source_uri: "b" }], next_cursor: null } as never);
    const found = await existingSources(ID);
    expect([...found.uris.keys()]).toEqual(["a", "b"]);
    expect(found.complete).toBe(true);
    expect(vi.mocked(listKnowledge).mock.calls[1][1]).toMatchObject({
      cursor: "c",
      fields: ["source_uri", "title", "description", "tags"],
      limit: 500,
    });
  });
});
