// A Library row's More menu's download fetches the document under the viewer's scope with the
// original filename (or the Markdown name for a text-only document), and a failed call —
// download or delete — raises an error toast that carries the request id.
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { ApiError } from "../lib/errors";
import { ToastViewport } from "../components/Toast";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  downloadKnowledge: vi.fn(),
  deleteKnowledge: vi.fn(),
  getKnowledge: vi.fn(),
  fetchKnowledgePreview: vi.fn(),
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

/** Pick an item from a row's More menu. */
async function pick(title: string, item: string) {
  await userEvent.click(screen.getByRole("button", { name: `Actions for ${title}` }));
  await userEvent.click(await screen.findByRole("menuitem", { name: item }));
}

const failure = () => new ApiError({ status: 500, code: "INTERNAL", message: "boom", requestId: "rid-123" });

describe("Library download", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: ROWS, total: ROWS.length, next_cursor: null });
    useStore.setState({
      identity: { org: ORG, namespace: NS, agent: "loom-ui" },
      view: "knowledge",
      whoami: { authenticated: false },
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null, whoami: null });
  });

  it("downloads under the viewer's scope with the original filename, else the Markdown name", async () => {
    api.downloadKnowledge.mockResolvedValue(undefined);
    render(<Documents />);
    await settle();

    await pick("Handbook", "Download PDF");
    expect(api.downloadKnowledge).toHaveBeenLastCalledWith(
      { org: ORG, namespace: NS },
      "docs/handbook.pdf",
      "handbook.pdf",
    );

    await pick("Text only", "Download text (.md)");
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
    await pick("Handbook", "Download PDF");
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
    await pick("Handbook", "Delete");
    await settle();
    expect(api.deleteKnowledge).toHaveBeenCalledWith({ org: ORG, namespace: NS }, "docs/handbook.pdf");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Delete failed");
    expect(alert.textContent).toContain("request id rid-123");
  });

  // The deleted row took its menu and the focus with it: focus lands on the search field.
  it("hands focus to the search field after a row's delete", async () => {
    api.deleteKnowledge.mockImplementation(async () => {
      const rest = ROWS.filter((r) => r.source_uri !== "docs/handbook.pdf");
      api.listKnowledge.mockResolvedValue({ rows: rest, total: rest.length, next_cursor: null });
      return {};
    });
    render(<Documents />);
    await settle();
    await pick("Handbook", "Delete");
    await waitFor(() => expect(screen.queryByText("Handbook")).toBeNull());
    expect(screen.getByLabelText("Search library")).toHaveFocus();
  });
});

describe("Library preview", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: ROWS, total: ROWS.length, next_cursor: null });
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "docs/handbook.pdf" });
    useStore.setState({
      identity: { org: ORG, namespace: NS, agent: "loom-ui" },
      view: "knowledge",
      whoami: { authenticated: false },
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null, whoami: null });
  });

  async function open(preview: unknown) {
    api.fetchKnowledgePreview.mockResolvedValue(preview);
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Preview Handbook" }));
    await settle();
    return screen.getByRole("dialog");
  }

  it("shows text as a text node, never parsed or framed", async () => {
    const d = await open({ kind: "text", url: null, text: "<b>bold</b>", contentType: "text/plain", size: 11 });
    expect(d.querySelector("iframe")).toBeNull();
    const pre = d.querySelector("pre");
    expect(pre?.textContent).toBe("<b>bold</b>");
    expect(pre?.querySelector("b")).toBeNull();
  });

  it("says when the text shown is only the start of the file", async () => {
    const d = await open({ kind: "text", url: null, text: "start", contentType: "text/plain", size: 3 << 20 });
    expect(d).toHaveTextContent("The first 1.0 MB; Download has the whole file.");
    cleanup();
    const whole = await open({ kind: "text", url: null, text: "all", contentType: "text/plain", size: 3 });
    expect(whole).not.toHaveTextContent("Download has the whole file");
  });

  it("shows an image as <img>, never in a frame", async () => {
    const d = await open({ kind: "image", url: "blob:i", contentType: "image/png", size: 4 });
    expect(d.querySelector("iframe")).toBeNull();
    expect(d.querySelector("img")?.getAttribute("src")).toBe("blob:i");
  });

  it("frames nothing for a type that only downloads", async () => {
    const d = await open({ kind: "download", url: null, contentType: "text/html", size: 4 });
    expect(d.querySelector("iframe")).toBeNull();
    expect(d.querySelector("img")).toBeNull();
    expect(d.textContent).toContain("No preview for this file type");
  });
});
