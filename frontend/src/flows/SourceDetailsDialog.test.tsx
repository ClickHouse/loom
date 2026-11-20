import { cleanup, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeSourceDetail } from "../lib/knowledgeSources";
import * as api from "../api/knowledgeSources";
import { SourceDetailsDialog } from "./SourceDetailsDialog";

const DETAIL: KnowledgeSourceDetail = {
  name: "design-docs",
  kind: "gdrive",
  config: { folder_ids: ["abc", "def"] },
  created_by: "alice",
  created_at: "",
  sync: {
    requested_at: "",
    last_synced_at: "",
    status: "failed",
    documents_seen: 0,
    changed: 0,
    removed: 0,
    unconverted: 0,
    unconverted_reasons: {},
    skipped: 0,
    error: "http_403",
  },
  documents: [{ source_uri: "a.md", title: "Runbook", source_revision: "1", ingested_at: "2000-01-01 00:00:00" }],
};

vi.mock("../api/knowledgeSources", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/knowledgeSources")>()),
  inspectKnowledgeSource: vi.fn(),
}));

beforeEach(() => vi.mocked(api.inspectKnowledgeSource).mockResolvedValue(DETAIL));
afterEach(() => cleanup());

describe("SourceDetailsDialog", () => {
  it("shows the source's settings, its last sync and the documents it wrote", async () => {
    render(<SourceDetailsDialog org="acme" namespace="code-x" name="design-docs" onClose={() => {}} />);
    expect(await screen.findByText("abc, def")).toBeInTheDocument();
    expect(screen.getByText("http_403")).toBeInTheDocument();
    expect(screen.getByText("Runbook")).toBeInTheDocument();
    expect(screen.getByText("Sync failed")).toBeInTheDocument();
  });

  it("words the last turn as the source row does, and shows an ok turn's note as a note", async () => {
    vi.mocked(api.inspectKnowledgeSource).mockResolvedValueOnce({
      ...DETAIL,
      sync: { ...DETAIL.sync, status: "ok", error: "3 files too large" },
    });
    render(<SourceDetailsDialog org="acme" namespace="code-x" name="design-docs" onClose={() => {}} />);
    expect(await screen.findByText(/Synced with a note/)).toBeInTheDocument();
    expect(screen.getByText("Note")).toBeInTheDocument();
    expect(screen.getByText("3 files too large")).toBeInTheDocument();
    expect(screen.queryByText("Error")).toBeNull();
  });

  it("shows a synced source's last turn time beside its badge", async () => {
    vi.mocked(api.inspectKnowledgeSource).mockResolvedValueOnce({
      ...DETAIL,
      sync: { ...DETAIL.sync, status: "ok", error: "", last_synced_at: "2000-01-01 00:00:00" },
    });
    render(<SourceDetailsDialog org="acme" namespace="code-x" name="design-docs" onClose={() => {}} />);
    const lastSync = await screen.findByText(/· Synced/);
    expect(lastSync.textContent).toContain("2000");
  });

  it("shows what a source of items reads, which its config form cannot hold", async () => {
    vi.mocked(api.inspectKnowledgeSource).mockResolvedValueOnce({
      ...DETAIL,
      config: {},
      items: [
        { type: "folder", id: "abc", recursive: true },
        { type: "filter", name_contains: "Roadmap" },
        { type: "shared_drive", id: "0AbC" },
      ],
      include: ["docs", "pdf"],
    });
    render(<SourceDetailsDialog org="acme" namespace="code-x" name="design-docs" onClose={() => {}} />);
    expect(await screen.findByText("Folder abc (with subfolders)")).toBeInTheDocument();
    expect(screen.getByText("Files matching name contains: Roadmap")).toBeInTheDocument();
    expect(screen.getByText("Shared drive 0AbC")).toBeInTheDocument();
    expect(screen.getByText("docs, pdf")).toBeInTheDocument();
  });

  it("shows a recording source's config, which answers no items", async () => {
    vi.mocked(api.inspectKnowledgeSource).mockResolvedValueOnce({
      ...DETAIL,
      kind: "recording",
      config: { provider: "zoom", ids: ["123456789"] },
      items: [],
      include: [],
    });
    render(<SourceDetailsDialog org="acme" namespace="code-x" name="design-docs" onClose={() => {}} />);
    expect(await screen.findByText("123456789")).toBeInTheDocument();
    expect(screen.getByText("zoom")).toBeInTheDocument();
  });
});
