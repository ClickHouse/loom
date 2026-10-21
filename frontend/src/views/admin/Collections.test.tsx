/** The Collections page lists what `collection.list` answers, opens a collection's members
 *  and documents, deletes one only after a confirm, and creates one under a valid name. */
import userEvent from "@testing-library/user-event";
import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { ApiError } from "../../lib/errors";
import { axeViolations } from "../../test/axe";
import { collectionNameError, parentChoices } from "../../flows/CreateCollectionDialog";
import { Collections } from "./Collections";

const listCollections = vi.fn();
const createCollection = vi.fn();
const deleteCollection = vi.fn();
const getNamespace = vi.fn();
const listGrants = vi.fn();
const listKnowledge = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listCollections: (...a: unknown[]) => listCollections(...a),
    createCollection: (...a: unknown[]) => createCollection(...a),
    deleteCollection: (...a: unknown[]) => deleteCollection(...a),
    getNamespace: (...a: unknown[]) => getNamespace(...a),
    listGrants: (...a: unknown[]) => listGrants(...a),
    listKnowledge: (...a: unknown[]) => listKnowledge(...a),
  };
});
const confirmAction = vi.fn();
vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: (...a: unknown[]) => confirmAction(...a) }));
const toast = vi.fn();
vi.mock("../../components/Toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../components/Toast")>();
  return { ...actual, toast: (...a: unknown[]) => toast(...a) };
});

const HANDBOOK = { namespace: "col-handbook", parent: "", personal: false, role: "admin" };
const ONCALL = { namespace: "col-oncall", parent: "col-handbook", personal: false, role: "viewer" };
const page = (...collections: unknown[]) => ({ collections, next_cursor: null });

beforeEach(() => {
  for (const f of [listCollections, createCollection, deleteCollection, getNamespace, listGrants, listKnowledge])
    f.mockReset();
  confirmAction.mockReset();
  toast.mockReset();
  getNamespace.mockResolvedValue({
    namespace: "col-handbook",
    description: "Team runbooks",
    created_at: "2026-10-01 10:00:00",
  });
  listGrants.mockResolvedValue({
    rows: [
      { subject: "ana@example.com", role: "admin", status: "active", granted_at: "", granted_by: "" },
      { subject: "group:oncall", role: "viewer", status: "active", granted_at: "", granted_by: "" },
    ],
  });
  listKnowledge.mockResolvedValue({
    rows: [{ source_uri: "kb://restart", title: "Restarting a replica", ingested_at: "2026-10-02 10:00:00" }],
    next_cursor: null,
  });
  useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null });
});

const table = () => screen.getByTestId("collections-table");
const rowOf = (name: string) => screen.getByRole("button", { name }).closest("tr") as HTMLElement;

describe("Collections: the list", () => {
  it("draws the first read as the table's header over a skeleton, marked busy", async () => {
    listCollections.mockReturnValue(new Promise(() => {}));
    render(<Collections />);
    const t = await screen.findByRole("table", { name: "Collections" });
    expect(t).toHaveAttribute("aria-busy", "true");
    expect(within(t).getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
  });

  it("lists each collection with where it sits and the caller's role, and counts them", async () => {
    listCollections.mockResolvedValueOnce(page(HANDBOOK, ONCALL));
    render(<Collections />);
    await screen.findByRole("button", { name: "col-oncall" });
    expect(listCollections).toHaveBeenCalledWith("acme", expect.anything());
    expect(within(rowOf("col-handbook")).getByText("Top level")).toBeInTheDocument();
    expect(within(rowOf("col-oncall")).getByText("col-handbook")).toBeInTheDocument();
    expect(within(rowOf("col-oncall")).getByText("Viewer")).toBeInTheDocument();
    expect(screen.getByText("2 collections")).toBeInTheDocument();
  });

  it("says when there are none, and when a search matches none", async () => {
    listCollections.mockResolvedValueOnce(page());
    render(<Collections />);
    expect(await screen.findByText("No collections yet")).toBeInTheDocument();
    cleanup();
    listCollections.mockResolvedValueOnce(page(HANDBOOK));
    render(<Collections />);
    await screen.findByRole("button", { name: "col-handbook" });
    await userEvent.type(screen.getByRole("textbox", { name: "Search collections" }), "zzz");
    expect(await screen.findByText(/No collections match/)).toBeInTheDocument();
  });

  it("says a failed read failed, with its request id and Retry, never the empty state", async () => {
    listCollections.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "UNAVAILABLE", message: "down", requestId: "req-7" }),
    );
    render(<Collections />);
    expect(await screen.findByText("Couldn't load collections")).toBeInTheDocument();
    expect(screen.getByText(/req-7/)).toBeInTheDocument();
    expect(screen.queryByText("No collections yet")).toBeNull();
    listCollections.mockResolvedValueOnce(page(HANDBOOK));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "col-handbook" })).toBeInTheDocument();
  });

  it("loads the next page after the last one read, and counts what is loaded as a floor", async () => {
    listCollections.mockResolvedValueOnce({ collections: [HANDBOOK], next_cursor: "col-handbook" });
    render(<Collections />);
    await screen.findByRole("button", { name: "col-handbook" });
    expect(screen.getByText("1+ collections")).toBeInTheDocument();
    listCollections.mockResolvedValueOnce(page(ONCALL));
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("button", { name: "col-oncall" })).toBeInTheDocument();
    expect(listCollections).toHaveBeenLastCalledWith("acme", { cursor: "col-handbook" });
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(table()).toBeInTheDocument();
    // The last page took away the button that held the focus: the first row it added has it.
    await waitFor(() => expect(screen.getByRole("button", { name: "col-oncall" })).toHaveFocus());
  });

  it("hands focus to the search when the last page adds no row the search shows", async () => {
    listCollections.mockResolvedValueOnce({ collections: [HANDBOOK], next_cursor: "col-handbook" });
    render(<Collections />);
    await screen.findByRole("button", { name: "col-handbook" });
    const search = screen.getByRole("textbox", { name: "Search collections" });
    await userEvent.type(search, "zzz");
    await screen.findByText(/No collections match/);
    listCollections.mockResolvedValueOnce(page(ONCALL));
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load more" })).toBeNull());
    await waitFor(() => expect(search).toHaveFocus());
  });

  it("says a search that matches none covers only the pages loaded", async () => {
    listCollections.mockResolvedValueOnce({ collections: [HANDBOOK], next_cursor: "col-handbook" });
    render(<Collections />);
    await screen.findByRole("button", { name: "col-handbook" });
    await userEvent.type(screen.getByRole("textbox", { name: "Search collections" }), "zzz");
    expect(await screen.findByText(/in the first 1 loaded/)).toBeInTheDocument();
  });
});

describe("Collections: a collection's details", () => {
  it("shows its description, its members and its documents", async () => {
    listCollections.mockResolvedValueOnce(page(HANDBOOK));
    render(<Collections />);
    await userEvent.click(await screen.findByRole("button", { name: "col-handbook" }));
    const dialog = await screen.findByRole("dialog", { name: "col-handbook" });
    expect(await within(dialog).findByText("group:oncall")).toBeInTheDocument();
    expect(within(dialog).getByText("Team runbooks")).toBeInTheDocument();
    expect(within(dialog).getByText("Restarting a replica")).toBeInTheDocument();
    expect(within(dialog).getByText("1 in all")).toBeInTheDocument();
    expect(listKnowledge).toHaveBeenCalledWith(
      { org: "acme", namespace: "col-handbook" },
      expect.objectContaining({ limit: 25 }),
    );
  });

  it("opens on the dialog itself, not on Close or Delete", async () => {
    listCollections.mockResolvedValueOnce(page(HANDBOOK));
    render(<Collections />);
    await userEvent.click(await screen.findByRole("button", { name: "col-handbook" }));
    const dialog = await screen.findByRole("dialog", { name: "col-handbook" });
    await waitFor(() => expect(dialog).toHaveFocus());
  });

  it("says it lists the newest documents when the collection holds more", async () => {
    listKnowledge.mockResolvedValue({ rows: [], next_cursor: "next" });
    listCollections.mockResolvedValueOnce(page(HANDBOOK));
    render(<Collections />);
    await userEvent.click(await screen.findByRole("button", { name: "col-handbook" }));
    expect(await screen.findByText("the newest 25")).toBeInTheDocument();
  });

  it("keeps Delete reachable but unavailable, with its reason, for a viewer", async () => {
    listCollections.mockResolvedValueOnce(page(ONCALL));
    render(<Collections />);
    await userEvent.click(await screen.findByRole("button", { name: "col-oncall" }));
    const dialog = await screen.findByRole("dialog", { name: "col-oncall" });
    const del = within(dialog).getByRole("button", { name: "Delete" });
    expect(del).toHaveAttribute("aria-disabled", "true");
    expect(del).toHaveAccessibleDescription("Only an admin of this collection can delete it");
    await userEvent.click(del);
    expect(confirmAction).not.toHaveBeenCalled();
  });
});

describe("Collections: delete", () => {
  it("deletes only after the confirm, then reads the list again", async () => {
    listCollections.mockResolvedValue(page(HANDBOOK));
    deleteCollection.mockResolvedValue({
      namespace: "col-handbook",
      deleted: true,
      erasure_id: "e1",
      erasure_status: "completed",
    });
    render(<Collections />);
    await screen.findByRole("button", { name: "col-handbook" });

    confirmAction.mockResolvedValueOnce(false);
    await userEvent.click(screen.getByRole("button", { name: "Actions for col-handbook" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(confirmAction).toHaveBeenCalledTimes(1));
    expect(confirmAction.mock.calls[0][0]).toMatchObject({ danger: true, title: "Delete col-handbook?" });
    expect(deleteCollection).not.toHaveBeenCalled();

    confirmAction.mockResolvedValueOnce(true);
    await userEvent.click(screen.getByRole("button", { name: "Actions for col-handbook" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(deleteCollection).toHaveBeenCalledWith("acme", "col-handbook"));
    await waitFor(() => expect(listCollections).toHaveBeenCalledTimes(2));
    expect(toast).toHaveBeenCalledWith("Deleted col-handbook", "ok");
    // The read that drops the row hands focus to the search.
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Search collections" })).toHaveFocus());
  });

  it("says when erasing what it held failed and is being retried, with the erasure id", async () => {
    listCollections.mockResolvedValue(page(HANDBOOK));
    deleteCollection.mockResolvedValue({
      namespace: "col-handbook",
      deleted: true,
      erasure_id: "e9",
      erasure_status: "failed",
    });
    confirmAction.mockResolvedValueOnce(true);
    render(<Collections />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for col-handbook" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("Deleted col-handbook; erasing what it held is being retried", "info", {
        detail: "erasure e9",
      }),
    );
  });

  it("offers no Delete in the menu of a collection the caller does not administer", async () => {
    listCollections.mockResolvedValueOnce(page(ONCALL));
    render(<Collections />);
    await screen.findByRole("button", { name: "col-oncall" });
    await userEvent.click(screen.getByRole("button", { name: "Actions for col-oncall" }));
    await screen.findByRole("menuitem", { name: "Open" });
    expect(screen.queryByRole("menuitem", { name: "Delete" })).toBeNull();
  });
});

describe("Collections: members", () => {
  it("lets an admin whose role comes from a parent or a group manage the members", async () => {
    // No grant row names the caller: the role collection.list reports is what counts.
    listCollections.mockResolvedValueOnce(page({ ...ONCALL, role: "admin" }));
    render(<Collections />);
    await userEvent.click(await screen.findByRole("button", { name: "Actions for col-oncall" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Members" }));
    const dialog = await screen.findByRole("dialog", { name: "Members of col-oncall" });
    expect(await within(dialog).findByRole("button", { name: "Add member" })).toBeInTheDocument();
    expect(listGrants).toHaveBeenCalledWith({ org: "acme", namespace: "col-oncall" });
  });
});

describe("Collections: create", () => {
  it("offers as parents only the collections the caller writes, never a personal one", () => {
    const mine = { namespace: "col-me-1a2b", parent: "", personal: true, role: "admin" } as const;
    const writer = { ...ONCALL, namespace: "col-writes", role: "writer" } as const;
    const rows = [HANDBOOK, ONCALL, writer, mine] as Parameters<typeof parentChoices>[0];
    expect(parentChoices(rows).map((c) => c.namespace)).toEqual(["col-handbook", "col-writes"]);
  });

  it("checks the name before it is sent", () => {
    expect(collectionNameError("col-handbook")).toBeNull();
    expect(collectionNameError("handbook")).toMatch(/Start the name with "col-"/);
    expect(collectionNameError("col-")).toMatch(/Start the name/);
    expect(collectionNameError("col-me-x")).toMatch(/personal/);
    expect(collectionNameError("col-Hand book")).toMatch(/lowercase/);
  });

  it("creates a top-level collection and reads the list again", async () => {
    listCollections.mockResolvedValue(page(HANDBOOK));
    createCollection.mockResolvedValueOnce({ namespace: "col-runbooks", parent: "", created: true });
    render(<Collections />);
    await screen.findByRole("button", { name: "col-handbook" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const dialog = await screen.findByRole("dialog", { name: "New collection" });
    const name = within(dialog).getByRole("textbox", { name: "Name" });
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeDisabled();
    await userEvent.type(name, "runbooks");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Description (optional)" }), "How we run it");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(createCollection).toHaveBeenCalledWith("acme", "col-runbooks", {
        parent: "",
        description: "How we run it",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "New collection" })).toBeNull());
    await waitFor(() => expect(listCollections).toHaveBeenCalledTimes(2));
  });

  it("shows the server's refusal in the dialog", async () => {
    listCollections.mockResolvedValue(page());
    createCollection.mockRejectedValueOnce(
      new ApiError({ status: 412, code: "PRECONDITION_FAILED", message: "the name 'col-x' is taken in acme" }),
    );
    render(<Collections />);
    await screen.findByText("No collections yet");
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const dialog = await screen.findByRole("dialog", { name: "New collection" });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "x");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/taken/);
  });
});

describe("Collections: accessibility", () => {
  it("has no axe violations in the list and in a collection's details", async () => {
    listCollections.mockResolvedValueOnce(page(HANDBOOK, ONCALL));
    const { container } = render(<Collections />);
    await screen.findByRole("button", { name: "col-oncall" });
    expect(await axeViolations(container)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "col-handbook" }));
    const dialog = await screen.findByRole("dialog", { name: "col-handbook" });
    await within(dialog).findByText("group:oncall");
    await act(async () => {});
    expect(await axeViolations(dialog)).toEqual([]);
  });
});
