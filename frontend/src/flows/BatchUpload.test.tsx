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
  createCollection: vi.fn(),
}));
// A confirm a test holds open (`held.next`), else the browser's confirm.
const held = vi.hoisted(() => ({ next: null as Promise<boolean> | null }));
vi.mock("../components/ConfirmDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/ConfirmDialog")>()),
  confirmAction: (opts: { title: string; message?: string }) =>
    held.next ?? Promise.resolve(window.confirm(opts.title + (opts.message ? `\n\n${opts.message}` : ""))),
}));
vi.mock("../lib/collectionPlaces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/collectionPlaces")>()),
  invalidateSharedCollections: vi.fn(),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
}));
import {
  createCollection,
  ingestKnowledge,
  type KnowledgeFormats,
  knowledgeIngestStatus,
  listKnowledge,
  uploadKnowledgeFile,
} from "../api";
import { toast } from "../components/Toast";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { type BatchRow, MAX_BATCH_FILES, REPEAT } from "../lib/batchIngest";
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

  it("checks a batch of loose files again page by page when the server says it may", async () => {
    vi.mocked(listKnowledge)
      .mockRejectedValueOnce(
        new ApiError({
          status: 503,
          code: "SERVER_BUSY",
          message: "busy",
          detail: { retryable: true, retry_after_s: 1 },
        }),
      )
      .mockResolvedValue({ rows: [{ source_uri: "a.md" }], next_cursor: null } as never);
    render(
      <BatchUpload
        identity={{ org: "o", namespace: "n" }}
        place="n"
        entries={["a.md", "b.md"].map((n) => ({ file: new File(["x"], n), path: "" }))}
        formats={FORMATS}
        formatsRead
        onAdded={() => {}}
        onClose={() => {}}
      />,
    );
    expect(await screen.findByText("Already in n: replaced")).toBeInTheDocument();
    expect(listKnowledge).toHaveBeenCalledTimes(2);
  });

  describe("folders", () => {
    const team = { namespace: "col-team", parent: "", personal: false, role: "writer", title: "Team" } as const;
    const mine = { namespace: "col-me-1", parent: "", personal: true, role: "admin" } as const;
    const folderEntries = (paths: string[]) =>
      paths.map((p) => ({ file: new File(["x"], p.split("/").pop() ?? p), path: p }));

    it("shows what the folder upload makes before anything is sent", async () => {
      const rows = [team, { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" }];
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md", "Q3/Drafts/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [...rows], shown: team, onMade: () => {} }}
        />,
      );
      const made = await screen.findByRole("list", { name: "Collections the folders go to" });
      expect(screen.getByText("Creates 1 collection in Team:")).toBeInTheDocument();
      expect(within(made).getByText("Q3 · 1 file · exists — files added to it")).toBeInTheDocument();
      expect(within(made).getByText("Q3 › Drafts · 1 file")).toBeInTheDocument();
      expect(screen.queryByRole("radiogroup")).toBeNull();
    });

    it("names a collection already there by its address when another shares its title", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      const other = { ...team, namespace: "col-q3-ab12", parent: "", title: "Q3" };
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3, other], shown: team, onMade: () => {} }}
        />,
      );
      const made = await screen.findByRole("list", { name: "Collections the folders go to" });
      expect(within(made).getByText("Q3 (col-q3) · 1 file · exists — files added to it")).toBeInTheDocument();
    });

    it("shows folders whose names differ only in case as one collection", async () => {
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Docs/a.md", "docs/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const made = await screen.findByRole("list", { name: "Collections the folders go to" });
      expect(screen.getByText("Creates 1 collection in Team:")).toBeInTheDocument();
      expect(within(made).getAllByRole("listitem")).toHaveLength(1);
      expect(within(made).getByText(/· 2 files$/)).toBeInTheDocument();
    });

    it("on My files, asks whether folders become top-level collections or keep only their paths", async () => {
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Q3/a.md", "Q3/Drafts/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine], shown: mine, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      // A folder of folders: collections first.
      const nest = await screen.findByRole("radio", { name: "Make Q3 a top-level collection" });
      expect(nest).toBeChecked();
      // Q3 sits at the top level; Drafts nests inside it.
      expect(screen.getByText("Creates 2 collections, 1 at the top level:")).toBeInTheDocument();
      await user.click(
        screen.getByRole("radio", { name: "Add the files to My files, keeping folder paths in their source" }),
      );
      expect(screen.queryByRole("list", { name: "Collections the folders go to" })).toBeNull();
      expect(screen.getByRole("button", { name: "Upload 2 files to My files" })).toBeInTheDocument();
      await user.click(screen.getByRole("radio", { name: "Make Q3 a top-level collection" }));
      expect(screen.getByRole("button", { name: "Upload 2 files into Q3" })).toBeInTheDocument();
    });

    it("says only the new collections inside a top-level one already there, on My files", async () => {
      const docs = { namespace: "col-docs", parent: "", personal: false, role: "admin", title: "Docs" } as const;
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Docs/a/x.md", "Docs/b/y.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine, docs], shown: mine, onMade: () => {} }}
        />,
      );
      expect(await screen.findByText("Creates 2 collections:")).toBeInTheDocument();
    });

    it("names the collection that folders differing only in case become on the Upload button", async () => {
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Docs/a.md", "docs/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine], shown: mine, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(
        await screen.findByRole("radio", {
          name: "Make Docs a top-level collection",
        }),
      );
      expect(screen.getByRole("button", { name: "Upload 2 files into Docs" })).toBeInTheDocument();
    });

    it("under Skip, sends all of a folder's files when the server makes a new collection, not the one checked", async () => {
      // Someone else's top-level collection the person is admin of: the server reuses only
      // the caller's own, so it makes a new one.
      const reports = {
        namespace: "col-reports",
        parent: "",
        personal: false,
        role: "admin",
        title: "Reports",
      } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-reports" ? [{ source_uri: "a.md" }, { source_uri: "b.md" }] : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-reports-2",
        parent: "",
        created: true,
        title: "Reports",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Reports/a.md", "Reports/b.md", "Reports/c.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine, reports], shown: mine, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("radio", { name: "Add the files to Reports, its folders inside it" }));
      expect(await screen.findByRole("radio", { name: "Skip them" })).toBeChecked();
      await user.click(screen.getByRole("button", { name: "Upload 1 file into Reports" }));
      // The collection made holds none of them: all three go, in the one upload.
      await screen.findByRole("button", { name: "Done" });
      expect(ingestKnowledge).toHaveBeenCalledTimes(3);
      expect(new Set(vi.mocked(ingestKnowledge).mock.calls.map(([id]) => id.namespace))).toEqual(
        new Set(["col-reports-2"]),
      );
    });

    it("under Skip, sends the new files when every one of the first 200 is already there", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      const names = Array.from({ length: 250 }, (_, i) => `f${String(i).padStart(3, "0")}.md`);
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-q3" ? names.slice(0, 200).map((n) => ({ source_uri: n })) : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(names.map((n) => `Q3/${n}`))}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      expect(await screen.findByRole("radio", { name: "Skip them" })).toBeChecked();
      const upload = await screen.findByRole("button", { name: "Upload 50 files to Team" });
      await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
      await user.click(upload);
      await screen.findByRole("button", { name: "Done" }, { timeout: 10_000 });
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([, o]) => o.source_uri)).toEqual(names.slice(200));
      expect(screen.queryByRole("button", { name: /^Upload the next/ })).toBeNull();
    }, 30_000);

    it("under Skip, holds to one upload's limits when the server makes a new collection, not the one checked", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      const names = Array.from({ length: 450 }, (_, i) => `f${String(i).padStart(3, "0")}.md`);
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-q3" ? names.slice(0, 300).map((n) => ({ source_uri: n })) : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3-2",
        parent: "col-team",
        created: true,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(names.map((n) => `Q3/${n}`))}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      expect(await screen.findByRole("radio", { name: "Skip them" })).toBeChecked();
      await user.click(await screen.findByRole("button", { name: "Upload 150 files to Team" }));
      // The collection made holds none of them: all 450 go, 200 at a time.
      await user.click(await screen.findByRole("button", { name: "Upload the next 200 files" }, { timeout: 10_000 }));
      await user.click(await screen.findByRole("button", { name: "Upload the next 50 files" }, { timeout: 10_000 }));
      await screen.findByRole("button", { name: "Done" }, { timeout: 10_000 });
      expect(ingestKnowledge).toHaveBeenCalledTimes(450);
      expect(new Set(vi.mocked(ingestKnowledge).mock.calls.map(([id]) => id.namespace))).toEqual(new Set(["col-q3-2"]));
    }, 60_000);

    it("under Skip, sends a folder's files where the server put it, skipping only what that collection holds", async () => {
      const docs = { ...team, namespace: "col-docs", parent: "col-team", title: "Docs" };
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows:
              id.namespace === "col-docs"
                ? [{ source_uri: "README.md" }, { source_uri: "a.md" }, { source_uri: "b.md" }]
                : id.namespace === "col-docs-old"
                  ? [{ source_uri: "b.md" }]
                  : [],
            next_cursor: null,
          }) as never,
      );
      // The server reuses another collection than the one checked, which holds b.md.
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-docs-old",
        parent: "col-team",
        created: false,
        title: "Docs",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          // docs/README.md repeats Docs/README.md: left out, though the check marks it too.
          entries={folderEntries(["Docs/README.md", "docs/README.md", "Docs/a.md", "Docs/b.md", "Docs/c.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, docs], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      expect(await screen.findByRole("radio", { name: "Skip them" })).toBeChecked();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file to Team" }));
      await screen.findByRole("button", { name: "Done" });
      expect(
        vi
          .mocked(ingestKnowledge)
          .mock.calls.map(([id, o]) => [id.namespace, o.source_uri])
          .sort(),
      ).toEqual([
        ["col-docs-old", "README.md"],
        ["col-docs-old", "a.md"],
        ["col-docs-old", "c.md"],
      ]);
      expect(screen.getByText("Same path as another file here")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Upload the next/ })).toBeNull();
    });

    it("under Skip, sends a parent folder's files into the collection made for it on a subfolder's way", async () => {
      // Someone else's top-level Docs: the server makes the person's own instead.
      const docs = { namespace: "col-docs", parent: "", personal: false, role: "admin", title: "Docs" } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-docs" ? [{ source_uri: "a.md" }, { source_uri: "x.md" }] : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
        namespace: o?.title === "Docs" ? "col-docs-2" : "col-sub",
        parent: o?.parent ?? "",
        created: true,
        title: o?.title,
      }));
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Docs/a.md", "Docs/x.md", "Docs/sub/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine, docs], shown: mine, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      expect(await screen.findByRole("radio", { name: "Skip them" })).toBeChecked();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file into Docs" }));
      await screen.findByRole("button", { name: "Done" });
      expect(
        vi
          .mocked(ingestKnowledge)
          .mock.calls.map(([id, o]) => [id.namespace, o.source_uri])
          .sort(),
      ).toEqual([
        ["col-docs-2", "a.md"],
        ["col-docs-2", "x.md"],
        ["col-sub", "b.md"],
      ]);
    });

    it("under Skip, sends a subfolder's files into a new collection when the folder above it went to another", async () => {
      const docs = { namespace: "col-docs", parent: "", personal: false, role: "admin", title: "Docs" } as const;
      const sub = {
        namespace: "col-docs-sub",
        parent: "col-docs",
        personal: false,
        role: "admin",
        title: "sub",
      } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-docs-sub" ? [{ source_uri: "b.md" }, { source_uri: "c.md" }] : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
        namespace: o?.title === "Docs" ? "col-docs-2" : "col-sub-2",
        parent: o?.parent ?? "",
        created: true,
        title: o?.title,
      }));
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Docs/new.md", "Docs/sub/b.md", "Docs/sub/c.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine, docs, sub], shown: mine, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      expect(await screen.findByRole("radio", { name: "Skip them" })).toBeChecked();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file into Docs" }));
      await screen.findByRole("button", { name: "Done" });
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([id, o]) => [id.namespace, o.source_uri])).toEqual([
        ["col-docs-2", "new.md"],
        ["col-sub-2", "b.md"],
        ["col-sub-2", "c.md"],
      ]);
    });

    it("keeps files held for the next upload held when the collection made holds more than the check saw", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      const names = Array.from({ length: 450 }, (_, i) => `f${String(i).padStart(3, "0")}.md`);
      // The check sees 150 of them; the collection the server answers with holds 350.
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: (id.namespace === "col-q3"
              ? names.slice(0, 150)
              : id.namespace === "col-q3-x"
                ? names.slice(0, 350)
                : []
            ).map((n) => ({ source_uri: n })),
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3-x",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(names.map((n) => `Q3/${n}`))}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("radio", { name: "Skip them" }));
      await user.click(await screen.findByRole("button", { name: "Upload 200 files to Team" }));
      // Every file this upload placed is there already; the 100 held stay held, not sent unplaced.
      await user.click(await screen.findByRole("button", { name: "Upload the next 100 files" }, { timeout: 10_000 }));
      await screen.findByRole("button", { name: "Done" }, { timeout: 10_000 });
      expect(new Set(vi.mocked(ingestKnowledge).mock.calls.map(([id]) => id.namespace))).toEqual(new Set(["col-q3-x"]));
      expect(ingestKnowledge).toHaveBeenCalledTimes(100);
    }, 30_000);

    it("sends files held for the next upload that share a name across folders, each into its own", async () => {
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
        namespace: `col-${(o?.title ?? "").toLowerCase()}`,
        parent: o?.parent ?? "",
        created: true,
        title: o?.title,
      }));
      const many = Array.from({ length: 200 }, (_, i) => `a/f${String(i).padStart(3, "0")}.md`);
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries([...many, "b/README.md", "c/README.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 200 files to Team" }));
      await user.click(await screen.findByRole("button", { name: "Upload the next 2 files" }, { timeout: 10_000 }));
      await screen.findByRole("button", { name: "Done" }, { timeout: 10_000 });
      expect(
        vi
          .mocked(ingestKnowledge)
          .mock.calls.filter(([, o]) => o.source_uri === "README.md")
          .map(([id]) => id.namespace)
          .sort(),
      ).toEqual(["col-b", "col-c"]);
    }, 30_000);

    it("fails, for Retry failed, the files of a collection whose holdings could not be read", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      let reads = 0;
      vi.mocked(listKnowledge).mockImplementation(async (id) => {
        // The check reads col-q3; the server answers with another, whose first read fails.
        if (id.namespace === "col-q3-x" && reads++ === 0)
          throw new ApiError({ status: 500, code: "INTERNAL", message: "boom" });
        return { rows: [], next_cursor: null } as never;
      });
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3-x",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file to Team" }));
      expect(await screen.findByText(/^Not sent: couldn't check what is already in it: /)).toBeInTheDocument();
      expect(ingestKnowledge).not.toHaveBeenCalled();
      await user.click(screen.getByRole("button", { name: "Retry 1 failed" }));
      await waitFor(() => expect(ingestKnowledge).toHaveBeenCalledTimes(1));
    });

    it("replaces no file in a collection the check did not read unless the person picked Replace", async () => {
      // Another member made Specs after the Library listed its collections: the server reuses it.
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({ rows: id.namespace === "col-specs" ? [{ source_uri: "a.md" }] : [], next_cursor: null }) as never,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-specs",
        parent: "col-team",
        created: false,
        title: "Specs",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Specs/a.md", "Specs/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      expect(await screen.findByText("Creates 1 collection in Team:")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Upload 2 files to Team" }));
      expect(await screen.findByText("Already there: not replaced, since no one was asked")).toBeInTheDocument();
      await screen.findByRole("button", { name: "Done" });
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([, o]) => o.source_uri)).toEqual(["b.md"]);
    });

    it("replaces a file in a collection the check did not read when the person picked Replace", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows:
              id.namespace === "col-q3"
                ? [{ source_uri: "q.md" }]
                : id.namespace === "col-specs"
                  ? [{ source_uri: "a.md", title: "Spec A", tags: ["kept"] }]
                  : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
        namespace: o?.title === "Q3" ? "col-q3" : "col-specs",
        parent: "col-team",
        created: false,
        title: o?.title,
      }));
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/q.md", "Specs/a.md", "Specs/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      // Picked by the person (Replace is the default until then).
      await user.click(await screen.findByRole("radio", { name: "Skip them" }));
      await user.click(screen.getByRole("radio", { name: "Replace them with these files" }));
      await user.click(screen.getByRole("button", { name: "Upload 3 files to Team" }));
      await screen.findByRole("button", { name: "Done" });
      const a = vi.mocked(ingestKnowledge).mock.calls.find(([, o]) => o.source_uri === "a.md");
      expect(a?.[0].namespace).toBe("col-specs");
      expect(a?.[1]).not.toHaveProperty("expected_revision");
      expect(a?.[1]).toMatchObject({ title: "Spec A", tags: ["kept"] });
    });

    it("skips the files no one was asked about, and sends the held ones in the next upload", async () => {
      const names = Array.from({ length: 250 }, (_, i) => `f${String(i).padStart(3, "0")}.md`);
      // The server reuses a collection no one checked, holding the first 100.
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-q3" ? names.slice(0, 100).map((n) => ({ source_uri: n })) : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(names.map((n) => `Q3/${n}`))}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 200 files to Team" }));
      // The 100 found are skipped, not sent; the 50 held have no collection yet and go next.
      await user.click(await screen.findByRole("button", { name: "Upload the next 50 files" }, { timeout: 10_000 }));
      await screen.findByRole("button", { name: "Done" }, { timeout: 10_000 });
      expect(ingestKnowledge).toHaveBeenCalledTimes(150);
      expect(screen.getAllByText("Already there: not replaced, since no one was asked")).toHaveLength(100);
    }, 30_000);

    it("replaces nothing unasked when the server gives an untitled collection to the other folder that spells it", async () => {
      // The person is its admin: the check predicts it for My Docs, whose file comes first.
      const myDocs = { namespace: "col-my-docs", parent: "col-team", personal: false, role: "admin" } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({ rows: id.namespace === "col-my-docs" ? [{ source_uri: "a.md" }] : [], next_cursor: null }) as never,
      );
      // The other create is answered first: my-docs gets it, My Docs a new one.
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) =>
        o?.title === "my-docs"
          ? { namespace: "col-my-docs", parent: "col-team", created: false, title: "my-docs" }
          : { namespace: "col-my-docs-2", parent: "col-team", created: true, title: o?.title },
      );
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["My Docs/x.md", "my-docs/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, myDocs], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 2 files to Team" }));
      await screen.findByRole("button", { name: "Done" });
      expect(screen.getByText("Already there: not replaced, since no one was asked")).toBeInTheDocument();
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([, o]) => o.source_uri)).toEqual(["x.md"]);
    });

    it("has the shared collection list read again once a folder upload made its collections", async () => {
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: true,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file to Team" }));
      await screen.findByRole("button", { name: "Done" });
      expect(invalidateSharedCollections).toHaveBeenCalledTimes(1);
    });

    it("names a folder's collection the check could not read whole, and reads it no more at upload", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      // A collection past what the check reads: every page has another after it.
      vi.mocked(listKnowledge).mockImplementation(async () => ({ rows: [], next_cursor: "more" }) as never);
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      expect(await screen.findByText(/^Q3 holds over 10,000 documents/)).toBeInTheDocument();
      const reads = vi.mocked(listKnowledge).mock.calls.length;
      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Upload 1 file to Team" }));
      await screen.findByRole("button", { name: "Done" });
      expect(vi.mocked(listKnowledge).mock.calls.length).toBe(reads);
    });

    it("names a collection the upload could not read whole when the check did not predict it", async () => {
      vi.mocked(listKnowledge).mockImplementation(async () => ({ rows: [], next_cursor: "more" }) as never);
      // Not in the list: the server answers with one that is already there.
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file to Team" }));
      await screen.findByRole("button", { name: "Done" });
      expect(screen.getByText(/^Q3 holds over 10,000 documents/)).toBeInTheDocument();
    });

    it("says a placed file waits its turn once its collection is made", async () => {
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: true,
        title: "Q3",
      });
      vi.mocked(ingestKnowledge).mockReturnValue(new Promise(() => {})); // the first three stay open
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md", "Q3/b.md", "Q3/c.md", "Q3/d.md", "Q3/e.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 5 files to Team" }));
      await waitFor(() => expect(ingestKnowledge).toHaveBeenCalledTimes(3));
      expect(screen.getAllByText("Not sent yet")).toHaveLength(2);
      expect(screen.queryByText("Making its collection…")).toBeNull();
    });

    it("counts once a file two folders send to one untitled collection, before the upload", async () => {
      // Not the person's to title: the check predicts it for both folders.
      const notes = { namespace: "col-notes", parent: "col-team", personal: false, role: "writer" } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({ rows: id.namespace === "col-notes" ? [{ source_uri: "README.md" }] : [], next_cursor: null }) as never,
      );
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Notes!/README.md", "Notes/README.md", "Notes/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, notes], shown: team, onMade: () => {} }}
        />,
      );
      expect(await screen.findByRole("radiogroup", { name: "1 file is already there" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Upload 2 files to Team" })).toBeInTheDocument();
    });

    it("sends a file of a name once into one collection when its twin is held for the next upload", async () => {
      // Not the person's to title: the server answers both Zoo folders with it.
      const zoo = { namespace: "col-zoo", parent: "col-team", personal: false, role: "writer" } as const;
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) =>
        o?.title === "a"
          ? { namespace: "col-a", parent: "col-team", created: true, title: "a" }
          : { namespace: "col-zoo", parent: "col-team", created: false, title: "" },
      );
      // 199 files sort first, then the twins: one goes now, the other waits for the next upload.
      const many = Array.from({ length: 199 }, (_, i) => `a/f${String(i).padStart(3, "0")}.md`);
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries([...many, "Zoo/README.md", "Zoo!/README.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, zoo], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 200 files to Team" }, { timeout: 10_000 }));
      await user.click(await screen.findByRole("button", { name: "Upload the next 1 file" }, { timeout: 10_000 }));
      await screen.findByRole("button", { name: "Done" }, { timeout: 10_000 });
      expect(vi.mocked(ingestKnowledge).mock.calls.filter(([, o]) => o.source_uri === "README.md")).toHaveLength(1);
      expect(screen.getByText(REPEAT)).toBeInTheDocument();
    }, 30_000);

    it("skips, as picked, a file in a collection the check did not read when the person picked Skip", async () => {
      const q3 = { ...team, namespace: "col-q3", parent: "col-team", title: "Q3" };
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows:
              id.namespace === "col-q3"
                ? [{ source_uri: "q.md" }]
                : id.namespace === "col-specs"
                  ? [{ source_uri: "a.md" }]
                  : [],
            next_cursor: null,
          }) as never,
      );
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
        namespace: o?.title === "Q3" ? "col-q3" : "col-specs",
        parent: "col-team",
        created: false,
        title: o?.title,
      }));
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/q.md", "Specs/a.md", "Specs/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, q3], shown: team, onMade: () => {} }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("radio", { name: "Skip them" }));
      await user.click(screen.getByRole("button", { name: "Upload 2 files to Team" }));
      await screen.findByRole("button", { name: "Done" });
      expect(screen.getAllByText("Already here")).toHaveLength(2);
      expect(screen.queryByText("Already there: not replaced, since no one was asked")).toBeNull();
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([, o]) => o.source_uri)).toEqual(["b.md"]);
    });

    it("hands the Library the collection a folder upload made, and skips what a reused one holds", async () => {
      const docs = { namespace: "col-docs", parent: "", personal: false, role: "admin", title: "Docs" } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace.startsWith("col-docs") ? [{ source_uri: "a.md" }, { source_uri: "x.md" }] : [],
            next_cursor: null,
          }) as never,
      );
      // The person's own Docs, not the one listed: it holds both files too.
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) =>
        o?.title === "Docs"
          ? { namespace: "col-docs-own", parent: "", created: false, title: "Docs" }
          : { namespace: "col-sub", parent: o?.parent ?? "", created: true, title: o?.title },
      );
      const onMade = vi.fn();
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={folderEntries(["Docs/a.md", "Docs/x.md", "Docs/sub/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine, docs], shown: mine, onMade }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file into Docs" }));
      await user.click(await screen.findByRole("button", { name: "Done" }));
      expect(vi.mocked(ingestKnowledge).mock.calls.map(([id, o]) => [id.namespace, o.source_uri])).toEqual([
        ["col-sub", "b.md"],
      ]);
      expect(onMade).toHaveBeenCalledWith("col-sub");
    });

    it("sends a file once when two folders go to one untitled collection both their names spell", async () => {
      // Not the person's to title, so the server answers both folders with it.
      const fooBar = { namespace: "col-foo-bar", parent: "col-team", personal: false, role: "writer" } as const;
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-foo-bar" ? [{ source_uri: "a.md" }] : [],
            next_cursor: null,
          }) as never,
      );
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Foo Bar/a.md", "foo_bar/a.md", "Foo Bar/b.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, fooBar], shown: team, onMade: () => {} }}
        />,
      );
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-foo-bar",
        parent: "col-team",
        created: false,
        title: "",
      });
      const user = userEvent.setup();
      await user.click(await screen.findByRole("radio", { name: "Replace them with these files" }));
      await user.click(screen.getByRole("button", { name: "Upload 2 files to Team" }));
      expect(await screen.findByText(REPEAT)).toBeInTheDocument();
      await screen.findByRole("button", { name: "Done" });
      expect(
        vi
          .mocked(ingestKnowledge)
          .mock.calls.map(([, o]) => o.source_uri)
          .sort(),
      ).toEqual(["a.md", "b.md"]);
    });

    it("lists the Library's collections again on close when a Stop cut a collection's create short", async () => {
      let refuse: (e: unknown) => void = () => {};
      vi.mocked(createCollection).mockReturnValue(
        new Promise((_ok, fail) => {
          refuse = fail;
        }),
      );
      const onMade = vi.fn();
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file to Team" }));
      await screen.findByText("Making its collection…");
      held.next = Promise.resolve(true);
      await user.click(screen.getByRole("button", { name: "Stop" }));
      held.next = null;
      refuse(new DOMException("aborted", "AbortError")); // the server may have made it all the same
      held.next = Promise.resolve(true);
      await user.click(await screen.findByRole("button", { name: "Done" }));
      held.next = null;
      expect(onMade).toHaveBeenCalledWith("");
    });

    it("checks again what is already there when late formats switch the folders to paths", async () => {
      // An earlier upload added the folder's file here by its path.
      vi.mocked(listKnowledge).mockResolvedValue({ rows: [{ source_uri: "docs/a.md" }], next_cursor: null } as never);
      const props = {
        identity: { org: "o", namespace: "col-me-1" },
        place: "My files",
        entries: [...folderEntries(["docs/a.md", "docs/img/x.png"]), { file: new File(["x"], "notes.md"), path: "" }],
        onAdded: () => {},
        onClose: () => {},
        tree: { rows: [mine], shown: mine, onMade: () => {} },
      };
      const { rerender } = render(<BatchUpload {...props} formats={null} formatsRead={false} />);
      expect(await screen.findByRole("radio", { name: "Make docs a top-level collection" })).toBeChecked();
      await waitFor(() => expect(listKnowledge).toHaveBeenCalledTimes(1));
      // The picture is left out: docs holds no folder that is sent, so its files keep their paths.
      rerender(<BatchUpload {...props} formats={FORMATS} formatsRead />);
      expect(await screen.findByText("Already in My files: replaced")).toBeInTheDocument();
      expect(listKnowledge).toHaveBeenCalledTimes(2);
    });

    it("starts the folders choice again from the formats that arrive late, until the person picks", async () => {
      const props = {
        identity: { org: "o", namespace: "col-me-1" },
        place: "My files",
        entries: folderEntries(["Q3/a.md", "Q3/Old/pic.png"]),
        onAdded: () => {},
        onClose: () => {},
        tree: { rows: [mine], shown: mine, onMade: () => {} },
      };
      const { rerender } = render(<BatchUpload {...props} formats={null} formatsRead={false} />);
      expect(await screen.findByRole("radio", { name: "Make Q3 a top-level collection" })).toBeChecked();
      // The picture is left out: Q3 holds no folder that is sent.
      rerender(<BatchUpload {...props} formats={FORMATS} formatsRead />);
      expect(
        await screen.findByRole("radio", { name: "Add the files to My files, keeping folder paths in their source" }),
      ).toBeChecked();
    });

    it("names My files beside the folders' collection, and says a top-level collection belongs to the org", async () => {
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={[...folderEntries(["Q3/a.md", "Q3/Old/b.md"]), { file: new File(["x"], "notes.md"), path: "" }]}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine], shown: mine, onMade: () => {} }}
        />,
      );
      const nest = await screen.findByRole("radio", { name: "Make Q3 a top-level collection" });
      expect(nest).toHaveAccessibleDescription(
        "A top-level collection belongs to the org: only the members you add read it.",
      );
      expect(screen.getByRole("button", { name: "Upload 3 files into Q3 and My files" })).toBeInTheDocument();
    });

    it("lists the Library's collections again on close when the upload gave a listed one a title", async () => {
      const untitled = { namespace: "col-q3", parent: "col-team", personal: false, role: "admin" } as const;
      // The person is its admin: the server titles it for the folder.
      vi.mocked(createCollection).mockResolvedValue({
        namespace: "col-q3",
        parent: "col-team",
        created: false,
        title: "Q3",
      });
      const onMade = vi.fn();
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={folderEntries(["Q3/a.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team, untitled], shown: team, onMade }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 1 file to Team" }));
      await user.click(await screen.findByRole("button", { name: "Done" }));
      expect(onMade).toHaveBeenCalledWith("");
    });

    it("treats a folder whose files are all left out as no folder", async () => {
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-me-1" }}
          place="My files"
          entries={[
            ...folderEntries(["Pics/a.png", "Pics/Old/b.png"]),
            { file: new File(["x"], "notes.md"), path: "" },
          ]}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [mine], shown: mine, onMade: () => {} }}
        />,
      );
      expect(await screen.findByRole("button", { name: "Upload 1 file to My files" })).toBeInTheDocument();
      expect(screen.queryByRole("radiogroup", { name: "The folders" })).toBeNull();
      expect(screen.queryByRole("list", { name: "Collections the folders go to" })).toBeNull();
    });

    it("refuses folders nested deeper than collections may, naming them", async () => {
      const chain = Array.from({ length: 8 }, (_, i) => ({
        namespace: `col-${i}`,
        parent: i ? `col-${i - 1}` : "",
        personal: false,
        role: "writer" as const,
        title: `L${i}`,
      }));
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-7" }}
          place="L7"
          entries={folderEntries(["A/B/c.md"])}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: chain, shown: chain[7], onMade: () => {} }}
        />,
      );
      expect(
        await screen.findByText(/Collections nest at most 8 deep\. These folders are too deep here: A\/B\./),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Upload 1 file to L7" })).toHaveAttribute("aria-disabled", "true");
    });

    it("keeps each folder's check marks, and drops its left-out folders, when the formats arrive late", async () => {
      const docs = { ...team, namespace: "col-docs", parent: "col-team", title: "Docs" };
      const specs = { ...team, namespace: "col-specs", parent: "col-team", title: "Specs" };
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-docs" ? [{ source_uri: "README.md", title: "Docs readme" }] : [],
            next_cursor: null,
          }) as never,
      );
      const props = {
        identity: { org: "o", namespace: "col-team" },
        place: "Team",
        entries: folderEntries(["Docs/README.md", "Specs/README.md", "Pics/a.png"]),
        onAdded: () => {},
        onClose: () => {},
        tree: { rows: [team, docs, specs], shown: team, onMade: () => {} },
      };
      const { rerender } = render(<BatchUpload {...props} formats={null} formatsRead={false} />);
      const made = await screen.findByRole("list", { name: "Collections the folders go to" });
      expect(within(made).getByText("Pics · 1 file")).toBeInTheDocument();
      await waitFor(() => expect(screen.getByText("Already in Docs: replaced")).toBeInTheDocument());
      rerender(<BatchUpload {...props} formats={FORMATS} formatsRead />);
      expect(within(made).queryByText("Pics · 1 file")).toBeNull();
      expect(screen.getByText("Adds the files to collections already in Team:")).toBeInTheDocument();
      expect(screen.getAllByText(/^Already in /)).toHaveLength(1);
      expect(screen.getByText("Already in Docs: replaced")).toBeInTheDocument();
    });

    it("checks again when the formats leave out the folder a collection was predicted for", async () => {
      // Untitled, the person its admin: "Notes" takes it, "Notes." is new, until Notes goes.
      const notes = { ...team, namespace: "col-notes", parent: "col-team", title: "", role: "admin" as const };
      vi.mocked(listKnowledge).mockImplementation(
        async (id) =>
          ({
            rows: id.namespace === "col-notes" ? [{ source_uri: "b.md", title: "Old b" }] : [],
            next_cursor: null,
          }) as never,
      );
      const props = {
        identity: { org: "o", namespace: "col-team" },
        place: "Team",
        entries: folderEntries(["Notes/a.png", "Notes./b.md"]),
        onAdded: () => {},
        onClose: () => {},
        tree: { rows: [team, notes], shown: team, onMade: () => {} },
      };
      const { rerender } = render(<BatchUpload {...props} formats={null} formatsRead={false} />);
      await waitFor(() => expect(vi.mocked(listKnowledge)).toHaveBeenCalled());
      expect(screen.queryByText(/^Already in /)).toBeNull();
      rerender(<BatchUpload {...props} formats={FORMATS} formatsRead />);
      expect(await screen.findByText(/^Already in col-notes: /)).toBeInTheDocument();
    });

    it("refuses an upload that makes more collections than one may, or a folder name the server refuses", async () => {
      const many = folderEntries([...Array.from({ length: 101 }, (_, i) => `F${i}/a.md`), `${"x".repeat(201)}/b.md`]);
      render(
        <BatchUpload
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          entries={many}
          formats={FORMATS}
          formatsRead
          onAdded={() => {}}
          onClose={() => {}}
          tree={{ rows: [team], shown: team, onMade: () => {} }}
        />,
      );
      expect(
        await screen.findByText(/^102 new collections: one pick of folders makes at most 100\./),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          /^These folder names can't be collection titles: x{201} \(longer than 200 characters\)\. Rename them/,
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^Upload 102 files to Team$/ })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
    });

    it("keeps sending into the folders' collections while the Library reloads its list", async () => {
      vi.mocked(createCollection).mockImplementation(async (_org, _name, o) => ({
        namespace: `col-${(o?.title ?? "").toLowerCase()}`,
        parent: o?.parent ?? "",
        created: true,
        title: o?.title,
      }));
      const onMade = vi.fn();
      const entries = folderEntries(Array.from({ length: 201 }, (_, i) => `Q3/f${String(i).padStart(3, "0")}.md`));
      const props = { entries, formats: FORMATS, formatsRead: true, onAdded: () => {}, onClose: () => {} };
      const { rerender } = render(
        <BatchUpload
          {...props}
          identity={{ org: "o", namespace: "col-team" }}
          place="Team"
          tree={{ rows: [team], shown: team, onMade }}
        />,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Upload 200 files to Team" }));
      const next = await screen.findByRole("button", { name: "Upload the next 1 file" }, { timeout: 10_000 });
      // The Library's list reloads: no collection is shown, the header's namespace is.
      rerender(<BatchUpload {...props} identity={{ org: "o", namespace: "header-ns" }} place="header-ns" />);
      await user.click(next);
      await waitFor(() => expect(ingestKnowledge).toHaveBeenCalledTimes(201), { timeout: 10_000 });
      expect(new Set(vi.mocked(ingestKnowledge).mock.calls.map(([id]) => id.namespace))).toEqual(new Set(["col-q3"]));
      expect(onMade).not.toHaveBeenCalled(); // the list is told once the dialog closes
    }, 30_000);
  });
});
