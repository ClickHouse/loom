// All documents: one list of every place the person reads, each row saying where it lives
// and taking its writes from its own place; what a read of several places left out (a
// place refused or failed, a server that reads one place per call); and a search scoped to
// the place shown or everywhere.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { ApiError } from "../lib/errors";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
import { Documents } from "./Documents";

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(),
  listCollections: vi.fn(),
  bulkDeleteKnowledge: vi.fn(),
  searchKnowledge: vi.fn(),
  getKnowledge: vi.fn(async () => ({ found: true, content: "text" })),
  fetchKnowledgePreview: vi.fn(async () => null),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

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
    await userEvent.click(within(bar).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.bulkDeleteKnowledge).toHaveBeenCalledTimes(2));
    expect(api.bulkDeleteKnowledge).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, ["notes/plan.md"]);
    expect(api.bulkDeleteKnowledge).toHaveBeenCalledWith({ org: "acme", namespace: "col-eng" }, ["runbooks/drain.md"]);
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

  it("names a place it can no longer read and keeps the others", async () => {
    api.listKnowledge.mockImplementation(async (id: { namespace: string }, opts: ListOpts) => {
      const places = [id.namespace, ...(opts.namespaces ?? [])];
      if (places.includes("col-legal")) throw REFUSED;
      return { rows: EVERY.filter((r) => places.includes(r.namespace)), next_cursor: null };
    });
    render(<Documents />);
    await screen.findByText("Drain a lane");
    expect(screen.getByText("Not shown, since you can no longer read it: Legal.")).toBeInTheDocument();
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
    expect(screen.getByText("Not shown, since you can no longer read it: Legal.")).toBeInTheDocument();
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
  });

  it("says Add waits while the collections are read, with no header namespace to add to", async () => {
    api.listCollections.mockImplementation(() => new Promise(() => {}));
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" }, myNamespaces: [] });
    render(<Documents />);
    const add = await screen.findByRole("button", { name: "Add document" });
    expect(add).toHaveAttribute("aria-disabled", "true");
    expect(add).toHaveAccessibleDescription("Your role on this namespace is still loading.");
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
    expect(within(scope).getByRole("button", { name: "This collection" })).toBeInTheDocument();
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
