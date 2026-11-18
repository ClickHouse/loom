import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { DRAG_TYPE } from "../lib/useDragHold";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listCollections: vi.fn(),
  listGrants: vi.fn(),
  knowledgeFormats: vi.fn(),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
  addKnowledge: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));

const collection = {
  namespace: "col-runbooks",
  parent: "",
  personal: false,
  role: "writer",
  title: "Runbooks",
  description: "",
  document_count: 0,
};

beforeEach(() => {
  resetNamespaceRoleCache();
  invalidateSharedCollections();
  api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
  api.listCollections.mockResolvedValue({ collections: [collection], next_cursor: null });
  api.listGrants.mockResolvedValue({ grants: [] });
  api.knowledgeFormats.mockResolvedValue({
    formats: [{ format: "text", extensions: ["txt"], media_types: ["text/plain"], available: true }],
    max_upload_bytes: 1_000_000,
    refused_extensions: [],
    default_format: "text",
  });
  api.uploadKnowledgeFile.mockResolvedValue({
    upload_id: "up-1",
    sha256: "",
    size: 14,
    filename: "notes.txt",
    format: "text",
    expires_at: "",
  });
  api.ingestKnowledge.mockResolvedValue({ ingestion_id: "job-1", state: "pending", source_uri: "notes.txt" });
  api.knowledgeIngestStatus.mockResolvedValue({
    found: true,
    ingestion_id: "job-1",
    source_uri: "notes.txt",
    state: "ready",
    stage: "",
    error_code: "",
    error_message: "",
    format: "text",
    coverage: null,
    warnings: [],
    chunks: 1,
    revision: 1,
  });
  useStore.setState({
    authed: true,
    whoami: { authenticated: true, subject: "w", org: "acme", roles: ["WRITER"], namespaces: ["team"] },
    myNamespaces: [{ namespace: "team", role: "writer", status: "active", kind: "memory" } as never],
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    view: "knowledge",
    pendingDocSourceUri: null,
  });
  history.replaceState(null, "", "/library?collection=col-runbooks");
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useStore.setState({ identity: null, whoami: null, myNamespaces: null });
  history.replaceState(null, "", "/");
});

const folder = (file: File) => ({
  isFile: false,
  isDirectory: true,
  name: "Incidents",
  createReader: () => {
    const batches = [
      [{ isFile: true, isDirectory: false, name: file.name, file: (ok: (f: File) => void) => ok(file) }],
      [],
    ];
    return { readEntries: (ok: (entries: unknown[]) => void) => ok(batches.shift() ?? []) };
  },
});

describe("dropping into an empty collection", () => {
  it.each(["file", "folder"])(
    "opens the existing Add flow for a dropped %s, captured while the drop lasts",
    async (kind) => {
      render(
        <StrictMode>
          <Documents />
        </StrictMode>,
      );
      const hint = await screen.findByText("Drop files or folders here");
      expect(screen.getAllByRole("button", { name: "Add document" })).toHaveLength(1);
      const file = new File(["incident notes"], "notes.txt", { type: "text/plain" });
      let dropping = true;
      const item = {
        kind: "file",
        webkitGetAsEntry: () => {
          expect(dropping).toBe(true);
          return kind === "folder" ? folder(file) : null;
        },
        getAsFile: () => {
          expect(dropping).toBe(true);
          return file;
        },
      };
      fireEvent.drop(hint.closest("[data-testid=library-empty-drop]")!, {
        dataTransfer: { types: ["Files"], items: [item], files: [] },
      });
      dropping = false;
      if (kind === "folder") {
        expect(await screen.findByText("Creates 1 collection in Runbooks:")).toBeInTheDocument();
        expect(screen.getByText("Incidents · 1 file")).toBeInTheDocument();
        expect(screen.getByRole("dialog", { name: "Add documents to Runbooks" })).toBeInTheDocument();
      } else {
        expect(await screen.findByDisplayValue("notes.txt")).toBeInTheDocument();
        expect(screen.getByRole("dialog", { name: "Add document" })).toBeInTheDocument();
      }
      expect(api.uploadKnowledgeFile).not.toHaveBeenCalled();
      expect(api.addKnowledge).not.toHaveBeenCalled();
      if (kind === "file") {
        await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Upload" }));
        await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
        expect(api.uploadKnowledgeFile.mock.calls[0][0]).toEqual({ org: "acme", namespace: "col-runbooks" });
        expect(api.uploadKnowledgeFile.mock.calls[0][1]).toBe(file);
        expect(api.ingestKnowledge.mock.calls[0][1]).toMatchObject({ upload_id: "up-1", source_uri: "notes.txt" });
        await userEvent.click(screen.getByRole("button", { name: "Add document" }));
        expect(screen.queryByDisplayValue("notes.txt")).not.toBeInTheDocument();
      }
    },
  );

  it("offers no drop target to someone who can only read the collection", async () => {
    api.listCollections.mockResolvedValue({ collections: [{ ...collection, role: "viewer" }], next_cursor: null });
    render(<Documents />);
    await screen.findByText("This collection is empty");
    expect(screen.queryByText("Drop files or folders here")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add document" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("leaves Loom document and collection drags to the existing move targets", async () => {
    render(<Documents />);
    const hint = await screen.findByText("Drop files or folders here");
    fireEvent.drop(hint.closest("[data-testid=library-empty-drop]")!, {
      dataTransfer: { types: [DRAG_TYPE], items: [], files: [] },
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
