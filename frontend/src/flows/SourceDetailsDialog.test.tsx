import { cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourceDetailsDialog } from "./SourceDetailsDialog";

vi.mock("../api/knowledgeSources", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/knowledgeSources")>()),
  inspectKnowledgeSource: vi.fn(async () => ({
    name: "design-docs",
    kind: "folder",
    config: { folder_id: "abc" },
    created_by: "alice",
    created_at: "",
    sync: { status: "failed", last_synced_at: "", documents_seen: 0, error: "http_403" },
    documents: [{ source_uri: "a.md", title: "Runbook", source_revision: "1", ingested_at: "2000-01-01 00:00:00" }],
  })),
}));

afterEach(() => cleanup());

describe("SourceDetailsDialog", () => {
  it("shows the source's settings, its last sync and the documents it wrote", async () => {
    render(<SourceDetailsDialog org="acme" namespace="code-x" name="design-docs" onClose={() => {}} />);
    expect(await screen.findByText("abc")).toBeInTheDocument();
    expect(screen.getByText("http_403")).toBeInTheDocument();
    expect(screen.getByText("Runbook")).toBeInTheDocument();
    expect(screen.getByText(/never/)).toBeInTheDocument();
  });
});
