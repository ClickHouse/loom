// Documents dragged from the Library's list onto a place in the tree beside it: the row, or
// the selection it is in, lands only where Move to… would take it, and a drop opens Move to…
// set to that place.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeMoveResult } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { TREE_PAGE_MIN } from "../lib/useTreeHeight";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listCollections: vi.fn(),
  moveKnowledge: vi.fn(),
  searchKnowledge: vi.fn(),
  listMine: vi.fn(),
  listGrants: vi.fn(async () => ({ rows: [] })),
  getKnowledge: vi.fn(async () => ({ found: true, content: "text" })),
  fetchKnowledgePreview: vi.fn(async () => null),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const toasts = vi.hoisted(() => ({ toast: vi.fn(), toastError: vi.fn(), toastWithId: vi.fn() }));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  ...toasts,
}));

const COLLECTIONS = [
  { namespace: "col-me", parent: "", personal: true, role: "admin", description: "", title: "" },
  { namespace: "col-eng", parent: "", personal: false, role: "writer", description: "", title: "Engineering" },
  { namespace: "col-legal", parent: "", personal: false, role: "viewer", description: "", title: "Legal" },
];

const doc = (namespace: string, source_uri: string, title: string) => ({
  namespace,
  source_uri,
  title,
  description: "",
  tags: [],
  ingested_at: "2026-09-28T12:00:00Z",
  chunk_count: 2,
  superseded: false,
  has_blob: false,
});

const LISTED = [
  doc("team", "notes/plan.md", "Team plan"),
  doc("col-eng", "runbooks/drain.md", "Drain a lane"),
  doc("col-eng", "runbooks/fill.md", "Fill a lane"),
  doc("col-legal", "msa.md", "Services agreement"),
];
// What the server holds: a move changes it.
let held = LISTED;

type ListOpts = { namespaces?: string[] };

/** <main> as App draws it, wide enough for the tree beside the list unless `width` says less. */
function main(width = TREE_PAGE_MIN): HTMLElement {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const rect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.id === "main" ? ({ width, height: 800 } as DOMRect) : rect.call(this);
  });
  const el = Object.assign(document.createElement("main"), { id: "main" });
  document.body.append(el);
  return el;
}

const dataTransfer = () => ({ setData: vi.fn(), clearData: vi.fn(), effectAllowed: "", dropEffect: "" });
const docRow = (title: string) => screen.getByText(title).closest("[data-doc-row]") as HTMLElement;
const tree = () => screen.getByRole("navigation", { name: "Library places" });
const place = (name: string) => within(tree()).getByRole("link", { name }).closest(".library-tree-row") as HTMLElement;
/** Each place in the tree that takes what is held (a dialog open over it too). */
const takers = () => [...document.querySelectorAll(".library-tree-row[data-drop]")].map((r) => r.textContent);

/** Pick up `title`'s row and wait a tick for the tree to answer. */
async function pickUp(title: string) {
  const dt = dataTransfer();
  fireEvent.dragStart(docRow(title), { dataTransfer: dt });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  return dt;
}

async function dropOn(name: string, dt = dataTransfer()) {
  fireEvent.dragOver(place(name), { dataTransfer: dt });
  fireEvent.drop(place(name), { dataTransfer: dt });
}

describe("dragging documents onto the Library's tree", () => {
  beforeEach(() => {
    resetNamespaceRoleCache();
    held = LISTED;
    api.listCollections.mockResolvedValue({ collections: COLLECTIONS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      return { rows: held.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "w", org: "acme", roles: ["WRITER"], namespaces: ["team"] },
      myNamespaces: [{ namespace: "team", role: "writer", status: "active", kind: "memory" } as never],
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "knowledge",
      pendingDocSourceUri: null,
    });
    history.replaceState(null, "", "/library?place=all");
  });
  afterEach(() => {
    cleanup();
    document.getElementById("main")?.remove();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useStore.setState({ identity: null, whoami: null, myNamespaces: null });
    history.replaceState(null, "", "/");
  });

  async function open() {
    render(<Documents />, { container: main() });
    await screen.findByText("Services agreement");
    await within(tree()).findByRole("link", { name: "Engineering" });
  }

  it("marks the places Move to… lists for a row, opens Move to… set to the one dropped on, and moves it", async () => {
    api.moveKnowledge.mockImplementation(
      async (_id: unknown, _to: string, uris: string[], _s: unknown, onCall?: (r: KnowledgeMoveResult[]) => void) => {
        const results = uris.map((source_uri) => ({ source_uri, moved: true, reason: "" }) as KnowledgeMoveResult);
        held = held.map((r) => (uris.includes(r.source_uri) ? { ...r, namespace: "col-me" } : r));
        onCall?.(results);
        return { found: true, results };
      },
    );
    await open();
    expect(docRow("Drain a lane")).toHaveAttribute("draggable", "true");
    const dt = await pickUp("Drain a lane");
    // Not its own place, nor one the person only reads, nor All documents.
    expect(takers()).toEqual(["Namespace team", "My files"]);
    await dropOn("My files", dt);
    const dialog = await screen.findByRole("dialog", { name: "Move 1 document" });
    expect(within(dialog).getByText("From Engineering")).toBeInTheDocument();
    expect(within(dialog).getByText("Only you read them there.")).toBeInTheDocument();
    expect(takers()).toEqual([]);
    const reads = api.listKnowledge.mock.calls.length;
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() =>
      expect(api.moveKnowledge).toHaveBeenCalledWith(
        { org: "acme", namespace: "col-eng" },
        "col-me",
        ["runbooks/drain.md"],
        expect.anything(),
        expect.any(Function),
      ),
    );
    expect(toasts.toast).toHaveBeenCalledWith("Moved 1 document to My files", "ok");
    // Read again in place: the rows stay while the answer comes.
    await waitFor(() => expect(api.listKnowledge.mock.calls.length).toBeGreaterThan(reads));
    expect(screen.getByText("Team plan")).toBeInTheDocument();
  });

  it("drops nothing on a place that does not take the row, and no row a viewer reads drags", async () => {
    await open();
    expect(docRow("Services agreement")).not.toHaveAttribute("draggable");
    const dt = await pickUp("Drain a lane");
    for (const name of ["Engineering", "Legal", "All documents"]) {
      // Not cancelled: the browser shows no drop there.
      expect(fireEvent.dragOver(place(name), { dataTransfer: dt })).toBe(true);
      fireEvent.drop(place(name), { dataTransfer: dt });
    }
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.dragEnd(docRow("Drain a lane"), { dataTransfer: dt });
    expect(takers()).toEqual([]);
  });

  it("drags the selection when the row is in it, from one place only, and the row alone otherwise", async () => {
    await open();
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Drain a lane" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Fill a lane" }));
    await dropOn("Namespace team", await pickUp("Fill a lane"));
    const dialog = await screen.findByRole("dialog", { name: "Move 2 documents" });
    expect(within(dialog).getByText("From Engineering")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // A row outside the selection goes alone.
    await dropOn("My files", await pickUp("Team plan"));
    expect(await screen.findByRole("dialog", { name: "Move 1 document" })).toHaveTextContent("From Namespace team");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // A selection across two places goes nowhere, as Move to… takes one place at a time.
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Team plan" }));
    const dt = await pickUp("Team plan");
    expect(takers()).toEqual([]);
    await dropOn("My files", dt);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps Move to… open saying what to do when the move fails", async () => {
    api.moveKnowledge.mockImplementation(
      async (_id: unknown, _to: string, _u: string[], _s: unknown, onCall?: (r: KnowledgeMoveResult[]) => void) => {
        onCall?.([]);
        return { found: true, results: [], error: new Error("network down") };
      },
    );
    await open();
    await dropOn("My files", await pickUp("Drain a lane"));
    const dialog = await screen.findByRole("dialog", { name: "Move 1 document" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/the list shows where each one is now/);
    expect(toasts.toast).not.toHaveBeenCalled();
  });

  it("drags no row without the tree beside the list", async () => {
    render(<Documents />, { container: main(TREE_PAGE_MIN - 1) });
    await screen.findByText("Services agreement");
    expect(screen.queryByRole("navigation", { name: "Library places" })).toBeNull();
    expect(docRow("Drain a lane")).not.toHaveAttribute("draggable");
  });
});
