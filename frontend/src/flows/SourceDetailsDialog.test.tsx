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
    expect(await screen.findByText(/Incomplete/)).toBeInTheDocument();
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
    const badge = await screen.findByText(/· Synced/);
    expect(badge.textContent).toContain("2000");
  });
});
