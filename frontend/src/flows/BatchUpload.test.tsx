/**
 * A batch past one upload's limits: the first 200 go, the rest wait in the dialog, and
 * "Upload the next N files" sends them, so every file is sent exactly once. The place is
 * named in the button, and focus follows the batch.
 */
import { StrictMode } from "react";
import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
  listKnowledge: vi.fn(),
}));
// A confirm a test holds open (`held.next`), else the browser's confirm.
const held = vi.hoisted(() => ({ next: null as Promise<boolean> | null }));
vi.mock("../components/ConfirmDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/ConfirmDialog")>()),
  confirmAction: (opts: { title: string; message?: string }) =>
    held.next ?? Promise.resolve(window.confirm(opts.title + (opts.message ? `\n\n${opts.message}` : ""))),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
}));
import {
  ingestKnowledge,
  type KnowledgeFormats,
  knowledgeIngestStatus,
  listKnowledge,
  uploadKnowledgeFile,
} from "../api";
import { toast } from "../components/Toast";
import { type BatchRow, MAX_BATCH_FILES } from "../lib/batchIngest";
import { ApiError } from "../lib/errors";
import { ingestClock } from "../lib/fileIngest";
import { BatchUpload, progressLine } from "./BatchUpload";

const FORMATS: KnowledgeFormats = {
  formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
  max_upload_bytes: 1_000_000,
  refused_extensions: [],
  default_format: "text",
};
const realSleep = ingestClock.sleep;

describe("BatchUpload", () => {
  beforeEach(() => {
    ingestClock.sleep = async () => {};
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [], next_cursor: null } as never);
    vi.mocked(uploadKnowledgeFile).mockImplementation(async (_id, f) => ({
      upload_id: f.name,
      sha256: "",
      size: f.size,
      filename: f.name,
      format: "markdown",
      expires_at: "",
    }));
    vi.mocked(ingestKnowledge).mockImplementation(async (_id, o) => ({
      ingestion_id: o.source_uri,
      state: "pending",
      source_uri: o.source_uri,
    }));
    vi.mocked(knowledgeIngestStatus).mockImplementation(async (_id, job) => ({
      found: true,
      ingestion_id: job,
      source_uri: job,
      state: "ready",
      stage: "",
      error_code: "",
      error_message: "",
      format: "markdown",
      coverage: null,
      warnings: [],
      chunks: 1,
      revision: 1,
    }));
  });
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    ingestClock.sleep = realSleep;
  });

  it("sends 250 files in two uploads from the dialog, each once", async () => {
    const entries = Array.from({ length: 250 }, (_, i) => ({
      file: new File(["x"], `f${String(i).padStart(3, "0")}.md`),
      path: "",
    }));
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="Research notes"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    expect(
      await screen.findByText(/The first 200 are sent now; then send the next ones from here/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Upload 200 files to Research notes" }));
    await user.click(await screen.findByRole("button", { name: "Upload the next 50 files" }, { timeout: 10_000 }));
    await waitFor(
      () => expect(within(screen.getByRole("list", { name: "Files" })).getAllByText("Revision 1")).toHaveLength(250),
      {
        timeout: 10_000,
      },
    );
    const sent = vi.mocked(ingestKnowledge).mock.calls.map(([, o]) => o.source_uri);
    expect(sent).toHaveLength(250);
    expect(new Set(sent).size).toBe(250);
    expect(screen.queryByRole("button", { name: /Upload the next/ })).toBeNull();
  }, 30_000);

  it("keeps the next upload when every file sent was already there", async () => {
    // Each ingest finds its source already live: published meanwhile, by a job whose answer was lost.
    vi.mocked(knowledgeIngestStatus).mockImplementation(
      async (_id, job) =>
        ({
          found: true,
          ingestion_id: job,
          source_uri: job,
          state: "failed",
          error_code: "revision_conflict",
          error_message: "the source has revision 1, not 0",
        }) as never,
    );
    const entries = Array.from({ length: 202 }, (_, i) => ({
      file: new File(["x"], `f${String(i).padStart(3, "0")}.md`),
      path: "",
    }));
    const onAdded = vi.fn();
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={onAdded}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Upload 200 files to n" }));
    expect(
      await screen.findByRole("button", { name: "Upload the next 2 files" }, { timeout: 10_000 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    // Those documents may be new to the list: the Library reloads.
    expect(onAdded).toHaveBeenCalled();
  }, 30_000);

  it("holds a large pick to one upload's limits before the check answers", () => {
    vi.mocked(listKnowledge).mockReturnValue(new Promise(() => {})); // the check never answers
    const entries = Array.from({ length: 250 }, (_, i) => ({
      file: new File(["x"], `f${String(i).padStart(3, "0")}.md`),
      path: "",
    }));
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/The first 200 are sent now/)).toBeInTheDocument();
  });

  it("counts on Retry only the failed files one upload sends", async () => {
    vi.mocked(ingestKnowledge).mockRejectedValue(new ApiError({ status: 400, code: "", message: "no", detail: {} }));
    const entries = Array.from({ length: MAX_BATCH_FILES + 1 }, (_, i) => ({
      file: new File(["x"], `f${String(i).padStart(3, "0")}.md`),
      path: "",
    }));
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: `Upload ${MAX_BATCH_FILES} files to n` }));
    await user.click(await screen.findByRole("button", { name: "Upload the next 1 file" }, { timeout: 10_000 }));
    expect(
      await screen.findByRole("button", { name: `Retry ${MAX_BATCH_FILES} failed` }, { timeout: 10_000 }),
    ).toBeInTheDocument();
  }, 30_000);

  it("asks before Done drops the files waiting for the next upload", async () => {
    const entries = Array.from({ length: 201 }, (_, i) => ({
      file: new File(["x"], `f${String(i).padStart(3, "0")}.md`),
      path: "",
    }));
    const onClose = vi.fn();
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={onClose}
      />,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Upload 200 files to n" }));
    await screen.findByRole("button", { name: "Upload the next 1 file" }, { timeout: 10_000 });
    const asked = vi.spyOn(window, "confirm").mockReturnValue(false);
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(asked).toHaveBeenCalledWith(expect.stringMatching(/1 is not sent yet\. Close anyway\?/));
    expect(onClose).not.toHaveBeenCalled();
    asked.mockRestore();
  }, 30_000);

  it("counts as sent only what reached the server, and says what was not sent", () => {
    const row = (state: BatchRow["state"], id: number) => ({ state, id }) as BatchRow;
    const rows = [row("done", 0), row("working", 1), row("failed", 2), row("not_sent", 3), row("not_sent", 4)];
    expect(progressLine(rows)).toBe("2 of 5 sent · 1 failed · 2 not sent");
  });

  it("sends where the files were picked for when the page moves to another place", async () => {
    const entries = [0, 1].map((i) => ({ file: new File(["x"], `f${i}.md`), path: "" }));
    const props = { entries, formats: FORMATS, onAdded: () => {}, onClose: () => {} };
    const { rerender } = render(
      <BatchUpload formatsRead {...props} identity={{ org: "o", namespace: "n" }} place="n" />,
    );
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 2 files to n" })).not.toHaveAttribute("aria-disabled"),
    );
    rerender(<BatchUpload formatsRead {...props} identity={{ org: "o", namespace: "elsewhere" }} place="elsewhere" />);
    await user.click(screen.getByRole("button", { name: "Upload 2 files to n" }));
    await waitFor(() => expect(ingestKnowledge).toHaveBeenCalledTimes(2));
    expect(vi.mocked(ingestKnowledge).mock.calls.every(([id]) => id.namespace === "n")).toBe(true);
  });

  it("leaves out what the deployment does not read once its formats arrive after the pick", async () => {
    const entries = [
      { file: new File(["x"], "a.md"), path: "" },
      { file: new File(["y"], "photo.png"), path: "" },
    ];
    const props = { entries, onAdded: () => {}, onClose: () => {}, identity: { org: "o", namespace: "n" }, place: "n" };
    const { rerender } = render(<BatchUpload formatsRead {...props} formats={null} />);
    expect(await screen.findByText("photo.png")).toBeInTheDocument();
    rerender(<BatchUpload formatsRead {...props} formats={FORMATS} />);
    expect(await screen.findByText("Not a format this deployment reads")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 1 file to n" })).not.toHaveAttribute("aria-disabled"),
    );
  });

  it("reloads the Library and counts the failure when the last file to finish is the one that decides", async () => {
    const onAdded = vi.fn();
    vi.mocked(knowledgeIngestStatus).mockImplementation(async (_id, job) => {
      // b finishes last, and is the only one added; a fails.
      if (job === "a.md")
        return { found: true, ingestion_id: job, source_uri: job, state: "failed", error_message: "no text" } as never;
      await new Promise((r) => setTimeout(r, 30));
      return { found: true, ingestion_id: job, source_uri: job, state: "ready", revision: 1 } as never;
    });
    const entries = ["a.md", "b.md"].map((n) => ({ file: new File(["x"], n), path: "" }));
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={onAdded}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 2 files to n" })).not.toHaveAttribute("aria-disabled"),
    );
    await user.click(screen.getByRole("button", { name: "Upload 2 files to n" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(screen.getByRole("status")).toHaveTextContent("1 of 2 sent · 1 failed");
    expect(toast).toHaveBeenCalledWith("1 added, 1 failed", "info");
  });

  it("a Stop confirmed after the run ended leaves the next run stoppable", async () => {
    let finish: (v: unknown) => void = () => {};
    vi.mocked(knowledgeIngestStatus).mockImplementation(
      () => new Promise((r) => (finish = () => r({ found: true, state: "failed", error_message: "no text" } as never))),
    );
    let answer: (v: boolean) => void = () => {};
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 1 file to n" })).not.toHaveAttribute("aria-disabled"),
    );
    await user.click(screen.getByRole("button", { name: "Upload 1 file to n" }));
    held.next = new Promise((r) => (answer = r));
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    finish(null); // the run ends while the confirm is open
    await screen.findByRole("button", { name: "Retry 1 failed" });
    answer(true);
    held.next = null;
    await user.click(screen.getByRole("button", { name: "Retry 1 failed" }));
    expect(await screen.findByRole("button", { name: "Stop" })).toBeEnabled();
  });

  it("holds the progress total still under Skip them", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({
      rows: [{ source_uri: "c.md" }, { source_uri: "d.md" }, { source_uri: "e.md" }],
      next_cursor: null,
    } as never);
    vi.mocked(knowledgeIngestStatus).mockReturnValue(new Promise(() => {})); // still processing
    const entries = ["a.md", "b.md", "c.md", "d.md", "e.md"].map((n) => ({ file: new File(["x"], n), path: "" }));
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    // Most files are already there: Skip is picked.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 2 files to n" })).not.toHaveAttribute("aria-disabled"),
    );
    await user.click(screen.getByRole("button", { name: "Upload 2 files to n" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/ of 2 sent/));
  });

  it("holds Upload until the formats are read, and says so when they cannot be", async () => {
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    const props = { entries, onAdded: () => {}, onClose: () => {}, identity: { org: "o", namespace: "n" }, place: "n" };
    const { rerender } = render(<BatchUpload {...props} formats={null} formatsRead={false} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 1 file to n" })).toHaveAttribute("aria-disabled"),
    );
    rerender(<BatchUpload {...props} formats={null} formatsRead />);
    expect(await screen.findByText(/Couldn't read which formats this deployment reads/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 1 file to n" })).not.toHaveAttribute("aria-disabled"),
    );
  });

  it("says once what a page closed mid-upload left, and reloads nothing", async () => {
    const onAdded = vi.fn();
    vi.mocked(knowledgeIngestStatus).mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 5));
      return { found: true, state: "processing", stage: "parse" } as never; // still processing
    });
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    const { unmount } = render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={onAdded}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 1 file to n" })).not.toHaveAttribute("aria-disabled"),
    );
    vi.mocked(toast).mockClear();
    await user.click(screen.getByRole("button", { name: "Upload 1 file to n" }));
    await waitFor(() => expect(ingestKnowledge).toHaveBeenCalled());
    unmount();
    await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
    expect(vi.mocked(toast).mock.calls[0][0]).toMatch(/^Upload stopped when the page closed: /);
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("says why the check failed after its own words, as a read", async () => {
    vi.mocked(listKnowledge).mockRejectedValue(new ApiError({ status: 502, code: "", detail: {} }));
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const alert = await screen.findByText(/^Couldn't check which files are already in n: /);
    expect(alert.textContent).not.toMatch(/saved|committed|\(/);
  });

  it("moves the focus to Upload when Check again succeeds", async () => {
    vi.mocked(listKnowledge).mockRejectedValueOnce(new ApiError({ status: 502, code: "", detail: {} }));
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Check again" }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Upload 1 file to n" })),
    );
  });

  it("hands the focus back to Check again when its read fails again", async () => {
    vi.mocked(listKnowledge).mockRejectedValue(new ApiError({ status: 502, code: "", detail: {} }));
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Check again" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Check again" })));
  });

  it("keeps Upload focusable and sends nothing when every file is skipped", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({
      rows: [{ source_uri: "a.md" }, { source_uri: "b.md" }],
      next_cursor: null,
    } as never);
    const entries = ["a.md", "b.md"].map((n) => ({ file: new File(["x"], n), path: "" }));
    render(
      <BatchUpload
        formatsRead
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={entries}
        formats={FORMATS}
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    const upload = await screen.findByRole("button", { name: "Upload 0 files to n" });
    await waitFor(() => expect(upload).toHaveAccessibleDescription(/No file to send/));
    expect(upload).toHaveAttribute("aria-disabled", "true");
    await userEvent.setup().click(upload);
    expect(uploadKnowledgeFile).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("keeps the person's Skip and what is already there when the formats arrive after the check", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [{ source_uri: "a.md" }], next_cursor: null } as never);
    const entries = ["a.md", "b.md", "c.md"].map((n) => ({ file: new File(["x"], n), path: "" }));
    const props = { entries, onAdded: () => {}, onClose: () => {}, identity: { org: "o", namespace: "n" }, place: "n" };
    const { rerender } = render(<BatchUpload formatsRead {...props} formats={null} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("radio", { name: /Skip them/ }));
    rerender(<BatchUpload formatsRead {...props} formats={FORMATS} />);
    expect(screen.getByRole("radio", { name: /Skip them/ })).toBeChecked();
    expect(screen.getByText("1 file is already in n")).toBeInTheDocument();
    expect(listKnowledge).toHaveBeenCalledTimes(1);
  });

  it("picks Skip once the formats leave mostly files already there, when nothing was picked", async () => {
    vi.mocked(listKnowledge).mockResolvedValue({ rows: [{ source_uri: "a.md" }], next_cursor: null } as never);
    const entries = ["a.md", "b.png", "c.png"].map((n) => ({ file: new File(["x"], n), path: "" }));
    const props = { entries, onAdded: () => {}, onClose: () => {}, identity: { org: "o", namespace: "n" }, place: "n" };
    const { rerender } = render(<BatchUpload formatsRead {...props} formats={null} />);
    expect(await screen.findByRole("radio", { name: /Replace them/ })).toBeChecked();
    rerender(<BatchUpload formatsRead {...props} formats={FORMATS} />);
    expect(screen.getByRole("radio", { name: /Skip them/ })).toBeChecked();
  });

  it("ends a run as usual under StrictMode, which runs each effect's cleanup once first", async () => {
    const onAdded = vi.fn();
    const entries = [{ file: new File(["x"], "a.md"), path: "" }];
    render(
      <StrictMode>
        <BatchUpload
          formatsRead
          identity={{ org: "o", namespace: "n" }}
          place="n"
          entries={entries}
          formats={FORMATS}
          onAdded={onAdded}
          onClose={() => {}}
        />
      </StrictMode>,
    );
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Upload 1 file to n" })).not.toHaveAttribute("aria-disabled"),
    );
    vi.mocked(toast).mockClear();
    await user.click(screen.getByRole("button", { name: "Upload 1 file to n" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(toast).toHaveBeenCalledWith("1 added", "ok");
  });
});
