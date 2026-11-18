// All documents: one list of every place the person reads, each row saying where it lives
// and taking its writes from its own place; what a read of several places left out (a
// place refused or failed, a server that reads one place per call); and a search scoped to
// the place shown or everywhere.
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { ApiError } from "../lib/errors";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listCollections: vi.fn(),
  bulkDeleteKnowledge: vi.fn(),
  searchKnowledge: vi.fn(),
  listMine: vi.fn(),
  listGrants: vi.fn(),
  moveKnowledge: vi.fn(),
  getKnowledge: vi.fn(async () => ({ found: true, content: "text" })),
  fetchKnowledgePreview: vi.fn(async () => null),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const collectionPlaces = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock("../lib/collectionPlaces", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/collectionPlaces")>();
  return {
    ...real,
    invalidateSharedCollections: (...a: Parameters<typeof real.invalidateSharedCollections>) => {
      collectionPlaces.invalidate(...a);
      real.invalidateSharedCollections(...a);
    },
  };
});
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
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

const EVERY = [
  doc("team", "notes/plan.md", "Team plan"),
  doc("col-eng", "runbooks/drain.md", "Drain a lane"),
  doc("col-legal", "msa.md", "Services agreement"),
];

const REFUSED = new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "not a reader of col-legal" });

type ListOpts = { namespaces?: string[]; limit?: number; cursor?: string };

/** The places a call to the list read: its namespace and the others it named. */
const placesOf = (call: unknown[]) => [
  (call[0] as { namespace: string }).namespace,
  ...((call[1] as ListOpts).namespaces ?? []),
];

async function settle() {
  for (let i = 0; i < 4; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

function at(path: string) {
  history.replaceState(null, "", path);
}

describe("All documents", () => {
  beforeEach(() => {
    resetNamespaceRoleCache();
    api.listCollections.mockResolvedValue({ collections: COLLECTIONS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    api.bulkDeleteKnowledge.mockImplementation(async (_id: unknown, uris: string[]) => ({
      results: [],
      total_tombstoned: uris.length,
    }));
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "w", org: "acme", roles: ["WRITER"], namespaces: ["team"] },
      myNamespaces: [{ namespace: "team", role: "writer", status: "active", kind: "memory" } as never],
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "knowledge",
      pendingDocSourceUri: null,
    });
    at("/library?place=all");
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null, whoami: null, myNamespaces: null });
    at("/");
  });

  it("reads every place in one call and says on each row where it lives, linked to that place", async () => {
    const { container } = render(<Documents />);
    await screen.findByText("Services agreement");
    const read = api.listKnowledge.mock.calls.find((c) => (c[1] as ListOpts).namespaces?.length);
    expect(read && placesOf(read)).toEqual(["team", "col-me", "col-eng", "col-legal"]);
    const row = screen.getByText("Drain a lane").closest("[data-doc-row]") as HTMLElement;
    const link = within(row).getByRole("link", { name: "Engineering" });
    expect(link).toHaveAttribute("href", expect.stringContaining("collection=col-eng"));
    const own = screen.getByText("Team plan").closest("[data-doc-row]") as HTMLElement;
    expect(within(own).getByRole("link", { name: "Namespace team" })).toBeInTheDocument();
    expect(screen.getByText(/3 documents · 6 chunks · 4 places/)).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("gives each row its own place's writes: a viewer's row has no checkbox and its Delete says why", async () => {
    render(<Documents />);
    await screen.findByText("Services agreement");
    expect(screen.getByRole("checkbox", { name: "Select Drain a lane" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Select Services agreement" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Actions for Services agreement" }));
    const menu = await screen.findByRole("menu", { name: "Actions for Services agreement" });
    expect(within(menu).getByRole("menuitem", { name: "Delete" })).toHaveAttribute("aria-disabled", "true");
    expect(within(menu).getByRole("menuitem", { name: "Add to collection…" })).toHaveAttribute("aria-disabled", "true");
    expect(menu).toHaveTextContent("Legal");
  });

  it("deletes a selection in one call per place, and moves only from one place at a time", async () => {
    render(<Documents />);
    await screen.findByText("Services agreement");
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Team plan" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Drain a lane" }));
    const bar = screen.getByLabelText("Search and filters");
    const move = within(bar).getByRole("button", { name: "Move to…" });
    expect(move).toHaveAttribute("aria-disabled", "true");
    expect(move).toHaveAccessibleDescription("Move to… takes documents from one place at a time.");
    const add = within(bar).getByRole("button", { name: "Add to collection…" });
    expect(add).toHaveAttribute("aria-disabled", "true");
    expect(add).toHaveAccessibleDescription("Add to collection… takes documents from one place at a time.");
    await userEvent.click(within(bar).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.bulkDeleteKnowledge).toHaveBeenCalledTimes(2));
    expect(api.bulkDeleteKnowledge).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, ["notes/plan.md"]);
    expect(api.bulkDeleteKnowledge).toHaveBeenCalledWith({ org: "acme", namespace: "col-eng" }, ["runbooks/drain.md"]);
  });

  it("moves a selection from the place its rows are in, and a row from its own place", async () => {
    const more = [...EVERY, doc("col-eng", "runbooks/fill.md", "Fill a lane")];
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      return { rows: more.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Fill a lane");
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Drain a lane" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Fill a lane" }));
    const bar = screen.getByLabelText("Search and filters");
    await userEvent.click(within(bar).getByRole("button", { name: "Move to…" }));
    const dialog = await screen.findByRole("dialog", { name: "Move 2 documents" });
    expect(within(dialog).getByText("From Engineering")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: /To/ }));
    const mine = await screen.findByText("My files", { selector: ".collection-depth" });
    const list = mine.closest("[role=dialog]") as HTMLElement;
    const offered = [...list.querySelectorAll(".collection-depth")].map((o) => o.textContent);
    expect(offered).toContain("My files");
    expect(offered).not.toContain("Engineering");
    await userEvent.keyboard("{Escape}");
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Actions for Team plan" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Move to…" }));
    const own = await screen.findByRole("dialog", { name: "Move 1 document" });
    expect(within(own).getByText("From Namespace team")).toBeInTheDocument();
  });

  it("offers writable collections for a row and leaves its source intact when Add to collection is cancelled", async () => {
    render(<Documents />);
    await screen.findByText("Drain a lane");
    await userEvent.click(screen.getByRole("button", { name: "Actions for Drain a lane" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Add to collection…" }));
    const dialog = await screen.findByRole("dialog", { name: "Add 1 document to a collection" });
    expect(within(dialog).getByText("From Engineering")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: /To/ }));
    const mine = await screen.findByText("My files", { selector: ".collection-depth" });
    const offered = [...mine.closest("[role=dialog]")!.querySelectorAll(".collection-depth")].map((o) => o.textContent);
    expect(offered).toEqual(["My files"]);
    await userEvent.click(mine);
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.moveKnowledge).not.toHaveBeenCalled();
    expect(screen.getByText("Drain a lane")).toBeInTheDocument();
    expect(
      within(screen.getByText("Drain a lane").closest("[data-doc-row]") as HTMLElement).getByRole("link", {
        name: "Engineering",
      }),
    ).toBeInTheDocument();
  });

  it("adds a bulk selection from its source collection in All documents, and Move still offers the header namespace", async () => {
    const more = [...EVERY, doc("col-eng", "runbooks/fill.md", "Fill a lane")];
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => ({
      rows: more.filter((r) => [id.namespace, ...(opts.namespaces ?? [])].includes(r.namespace)),
      next_cursor: null,
    }));
    api.moveKnowledge.mockImplementation(async (_id, _to, uris, _signal, onCall) => {
      const results = uris.map((source_uri: string) => ({ source_uri, moved: true, reason: "" }));
      onCall(results);
      return { found: true, results };
    });
    render(<Documents />);
    await screen.findByText("Fill a lane");
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Drain a lane" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Fill a lane" }));
    await userEvent.click(
      within(screen.getByLabelText("Search and filters")).getByRole("button", { name: "Add to collection…" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Add 2 documents to a collection" });
    expect(
      within(dialog).getByText("Adding moves 2 documents out of Engineering. Each document lives in one place."),
    ).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: /To/ }));
    await userEvent.click(await screen.findByText("My files", { selector: ".collection-depth" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() =>
      expect(api.moveKnowledge).toHaveBeenCalledWith(
        { org: "acme", namespace: "col-eng" },
        "col-me",
        ["runbooks/drain.md", "runbooks/fill.md"],
        expect.any(AbortSignal),
        expect.any(Function),
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Actions for Drain a lane" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Move to…" }));
    const move = await screen.findByRole("dialog", { name: "Move 1 document" });
    await userEvent.click(within(move).getByRole("button", { name: /To/ }));
    expect(await screen.findByText("Namespace team", { selector: ".collection-depth" })).toBeInTheDocument();
  });

  it("says a server that reads one place per call shows only the first, never all of them", async () => {
    api.listKnowledge.mockResolvedValue({
      rows: [{ ...EVERY[0], namespace: undefined }],
      next_cursor: null,
      ignored_args: ["namespaces"],
    });
    render(<Documents />);
    await screen.findByText("Team plan");
    expect(
      screen.getByText(
        "Only Namespace team is shown: this server lists one place per read, so 3 other places are not.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 of 4 places/)).toBeInTheDocument();
    // The row is the first place's, and says so.
    const row = screen.getByText("Team plan").closest("[data-doc-row]") as HTMLElement;
    expect(within(row).getByRole("link", { name: "Namespace team" })).toBeInTheDocument();
  });

  it("names a place it cannot read and keeps the others", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    expect(screen.getByText("Not shown, as you can't read it: Legal.")).toBeInTheDocument();
    expect(screen.queryByText("Services agreement")).toBeNull();
    expect(screen.getByText(/3 of 4 places/)).toBeInTheDocument();
  });

  it("names a place whose own read failed after a refusal, keeps the others, and reads again", async () => {
    let down = true;
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      if (down && places.length === 1 && places[0] === "col-eng") {
        throw new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "shard down" });
      }
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Team plan");
    expect(screen.getByText("Not shown, as you can't read it: Legal.")).toBeInTheDocument();
    expect(screen.getByText(/^Couldn't read Engineering; the other places are shown: /)).toBeInTheDocument();
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Read again" }));
    expect(await screen.findByText("Drain a lane")).toBeInTheDocument();
    expect(screen.queryByText(/^Couldn't read Engineering/)).toBeNull();
  });

  it("shows a busy server's failure as the list's error, with no read per place", async () => {
    api.listKnowledge.mockRejectedValue(new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "busy" }));
    render(<Documents />);
    expect(await screen.findByText("Couldn't load documents")).toBeInTheDocument();
    expect(api.listKnowledge).toHaveBeenCalledTimes(1);
  });

  it("asks Load more for the places the first page read, after a refusal", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      if (opts.cursor) return { rows: [doc("col-me", "me/later.md", "A later note")], next_cursor: null };
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: opts.limit === 1 ? null : "c-2" };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    // The refusal reads the collections again, and their answer the list: then Load more.
    await waitFor(() => expect(api.listCollections.mock.calls.length).toBeGreaterThanOrEqual(2));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("A later note")).toBeInTheDocument();
    const more = api.listKnowledge.mock.calls.find((c) => (c[1] as ListOpts).cursor === "c-2") as unknown[];
    expect(placesOf(more)).toEqual(["team", "col-me", "col-eng"]);
  });

  it("reads the first page again when a replica refuses the cursor", async () => {
    let pages = 0;
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: ListOpts) => {
      if (opts.cursor)
        throw new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "cursor is not one this op issued" });
      pages += 1;
      return { rows: EVERY, next_cursor: "c-2" };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(pages).toBe(2));
    expect(screen.getByText("Drain a lane")).toBeInTheDocument();
    expect(toasts.toastWithId).toHaveBeenCalledWith(
      "The list was read again from the top: the server that answered could not go on from there",
      "info",
      expect.any(ApiError),
    );
    expect(toasts.toastError).not.toHaveBeenCalled();
  });

  it("reads the first page again when the cursor of a list read one place at a time is refused", async () => {
    let pages = 0;
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: ListOpts) => {
      if (opts.cursor) throw new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "bad cursor" });
      pages += 1;
      return { rows: [EVERY[0]], next_cursor: "c-2", ignored_args: ["namespaces"] };
    });
    render(<Documents />);
    await screen.findByText(/^Only Namespace team is shown/);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(pages).toBe(2));
    expect(toasts.toastWithId).toHaveBeenCalledWith(
      "The list was read again from the top: the server that answered could not go on from there",
      "info",
      expect.any(ApiError),
    );
    expect(toasts.toastError).not.toHaveBeenCalled();
  });

  it("says Add waits while the collections are read, with no header namespace to add to", async () => {
    api.listCollections.mockImplementation(() => new Promise(() => {}));
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" }, myNamespaces: [] });
    render(<Documents />);
    const add = await screen.findByRole("button", { name: "Add document" });
    expect(add).toHaveAttribute("aria-disabled", "true");
    expect(add).toHaveAccessibleDescription("My files is not known until the collections are loaded.");
  });

  it("searches passages in every place the list read, each hit naming its place", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    api.searchKnowledge.mockResolvedValue([
      {
        source_uri: "runbooks/drain.md",
        chunk_idx: 0,
        content: "Drain it.",
        section: "",
        score: 1,
        namespace: "col-eng",
      },
    ]);
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    expect(
      screen.getByText(/Searches every document in 3 places, whatever the tag filter; 1 other place is not searched/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "drain" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    const list = await screen.findByRole("list", { name: "Passages found" });
    // The refused place is left out of the search, which would refuse it whole.
    const [id, , , opts] = api.searchKnowledge.mock.calls[0] as [{ namespace: string }, string, number, ListOpts];
    expect([id.namespace, ...(opts.namespaces ?? [])]).toEqual(["team", "col-me", "col-eng"]);
    expect(list).toHaveTextContent("in Engineering");
  });

  it("shows the error and Retry when no place can be read", async () => {
    api.listKnowledge.mockRejectedValue(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Documents />);
    expect(await screen.findByText("Couldn't load documents")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByText(/No documents/)).toBeNull();
  });

  it("leaves a refused place out of later reads under one collections answer, reading the list once", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    const probes = () => api.listKnowledge.mock.calls.filter((c) => (c[1] as ListOpts).limit === 1).length;
    // The refusal reads the collections again, once, and their new answer reads every place
    // again (a grant may have caught up): one pass of reads per place per answer.
    await waitFor(() => expect(probes()).toBe(8));
    await settle();
    expect(collectionPlaces.invalidate).toHaveBeenCalledTimes(1);
    expect(api.listCollections.mock.calls.length).toBeGreaterThanOrEqual(2);
    // Then left out while that answer stands: one read, of the places it can read.
    const before = api.listKnowledge.mock.calls.length;
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "tag:two" } });
    await waitFor(() => expect(api.listKnowledge.mock.calls.length).toBe(before + 1));
    await settle();
    const last = api.listKnowledge.mock.calls.at(-1) as unknown[];
    expect(placesOf(last)).toEqual(["team", "col-me", "col-eng"]);
    expect(probes()).toBe(8);
    expect(collectionPlaces.invalidate).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Not shown, as you can't read it: Legal.")).toBeInTheDocument();
  });

  it("asks a refused place again when the collections answered while the refused read was in flight", async () => {
    let release = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    let held = false;
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (!held && places.length > 1) {
        held = true;
        await gate;
      }
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await waitFor(() => expect(held).toBe(true));
    // A new collections answer lands before the read's refusal does.
    const lists = api.listCollections.mock.calls.length;
    act(() => invalidateSharedCollections());
    await waitFor(() => expect(api.listCollections.mock.calls.length).toBeGreaterThan(lists));
    await settle();
    release();
    await screen.findByText("Drain a lane");
    await settle();
    // Found under the answer the read started with: not kept, and the next read asks again.
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "tag:one" } });
    await waitFor(() =>
      expect(
        api.listKnowledge.mock.calls.some(
          (c) => (c[1] as ListOpts & { tags?: string[] }).tags?.[0] === "one" && placesOf(c).includes("col-legal"),
        ),
      ).toBe(true),
    );
  });

  it("reads a refused place again as soon as the collections answer again, as a lagging grant catches up", async () => {
    // The grant reaches the reading replica by the time the collections answer again.
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (api.listCollections.mock.calls.length < 2 && places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    // No other read is asked for: the collections' new answer reads the list again.
    expect(await screen.findByText("Services agreement")).toBeInTheDocument();
    expect(screen.queryByText(/Not shown/)).toBeNull();
  });

  it("deletes the rows of the places that took the delete, and keeps a refused place's selected", async () => {
    api.bulkDeleteKnowledge.mockImplementation(async (id: { namespace: string }, uris: string[]) => {
      if (id.namespace === "col-eng") throw REFUSED;
      return { results: [], total_tombstoned: uris.length };
    });
    render(<Documents />);
    await screen.findByText("Services agreement");
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Team plan" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Drain a lane" }));
    await userEvent.click(within(screen.getByLabelText("Search and filters")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(toasts.toast).toHaveBeenCalledWith("Deleted 1 document", "ok"));
    expect(toasts.toastError).toHaveBeenCalledWith("Bulk delete failed in Engineering", REFUSED);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Select Team plan" })).not.toBeChecked());
    expect(screen.getByRole("checkbox", { name: "Select Drain a lane" })).toBeChecked();
  });

  it("counts the places a one-place-per-read server did not read without those skipped as refused", async () => {
    let older = false;
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (older)
        return { rows: [{ ...EVERY[0], namespace: undefined }], next_cursor: null, ignored_args: ["namespaces"] };
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    // Legal is refused and kept under the collections' answer, which is read again once.
    await waitFor(() => expect(api.listCollections.mock.calls.length).toBeGreaterThanOrEqual(2));
    await settle();
    // Then the reads land on a replica that reads one place per call.
    older = true;
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "tag:one" } });
    expect(
      await screen.findByText(
        "Only Namespace team is shown: this server lists one place per read, so 2 other places are not.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Not shown, as you can't read it: Legal.")).toBeInTheDocument();
  });

  it("reads the collections again when a place is refused again after a read that found none", async () => {
    let refuse = true;
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (refuse && places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    await waitFor(() => expect(collectionPlaces.invalidate).toHaveBeenCalledTimes(1));
    await settle();
    // Granted, and the collections answer again: the list reads Legal, and finds none refused.
    refuse = false;
    act(() => invalidateSharedCollections());
    expect(await screen.findByText("Services agreement")).toBeInTheDocument();
    await settle();
    expect(collectionPlaces.invalidate).toHaveBeenCalledTimes(2);
    // Revoked again: its refusal reads the collections again.
    refuse = true;
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "tag:two" } });
    await waitFor(() => expect(collectionPlaces.invalidate).toHaveBeenCalledTimes(3));
  });

  it("keeps one status region for its notes, there before any note is", async () => {
    let answer = (_v: unknown) => {};
    api.listKnowledge.mockImplementation(
      () =>
        new Promise((r) => {
          answer = r;
        }),
    );
    render(<Documents />);
    await waitFor(() => expect(api.listKnowledge).toHaveBeenCalled());
    const region = screen.getAllByRole("status").find((el) => el.classList.contains("empty:mb-0"));
    expect(region).toBeEmptyDOMElement();
    await act(async () => answer({ rows: [EVERY[0]], next_cursor: null, ignored_args: ["namespaces"] }));
    await waitFor(() => expect(region).toHaveTextContent(/^Only Namespace team is shown/));
  });

  it("says how many places it read, not of how many, when the collections could not be loaded", async () => {
    api.listCollections.mockRejectedValue(new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "busy" }));
    render(<Documents />);
    await screen.findByText("Team plan");
    expect(screen.getByText("Only Namespace team is shown: the collections could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/1 document · 2 chunks · 1 place read/)).toBeInTheDocument();
  });

  it("says how many places it read, not of how many, when the collections' list stops early", async () => {
    api.listCollections.mockResolvedValue({ collections: COLLECTIONS, next_cursor: "more" });
    render(<Documents />);
    await screen.findByText("Services agreement");
    expect(screen.getByText(/· 4 places read/)).toBeInTheDocument();
  });

  it("names what a passage search leaves out when one place is left to search", async () => {
    api.listCollections.mockResolvedValue({ collections: [COLLECTIONS[2]], next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Team plan");
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    expect(
      screen.getByText(/Searches every document in 1 place, whatever the tag filter; 1 other place is not searched/),
    ).toBeInTheDocument();
  });

  it("says a filter that matches nothing ran over the places read when one was refused", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "zzz" } });
    expect(await screen.findByText(/in the places read/)).toHaveTextContent(
      "No documents match zzz in the places read",
    );
  });

  for (const strict of [false, true]) {
    it(`reads the list once when the collections answer again without a refused place${strict ? ", in StrictMode" : ""}`, async () => {
      let refused = false;
      api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
        const places = [id.namespace, ...(opts.namespaces ?? [])];
        if (places.includes("col-legal")) {
          refused = true;
          throw REFUSED;
        }
        return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
      });
      // The refusal reads the collections again, and their new answer no longer lists Legal.
      api.listCollections.mockImplementation(async () => ({
        collections: refused ? COLLECTIONS.slice(0, 2) : COLLECTIONS,
        next_cursor: null,
      }));
      render(
        strict ? (
          <StrictMode>
            <Documents />
          </StrictMode>
        ) : (
          <Documents />
        ),
      );
      await screen.findByText("Drain a lane");
      await waitFor(() => expect(screen.queryByText(/Not shown/)).toBeNull());
      await settle();
      const pages = api.listKnowledge.mock.calls.filter((c) => (c[1] as ListOpts).limit !== 1).map(placesOf);
      // The refused read, the read of the rest, and one read under the new answer.
      expect(pages).toEqual([
        ["team", "col-me", "col-eng", "col-legal"],
        ["team", "col-me", "col-eng"],
        ["team", "col-me", "col-eng"],
      ]);
    });
  }

  it("reads a collection opened after its refusal in All documents, and leaves it as refused", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Not shown, as you can't read it: Legal.");
    await settle();
    const before = api.listKnowledge.mock.calls.length;
    act(() => {
      history.pushState(null, "", "/library?collection=col-legal");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    // The collection's own read asks the server, not the refusal kept for All documents.
    await waitFor(() =>
      expect(
        api.listKnowledge.mock.calls
          .slice(before)
          .some((c) => placesOf(c).join() === "col-legal" && (c[1] as ListOpts).limit === 50),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(toasts.toast).toHaveBeenCalledWith(
        expect.stringMatching(/^col-legal is not among the collections/),
        "info",
      ),
    );
    expect(screen.queryByText("This collection is empty")).toBeNull();
  });

  it("shows a header namespace named all as that namespace, not as All documents", async () => {
    useStore.setState({
      myNamespaces: [{ namespace: "all", role: "writer", status: "active", kind: "memory" } as never],
      identity: { org: "acme", namespace: "all", agent: "loom-ui" },
    });
    at("/library");
    api.listKnowledge.mockResolvedValue({ rows: [doc("all", "notes/a.md", "A note")], next_cursor: null });
    render(<Documents />);
    await screen.findByText("A note");
    await settle();
    expect(screen.getByLabelText("Show the documents of")).not.toHaveTextContent("All documents");
    expect(api.listKnowledge.mock.calls.every((c) => !(c[1] as ListOpts).namespaces)).toBe(true);
  });

  it("says nothing is in any place only when every place was read and holds none", async () => {
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    render(<Documents />);
    expect(await screen.findByText("No documents in any place you read")).toBeInTheDocument();
  });

  it("says there is no place to read when the token reaches none", async () => {
    api.listCollections.mockResolvedValue({ collections: [], next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" }, myNamespaces: [] });
    render(<Documents />);
    expect(await screen.findByText("No place to read yet")).toBeInTheDocument();
    expect(api.listKnowledge).not.toHaveBeenCalled();
  });
});

describe("The Library's search scope", () => {
  beforeEach(() => {
    resetNamespaceRoleCache();
    api.listCollections.mockResolvedValue({ collections: COLLECTIONS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "w", org: "acme", roles: ["WRITER"], namespaces: ["team"] },
      myNamespaces: [{ namespace: "team", role: "writer", status: "active", kind: "memory" } as never],
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "knowledge",
      pendingDocSourceUri: null,
    });
    at("/library?collection=col-eng");
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null, whoami: null, myNamespaces: null });
    at("/");
  });

  it("searches the collection shown, or every place once Everywhere is picked", async () => {
    render(<Documents />);
    await screen.findByText("Drain a lane");
    // Offered once something is searched.
    expect(screen.queryByRole("group", { name: "Search in" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    const scope = screen.getByRole("group", { name: "Search in" });
    // The option picked is said, not only drawn.
    expect(within(scope).getByRole("button", { name: "This collection" })).toHaveAttribute("aria-pressed", "true");
    expect(within(scope).getByRole("button", { name: "Everywhere" })).toHaveAttribute("aria-pressed", "false");
    // Here: one place read, and its rows say no place.
    expect(api.listKnowledge.mock.calls.every((c) => !(c[1] as ListOpts).namespaces)).toBe(true);
    expect(screen.queryByText("Team plan")).toBeNull();
    fireEvent.click(within(scope).getByRole("button", { name: "Everywhere" }));
    expect(await screen.findByText("Team plan")).toBeInTheDocument();
    const read = api.listKnowledge.mock.calls.at(-1) as unknown[];
    expect(placesOf(read)).toEqual(["team", "col-me", "col-eng", "col-legal"]);
    const row = screen.getByText("Team plan").closest("[data-doc-row]") as HTMLElement;
    expect(within(row).getByRole("link", { name: "Namespace team" })).toBeInTheDocument();
    // An empty search lists the place shown again.
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "" } });
    await waitFor(() => expect(screen.queryByText("Team plan")).toBeNull());
  });

  it("says anything added to an empty collection is shared, never from a read that left it out", async () => {
    api.listGrants.mockResolvedValue({
      rows: [
        { subject: "w", role: "admin", status: "active", granted_at: "2026-09-28 12:00:00", granted_by: "w" },
        { subject: "bob", role: "viewer", status: "active", granted_at: "2026-09-28 12:00:00", granted_by: "w" },
      ],
      next_cursor: null,
    });
    // The collection holds nothing; read with the others, its read fails.
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-eng") && places.length > 1) throw REFUSED;
      if (places.includes("col-eng") && opts.limit === 1)
        throw new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "busy" });
      return { rows: [], next_cursor: null };
    });
    render(<Documents />);
    expect(await screen.findByText(/Anything added here is shared with them/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
    );
    await screen.findByText(/Couldn't read Engineering/);
    expect(screen.queryByText(/Anything added here is shared with them/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "" } });
    expect(await screen.findByText(/Anything added here is shared with them/)).toBeInTheDocument();
  });

  it("starts each visit to a place here, also a return to one searched everywhere", async () => {
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
    );
    await screen.findByText("Team plan");
    for (const path of ["/library?collection=col-me", "/library?collection=col-eng"]) {
      act(() => {
        history.pushState(null, "", path);
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
      await settle();
    }
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    const scope = screen.getByRole("group", { name: "Search in" });
    expect(within(scope).getByRole("button", { name: "This collection" })).toHaveAttribute("aria-pressed", "true");
    expect(placesOf(api.listKnowledge.mock.calls.at(-1) as unknown[])).toEqual(["col-eng"]);
  });

  it("says Load more failed when one place's cursor is refused, with no read from the top", async () => {
    api.listKnowledge.mockImplementation(async (_id: unknown, opts: ListOpts) => {
      if (opts.cursor) throw new ApiError({ status: 422, code: "VALIDATION_FAILED", message: "bad cursor" });
      return { rows: EVERY.filter((r) => r.namespace === "col-eng"), next_cursor: "c-2" };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() =>
      expect(toasts.toastError).toHaveBeenCalledWith("Load more failed", expect.any(ApiError), { read: true }),
    );
    expect(toasts.toastWithId).not.toHaveBeenCalled();
    expect(api.listKnowledge.mock.calls.filter((c) => !(c[1] as ListOpts).cursor)).toHaveLength(1);
  });

  for (const [what, everywhere] of [
    ["fails", () => Promise.reject(new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "busy" }))],
    ["is still reading", () => new Promise(() => {})],
  ] as const) {
    it(`drops the passages found here when the search goes everywhere and its read ${what}`, async () => {
      api.searchKnowledge.mockResolvedValue([
        { source_uri: "runbooks/drain.md", chunk_idx: 0, content: "Drain it.", section: "", score: 1 },
      ]);
      api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
        if (opts.namespaces?.length) return everywhere();
        return { rows: EVERY.filter((r) => r.namespace === id.namespace), next_cursor: null };
      });
      render(<Documents />);
      await screen.findByText("Drain a lane");
      fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
      fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "drain" } });
      fireEvent.click(screen.getByRole("button", { name: "Find" }));
      await screen.findByRole("list", { name: "Passages found" });
      fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
      await settle();
      fireEvent.click(
        within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
      );
      await settle();
      // The hits were this collection's: they do not stand for every place.
      expect(screen.queryByRole("list", { name: "Passages found" })).toBeNull();
      expect(screen.getByLabelText("Passage to find")).toHaveValue("drain");
      expect(api.searchKnowledge).toHaveBeenCalledTimes(1);
    });
  }

  it("says a passage search reads every place while the list is still reading them", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      if (opts.namespaces?.length) return new Promise(() => {});
      return { rows: EVERY.filter((r) => r.namespace === id.namespace), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    expect(screen.getByText(/Searches every document in this library/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
    );
    await settle();
    expect(screen.getByText(/Searches every document in every place you read/)).toBeInTheDocument();
  });

  it("waits for the namespace's role before a row of it takes writes, in a search everywhere", async () => {
    api.listMine.mockImplementation(() => new Promise(() => {}));
    useStore.setState({ myNamespaces: null });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
    );
    await screen.findByText("Team plan");
    expect(screen.queryByRole("checkbox", { name: "Select Team plan" })).toBeNull();
    expect(screen.getByRole("checkbox", { name: "Select Drain a lane" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Actions for Team plan" }));
    const menu = await screen.findByRole("menu", { name: "Actions for Team plan" });
    expect(within(menu).getByRole("menuitem", { name: "Delete" })).toHaveAttribute("aria-disabled", "true");
    expect(within(menu).getByRole("menuitem", { name: "Add to collection…" })).toHaveAttribute("aria-disabled", "true");
    expect(menu).toHaveTextContent("Your role on this namespace is still loading.");
  });

  it("says a passage search cannot run while the list's read of every place failed", async () => {
    api.searchKnowledge.mockResolvedValue([]);
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      if (opts.namespaces?.length) throw new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "busy" });
      return { rows: EVERY.filter((r) => r.namespace === id.namespace), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
    );
    expect(await screen.findByText("Couldn't load documents")).toBeInTheDocument();
    // The panel stays, and Find says why it does not run rather than spinning.
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "drain" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    expect(await screen.findByText(/The places to search could not be read/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find" })).not.toHaveAttribute("aria-busy", "true");
    expect(api.searchKnowledge).not.toHaveBeenCalled();
  });

  it("keeps Find a passage open, with its query and hits, when the search goes everywhere", async () => {
    api.searchKnowledge.mockResolvedValue([
      { source_uri: "runbooks/drain.md", chunk_idx: 0, content: "Drain it.", section: "", score: 1 },
    ]);
    render(<Documents />);
    await screen.findByText("Drain a lane");
    fireEvent.click(screen.getAllByRole("button", { name: "Find a passage" })[0]);
    fireEvent.change(screen.getByLabelText("Passage to find"), { target: { value: "drain" } });
    fireEvent.click(screen.getByRole("button", { name: "Find" }));
    await screen.findByRole("list", { name: "Passages found" });
    expect((api.searchKnowledge.mock.calls[0][3] as ListOpts).namespaces).toEqual([]);
    fireEvent.change(screen.getByLabelText("Search library"), { target: { value: "a" } });
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Search in" })).getByRole("button", { name: "Everywhere" }),
    );
    await screen.findByText("Team plan");
    // The panel stayed, kept what was asked, and asked it again of every place.
    expect(screen.getByLabelText("Passage to find")).toHaveValue("drain");
    await waitFor(() => expect(api.searchKnowledge).toHaveBeenCalledTimes(2));
    const [id, , , opts] = api.searchKnowledge.mock.calls[1] as [{ namespace: string }, string, number, ListOpts];
    expect([id.namespace, ...(opts.namespaces ?? [])]).toEqual(["team", "col-me", "col-eng", "col-legal"]);
    expect(screen.getByRole("list", { name: "Passages found" })).toBeInTheDocument();
  });
});
