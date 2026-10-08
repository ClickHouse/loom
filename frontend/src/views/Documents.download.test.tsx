// A Library row's download button fetches the document under the viewer's scope with the
// original filename (or the Markdown name for a text-only document), and a failed call —
// download or delete — raises an error toast that carries the request id.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { ApiError } from "../lib/errors";
import { ToastViewport } from "../components/Toast";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  downloadKnowledge: vi.fn(),
  deleteKnowledge: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

const ORG = "acme";
const NS = "team";
const ROWS = [
  {
    source_uri: "docs/handbook.pdf",
    title: "Handbook",
    original_filename: "handbook.pdf",
    description: "",
    tags: [],
    has_blob: true,
  },
  {
    source_uri: "notes/text-only",
    title: "Text only",
    original_filename: "",
    description: "",
    tags: [],
    has_blob: false,
  },
];

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

function hoverRow(title: string) {
  const row = screen.getByText(title).closest("[class*='rounded-xl']");
  if (!row) throw new Error(`no row for ${title}`);
  fireEvent.mouseEnter(row);
}

const failure = () => new ApiError({ status: 500, code: "INTERNAL", message: "boom", requestId: "rid-123" });

describe("Library download", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: ROWS, total: ROWS.length, next_cursor: null });
    useStore.setState({ identity: { org: ORG, namespace: NS, agent: "loom-ui" }, view: "knowledge" });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null });
  });

  it("downloads under the viewer's scope with the original filename, else the Markdown name", async () => {
    api.downloadKnowledge.mockResolvedValue(undefined);
    render(<Documents />);
    await settle();

    hoverRow("Handbook");
    fireEvent.click(screen.getByRole("button", { name: "Download original file" }));
    expect(api.downloadKnowledge).toHaveBeenLastCalledWith(
      { org: ORG, namespace: NS },
      "docs/handbook.pdf",
      "handbook.pdf",
    );

    hoverRow("Text only");
    fireEvent.click(screen.getByRole("button", { name: "Download text (.md)" }));
    expect(api.downloadKnowledge).toHaveBeenLastCalledWith(
      { org: ORG, namespace: NS },
      "notes/text-only",
      "text-only.md",
    );
  });

  it("a failed download toasts with the request id", async () => {
    api.downloadKnowledge.mockRejectedValue(failure());
    render(
      <>
        <Documents />
        <ToastViewport />
      </>,
    );
    await settle();
    hoverRow("Handbook");
    fireEvent.click(screen.getByRole("button", { name: "Download original file" }));
    await settle();
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Download failed");
    expect(alert.textContent).toContain("request id rid-123");
  });

  it("a failed delete toasts with the request id", async () => {
    api.deleteKnowledge.mockRejectedValue(failure());
    render(
      <>
        <Documents />
        <ToastViewport />
      </>,
    );
    await settle();
    hoverRow("Handbook");
    fireEvent.click(screen.getByRole("button", { name: "Delete document" }));
    await settle();
    expect(api.deleteKnowledge).toHaveBeenCalledWith({ org: ORG, namespace: NS }, "docs/handbook.pdf");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Delete failed");
    expect(alert.textContent).toContain("request id rid-123");
  });
});
