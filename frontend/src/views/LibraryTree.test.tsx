// The Library's tree of places: the header's namespace, My files, the person's collections
// nested, shown or hidden, those shared with them, and New collection inline.
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionEntry } from "../api";
import { axeViolations } from "../test/axe";
import { act, cleanup, fireEvent, render, type RenderOptions, screen, waitFor, within } from "../test/rtl";
import { repeatedTitles } from "../lib/collections";
import { DRAG_TYPE } from "../lib/useDragHold";
import { LibraryTree, NEW_COLLECTION_FOCUS, placeFocusKey, placeTree } from "./LibraryTree";

const api = vi.hoisted(() => ({ createCollection: vi.fn(), moveCollection: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const toast = vi.hoisted(() => ({ toast: vi.fn(), toastError: vi.fn() }));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  ...toast,
}));

const entry = (namespace: string, over: Partial<CollectionEntry> = {}): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "admin",
  description: "",
  title: "",
  ...over,
});

const ROWS = [
  entry("col-me-1a2b", { personal: true }),
  entry("col-eng", { title: "Engineering" }),
  entry("col-runbooks", { parent: "col-eng", title: "Runbooks", role: "writer" }),
  entry("col-ingest", { parent: "col-runbooks", title: "Ingest", role: "writer" }),
  entry("col-legal", { title: "Legal", role: "viewer" }),
];

beforeEach(() => {
  localStorage.clear();
  api.createCollection.mockReset();
  api.moveCollection.mockReset();
  toast.toast.mockReset();
  toast.toastError.mockReset();
});
afterEach(cleanup);

/** `n` collections named col-k000… and titled Kid 000…, with `over` on each. */
function kids(n: number, over: Partial<CollectionEntry> = {}): CollectionEntry[] {
  const pad = (i: number) => String(i).padStart(3, "0");
  return Array.from({ length: n }, (_, i) => entry(`col-k${pad(i)}`, { title: `Kid ${pad(i)}`, ...over }));
}

/** The folds saved for ann in acme. */
function saved(subject = "ann"): string[] {
  return JSON.parse(localStorage.getItem(`loom_library_expanded_v1:${subject}:acme`) ?? "[]");
}

/** The places listed, in order. */
function places(): string[] {
  return screen.queryAllByRole("link").map((a) => a.textContent ?? "");
}

type TreeProps = Parameters<typeof LibraryTree>[0];

/** The tree with these props over the defaults (`byName` follows `rows`); `update` draws it
 *  again with more of them. */
function tree(over: Partial<TreeProps> = {}, options: RenderOptions = {}) {
  const onPick = vi.fn();
  const onCreated = vi.fn();
  let props: Partial<TreeProps> = over;
  const ui = () => {
    const p = {
      subject: "ann",
      org: "acme",
      rows: ROWS,
      repeated: new Set<string>(),
      namespace: "bench",
      current: null,
      viewHeight: 800,
      claimCreateVersion: () => 7,
      ...props,
    };
    const byName = new Map((p.rows ?? []).map((c) => [c.namespace, c]));
    return <LibraryTree byName={byName} onPick={onPick} onCreated={onCreated} {...p} />;
  };
  const { rerender } = render(ui(), options);
  const update = (more: Partial<TreeProps>) => {
    props = { ...props, ...more };
    rerender(ui());
  };
  return { onPick, onCreated, update };
}

describe("placeTree", () => {
  it("sorts the roots into the person's own and the shared, and a shared child under its parent", () => {
    const t = placeTree(ROWS);
    expect(t.own?.namespace).toBe("col-me-1a2b");
    expect(t.mine.map((c) => c.namespace)).toEqual(["col-eng"]);
    // Runbooks is shared with the caller (writer) inside their own Engineering: not twice.
    expect(t.shared.map((c) => c.namespace)).toEqual(["col-legal"]);
    expect(t.children.get("col-eng")?.map((c) => c.namespace)).toEqual(["col-runbooks"]);
  });
});

describe("LibraryTree", () => {
  it("lists All documents, the namespace, My files and the roots, the children hidden until shown", async () => {
    tree();
    expect(places()).toEqual(["All documents", "Namespace bench", "My files", "Engineering", "Legal"]);
    const show = screen.getByRole("button", { name: "Collections in Engineering" });
    expect(show).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(show);
    expect(places()).toEqual(["All documents", "Namespace bench", "My files", "Engineering", "Runbooks", "Legal"]);
    expect(screen.getByRole("button", { name: "Collections in Engineering" })).toHaveAttribute("aria-expanded", "true");
  });

  it("is marked busy when the page says the list is read again", () => {
    const { update } = tree();
    const nav = screen.getByRole("navigation", { name: "Library places" });
    expect(nav).not.toHaveAttribute("aria-busy");
    update({ busy: true });
    expect(nav).toHaveAttribute("aria-busy", "true");
    expect(places()).toContain("Engineering");
  });

  it("lists the first fifty top-level collections, Show more adding fifty, and the one shown wherever it sits", async () => {
    const many = kids(120);
    tree({ rows: many, namespace: null, current: "col-k080" });
    const mine = screen.getByRole("list", { name: "Collections" });
    const names = () =>
      within(mine)
        .getAllByRole("link")
        .map((a) => a.textContent);
    expect(names()).toHaveLength(51);
    expect(names().at(49)).toBe("Kid 049");
    expect(names().at(-1)).toBe("Kid 080");
    await userEvent.click(screen.getByRole("button", { name: "Show 49 more" }));
    expect(names()).toHaveLength(100);
    await userEvent.click(screen.getByRole("button", { name: "Show 20 more" }));
    expect(names()).toHaveLength(120);
    // The last press takes its button away: the focus goes to the first place it added.
    expect(screen.getByRole("link", { name: "Kid 100" })).toHaveFocus();
  });

  it("counts the one shown past the first fifty as drawn", () => {
    const many = kids(51);
    tree({ rows: many, namespace: null, current: "col-k050" });
    expect(within(screen.getByRole("list", { name: "Collections" })).getAllByRole("link")).toHaveLength(51);
    expect(screen.queryByRole("button", { name: /^Show \d+ more$/ })).toBeNull();
  });

  it("draws an open collection's first fifty children, and the one shown past them", async () => {
    const rows = [entry("col-eng", { title: "Engineering" }), ...kids(60, { parent: "col-eng" })];
    tree({ rows, namespace: null, current: "col-k055" });
    const kidLinks = () => screen.getAllByRole("link", { name: /^Kid / });
    expect(kidLinks()).toHaveLength(51);
    expect(kidLinks().at(-1)).toHaveTextContent("Kid 055");
    await userEvent.click(screen.getByRole("button", { name: "Show 9 more" }));
    expect(kidLinks()).toHaveLength(60);
  });

  it("names a show or hide button in a tooltip on focus", async () => {
    tree();
    const toggle = screen.getByRole("button", { name: "Collections in Engineering" });
    act(() => toggle.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Collections in Engineering");
    // Read once, as its name: not again as its description.
    expect(toggle).not.toHaveAccessibleDescription();
  });

  it("shows the parents of the collection shown, and what is inside it", () => {
    tree({ current: "col-runbooks" });
    expect(places()).toEqual([
      "All documents",
      "Namespace bench",
      "My files",
      "Engineering",
      "Runbooks",
      "Ingest",
      "Legal",
    ]);
    expect(screen.getByRole("link", { name: "Runbooks" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Engineering" })).not.toHaveAttribute("aria-current");
  });

  it("opens a place on a click, the namespace as null, and leaves a modified click to the browser", async () => {
    const { onPick } = tree();
    const legal = screen.getByRole("link", { name: "Legal" });
    expect(legal.getAttribute("href")).toContain("collection=col-legal");
    await userEvent.click(legal);
    expect(onPick).toHaveBeenLastCalledWith("col-legal", placeFocusKey("col-legal"), "Legal");
    await userEvent.click(screen.getByRole("link", { name: "Namespace bench" }));
    expect(onPick).toHaveBeenLastCalledWith(null, placeFocusKey(null), "Namespace bench");
    onPick.mockClear();
    fireEvent.click(legal, { ctrlKey: true });
    expect(onPick).not.toHaveBeenCalled();
  });

  it("keeps what was shown across a mount, per org", async () => {
    tree();
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    cleanup();
    tree();
    expect(places()).toContain("Runbooks");
    cleanup();
    tree({ org: "other" });
    expect(places()).not.toContain("Runbooks");
  });

  it("makes a collection inline inside the one shown, by its title", async () => {
    api.createCollection.mockResolvedValue({ namespace: "col-q4", parent: "col-eng", created: true, title: "Q4" });
    const { onCreated } = tree({ current: "col-eng" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: "New collection in Engineering" });
    expect(field).toHaveFocus();
    await userEvent.type(field, "Q4");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(api.createCollection).toHaveBeenCalledWith("acme", "", { parent: "col-eng", title: "Q4", deriveName: true });
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("col-q4", 7, NEW_COLLECTION_FOCUS));
    expect(toast.toast).toHaveBeenCalledWith("Created Q4", "ok");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByRole("button", { name: "New collection" })).toHaveFocus();
  });

  it("counts a title by character, as the server does, and refuses one past the limit", async () => {
    tree();
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: "New collection at the top level" });
    // 200 emoji are 400 UTF-16 units but 200 characters: allowed.
    fireEvent.change(field, { target: { value: "\u{1F600}".repeat(200) } });
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
    fireEvent.change(field, { target: { value: "a".repeat(201) } });
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    expect(field).toHaveAccessibleDescription("At most 200 characters.");
  });

  it("makes one at the top level where the person may not add inside the one shown", async () => {
    api.createCollection.mockResolvedValue({ namespace: "col-x", parent: "", created: false, title: "X" });
    tree({ current: "col-legal" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    await userEvent.type(screen.getByRole("textbox", { name: "New collection at the top level" }), "X");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(api.createCollection).toHaveBeenCalledWith("acme", "", { parent: "", title: "X", deriveName: true });
    await waitFor(() => expect(toast.toast).toHaveBeenCalledWith("X already exists here", "info"));
  });

  it("keeps the typed title when the create fails, and Cancel drops it", async () => {
    api.createCollection.mockRejectedValue(new Error("nope"));
    tree();
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: "New collection at the top level" });
    await userEvent.type(field, "Plans");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(toast.toastError).toHaveBeenCalled());
    expect(field).toHaveValue("Plans");
    // Ready to try again.
    expect(field).not.toHaveAttribute("readonly");
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toBeEnabled();
    await userEvent.click(create);
    await waitFor(() => expect(api.createCollection).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(toast.toastError).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByRole("button", { name: "New collection" })).toHaveFocus();
  });

  it("asks for a title before Create does anything", async () => {
    tree();
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    const field = screen.getByRole("textbox", { name: "New collection at the top level" });
    await userEvent.type(field, "   {enter}");
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    // Enter sends nothing either, nor does a title past the limit.
    fireEvent.change(field, { target: { value: "a".repeat(201) } });
    await userEvent.type(field, "{enter}");
    expect(api.createCollection).not.toHaveBeenCalled();
  });

  it("sends one create while it is answered, however often Enter or Create is pressed", async () => {
    let answer: (r: unknown) => void = () => {};
    api.createCollection.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const { onCreated } = tree();
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: "New collection at the top level" });
    await userEvent.type(field, "Plans{enter}");
    await userEvent.type(field, "{enter}");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(api.createCollection).toHaveBeenCalledTimes(1);
    await act(async () => answer({ namespace: "col-plans", parent: "", created: true, title: "Plans" }));
    expect(onCreated).toHaveBeenCalledTimes(1);
    expect(toast.toast).toHaveBeenCalledTimes(1);
    expect(toast.toast).toHaveBeenCalledWith("Created Plans", "ok");
  });

  it("lists a cycle in the list once, from the top, without hanging", () => {
    const rows = [
      ...ROWS,
      entry("col-a", { parent: "col-b", title: "A" }),
      entry("col-b", { parent: "col-a", title: "B" }),
    ];
    const t = placeTree(rows);
    // A's path stops where the cycle repeats, at B: B starts it.
    expect(t.mine.map((c) => c.namespace)).toEqual(["col-b", "col-eng"]);
    expect(t.children.get("col-b")?.map((c) => c.namespace)).toEqual(["col-a"]);
    expect(t.children.get("col-a")).toBeUndefined();
    tree({ rows, current: "col-a" });
    expect(places()).toEqual(["All documents", "Namespace bench", "My files", "B", "A", "Engineering", "Legal"]);
    expect(screen.getByRole("link", { name: "A" })).toHaveAttribute("aria-current", "page");
  });

  it("keeps a cycle's child under its parent", () => {
    const t = placeTree([
      entry("col-x", { parent: "col-y", title: "X" }),
      entry("col-y", { parent: "col-x", title: "Y" }),
      entry("col-c", { parent: "col-x", title: "A child" }),
    ]);
    expect(t.mine.map((c) => c.namespace)).toEqual(["col-y"]);
    expect(t.children.get("col-y")?.map((c) => c.namespace)).toEqual(["col-x"]);
    expect(t.children.get("col-x")?.map((c) => c.namespace)).toEqual(["col-c"]);
  });

  it("says it is loading only while the list is read, and nothing when the read failed", () => {
    vi.useFakeTimers();
    try {
      tree({ rows: null, namespace: null });
      act(() => vi.advanceTimersByTime(300));
      expect(screen.getByText("Loading collections")).toBeInTheDocument();
      cleanup();
      // With the namespace's row above it.
      tree({ rows: null });
      act(() => vi.advanceTimersByTime(300));
      expect(places()).toEqual(["All documents", "Namespace bench"]);
      expect(screen.getByText("Loading collections")).toBeInTheDocument();
      cleanup();
      tree({ rows: null, namespace: null, failed: true });
      act(() => vi.advanceTimersByTime(300));
      expect(screen.queryByText("Loading collections")).toBeNull();
      expect(screen.getByRole("button", { name: "New collection" })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("makes one at the top level after the list failed to load", async () => {
    tree({ rows: null, namespace: null, failed: true });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    expect(screen.getByRole("textbox", { name: "New collection at the top level" })).toHaveFocus();
  });

  it("opens the form at the tree's foot in place of its button, naming where it goes", async () => {
    tree({ current: "col-runbooks" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    expect(screen.queryByRole("button", { name: "New collection" })).toBeNull();
    // After every place, not inside the parent's row.
    const field = screen.getByRole("textbox", { name: "New collection in Runbooks" });
    const legal = screen.getByRole("link", { name: "Legal" });
    expect(legal.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the form, its text and the focus when a collection is hidden or the list answers", async () => {
    const { update } = tree({ namespace: null, current: "col-runbooks" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: "New collection in Runbooks" });
    await userEvent.type(field, "Plans");
    // Hiding the parent leaves the form at the foot mounted.
    fireEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    fireEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    update({ rows: [...ROWS] });
    expect(screen.getByRole("textbox", { name: "New collection in Runbooks" })).toBe(field);
    expect(field).toHaveValue("Plans");
    expect(field).toHaveFocus();
  });

  it("keeps the parent it was opened for when the page moves", async () => {
    api.createCollection.mockResolvedValue({ namespace: "col-x", parent: "col-runbooks", created: true, title: "X" });
    const { update } = tree({ namespace: null, current: "col-runbooks" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    update({ current: "col-eng" });
    await userEvent.type(screen.getByRole("textbox", { name: "New collection in Runbooks" }), "X");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(api.createCollection).toHaveBeenCalledWith("acme", "", {
      parent: "col-runbooks",
      title: "X",
      deriveName: true,
    });
  });

  it("keeps the parent it was opened for when a read no longer lists it, and the server decides", async () => {
    api.createCollection.mockResolvedValue({ namespace: "col-x", parent: "col-runbooks", created: true, title: "X" });
    const { update } = tree({ namespace: null, current: "col-runbooks" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: "New collection in Runbooks" });
    await userEvent.type(field, "X");
    update({ rows: ROWS.filter((c) => c.namespace !== "col-runbooks") });
    expect(field).toHaveAccessibleName("New collection in Runbooks");
    expect(field).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(api.createCollection).toHaveBeenCalledWith("acme", "", {
      parent: "col-runbooks",
      title: "X",
      deriveName: true,
    });
  });

  it("opens the parents of a linked collection once the list names it", () => {
    const { update } = tree({
      namespace: null,
      current: "col-ingest",
      rows: ROWS.filter((c) => c.namespace !== "col-ingest"),
    });
    expect(places()).not.toContain("Runbooks");
    update({ rows: ROWS });
    expect(places()).toEqual(["All documents", "My files", "Engineering", "Runbooks", "Ingest", "Legal"]);
  });

  it("saves only what the person opened, not what a move showed", () => {
    tree({ current: "col-ingest" });
    expect(places()).toContain("Ingest");
    expect(saved()).toEqual([]);
  });

  it("keeps one person's folds through another person's visit in the same org", async () => {
    tree();
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    expect(saved()).toEqual(["col-eng"]);
    cleanup();
    tree({ subject: "bob" });
    expect(screen.getByRole("button", { name: "Collections in Engineering" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    expect(saved("bob")).toEqual(["col-eng"]);
    expect(saved()).toEqual(["col-eng"]);
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    expect(saved("bob")).toEqual([]);
    cleanup();
    tree();
    expect(screen.getByRole("button", { name: "Collections in Engineering" })).toHaveAttribute("aria-expanded", "true");
    expect(saved()).toEqual(["col-eng"]);
  });

  it("keeps the folds of a collection a cut-off list does not name", () => {
    localStorage.setItem("loom_library_expanded_v1:ann:acme", JSON.stringify(["col-past-the-cap"]));
    tree({ truncated: true });
    expect(saved()).toEqual(["col-past-the-cap"]);
  });

  it("keeps a collection the person closed closed when the list is read again", async () => {
    const { update } = tree({ namespace: null, current: "col-runbooks" });
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    update({ rows: [...ROWS] });
    expect(places()).not.toContain("Runbooks");
  });

  it("opens the new parents of the collection shown once it moved under them", () => {
    const { update } = tree({ namespace: null, current: "col-runbooks" });
    update({ rows: ROWS.map((c) => (c.namespace === "col-runbooks" ? { ...c, parent: "col-legal" } : c)) });
    const legal = screen.getByRole("link", { name: "Legal" }).closest("li") as HTMLElement;
    expect(within(legal).getByRole("link", { name: "Runbooks" })).toHaveAttribute("aria-current", "page");
  });

  it("keeps a parent the person closed closed while answers swing between old and new paths", async () => {
    const moved = ROWS.map((c) => (c.namespace === "col-runbooks" ? { ...c, parent: "col-legal" } : c));
    const { update } = tree({ namespace: null, current: "col-runbooks" });
    update({ rows: moved });
    await userEvent.click(screen.getByRole("button", { name: "Collections in Legal" }));
    // A lagging replica answers with the old path, then the new one again.
    update({ rows: [...ROWS] });
    update({ rows: [...moved] });
    expect(screen.getByRole("button", { name: "Collections in Legal" })).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps a fold a later answer from a lagging replica does not list", async () => {
    const { update } = tree();
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    update({ rows: ROWS.filter((c) => c.namespace !== "col-eng") });
    // Saved as it was: a tab closed now opens it again next time.
    expect(saved()).toEqual(["col-eng"]);
    update({ rows: [...ROWS] });
    expect(places()).toContain("Runbooks");
    expect(saved()).toEqual(["col-eng"]);
  });

  it("forgets a collection opened here once the list no longer holds it", async () => {
    tree();
    await userEvent.click(screen.getByRole("button", { name: "Collections in Engineering" }));
    expect(saved()).toEqual(["col-eng"]);
    cleanup();
    tree({ rows: ROWS.filter((c) => c.namespace !== "col-eng") });
    expect(saved()).toEqual([]);
  });

  it("has no axe violations, with New collection's form open too", async () => {
    tree({ current: "col-ingest" });
    const nav = screen.getByRole("navigation", { name: "Library places" });
    expect(await axeViolations(nav)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    expect(screen.getByRole("textbox", { name: "New collection in Ingest" })).toBeInTheDocument();
    expect(await axeViolations(nav)).toEqual([]);
  });

  describe("a title", () => {
    afterEach(() => vi.restoreAllMocks());
    const long = "Engineering runbooks for the ingest lanes and their recovery";
    const layOut = (scroll: number, client: number) => {
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(scroll);
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(client);
    };

    it("cut is whole in the link's name and in a tooltip on focus, which a click closes", async () => {
      layOut(300, 180);
      const { onPick } = tree({ rows: [entry("col-eng", { title: long })], namespace: null });
      const link = screen.getByRole("link", { name: long });
      act(() => link.focus());
      expect(await screen.findByRole("tooltip")).toHaveTextContent(long);
      fireEvent.click(link);
      expect(onPick).toHaveBeenCalledWith("col-eng", placeFocusKey("col-eng"), long);
      await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
    });

    it("that fits gets no tooltip", async () => {
      layOut(180, 180);
      tree({ rows: [entry("col-eng", { title: long })], namespace: null });
      const link = screen.getByRole("link", { name: long });
      act(() => link.focus());
      await act(async () => {});
      expect(link).toHaveAttribute("data-state", "closed");
      expect(screen.queryByRole("tooltip")).toBeNull();
    });
  });
});

describe("LibraryTree's form", () => {
  it("names its target as the tree tells two collections of one title apart", async () => {
    const rows = [entry("col-a", { title: "Notes" }), entry("col-b", { title: "Notes" })];
    tree({ rows, repeated: repeatedTitles(rows), namespace: null, current: "col-b" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    expect(screen.getByRole("textbox", { name: "New collection in Notes (col-b)" })).toHaveFocus();
  });

  it("hands the page an answer that lands after the tree is gone, with the place version it started at", async () => {
    let answer: (v: unknown) => void = () => {};
    api.createCollection.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const { onCreated } = tree({ current: "col-eng" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    await userEvent.type(screen.getByRole("textbox", { name: "New collection in Engineering" }), "Q4");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    cleanup();
    await act(async () => answer({ namespace: "col-q4", parent: "col-eng", created: true, title: "Q4" }));
    // The page decides: a resize still opens it, a move to another place does not.
    expect(onCreated).toHaveBeenCalledWith("col-q4", 7, NEW_COLLECTION_FOCUS);
    expect(toast.toast).toHaveBeenCalledWith("Created Q4", "ok");
  });

  it("creates and moves on under StrictMode, which mounts the tree twice", async () => {
    api.createCollection.mockResolvedValue({ namespace: "col-q4", parent: "col-eng", created: true, title: "Q4" });
    const { onCreated } = tree({ current: "col-eng" }, { wrapper: StrictMode });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    await userEvent.type(screen.getByRole("textbox", { name: "New collection in Engineering" }), "Q4");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("col-q4", 7, NEW_COLLECTION_FOCUS));
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});

describe("LibraryTree's drops", () => {
  // Engineering holds Runbooks, which holds Ingest; the person administers all but Ingest.
  const NEST = [
    entry("col-me-1a2b", { personal: true }),
    entry("col-eng", { title: "Engineering" }),
    entry("col-ops", { title: "Ops" }),
    entry("col-runbooks", { parent: "col-eng", title: "Runbooks" }),
    entry("col-ingest", { parent: "col-runbooks", title: "Ingest", role: "writer" }),
    entry("col-legal", { title: "Legal", role: "viewer" }),
  ];
  const dataTransfer = () => ({ setData: vi.fn(), clearData: vi.fn(), effectAllowed: "", dropEffect: "" });
  /** The tree row of the place this link names. */
  const row = (name: string) => screen.getByRole("link", { name }).closest(".library-tree-row") as HTMLElement;
  /** Each listed place's drop state, by its name. */
  const drops = () =>
    Object.fromEntries(
      screen
        .queryAllByRole("link")
        .map((a) => [a.textContent, a.closest(".library-tree-row")?.getAttribute("data-drop")]),
    );

  /** The tree on Ingest (its parents open), with `name`'s row picked up. */
  async function hold(name: string) {
    tree({ rows: NEST, current: "col-ingest" });
    const dt = dataTransfer();
    fireEvent.dragStart(row(name), { dataTransfer: dt });
    await waitFor(() => expect(row("Ops")).toHaveAttribute("data-drop", "ok"));
    return dt;
  }

  it("takes a collection into one the person writes, never into itself, under itself or where it is", async () => {
    await hold("Runbooks");
    expect(drops()).toEqual({
      "All documents": null,
      "Namespace bench": null,
      "My files": null,
      Engineering: null, // already its parent
      Runbooks: null,
      Ingest: null, // under it
      Ops: "ok",
      Legal: null, // read only
    });
    expect(fireEvent.dragOver(row("Ingest"), { dataTransfer: dataTransfer() })).toBe(true);
    fireEvent.drop(row("Ingest"), { dataTransfer: dataTransfer() });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens Move set to the collection dropped on, and moves it there with collection.move", async () => {
    api.moveCollection.mockResolvedValue({ namespace: "col-runbooks", parent: "col-ops", moved: true });
    const dt = await hold("Runbooks");
    expect(fireEvent.dragOver(row("Ops"), { dataTransfer: dt })).toBe(false);
    expect(row("Ops")).toHaveAttribute("data-drop", "over");
    fireEvent.drop(row("Ops"), { dataTransfer: dt });
    const dialog = await screen.findByRole("dialog", { name: "Move Runbooks" });
    expect(within(dialog).getByRole("button", { name: /Inside/ })).toHaveTextContent("Ops");
    // The drop is over: no place stays marked.
    expect(Object.values(drops()).filter(Boolean)).toEqual([]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() => expect(api.moveCollection).toHaveBeenCalledWith("acme", "col-runbooks", "col-ops"));
    expect(toast.toast).toHaveBeenCalledWith("Moved Runbooks into Ops", "ok");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // Focus goes to the place it was dropped on, which the move leaves where it was.
    await waitFor(() => expect(screen.getByRole("link", { name: "Ops" })).toHaveFocus());
  });

  it("offers the top level to a collection that sits inside another, and none to one already there", async () => {
    api.moveCollection.mockResolvedValue({ namespace: "col-runbooks", parent: "", moved: true });
    const dt = await hold("Runbooks");
    const top = screen.getByText("Drop here for the top level");
    expect(top).toHaveAttribute("data-drop", "ok");
    fireEvent.dragOver(top, { dataTransfer: dt });
    fireEvent.drop(top, { dataTransfer: dt });
    const dialog = await screen.findByRole("dialog", { name: "Move Runbooks" });
    expect(within(dialog).getByRole("button", { name: /Inside/ })).toHaveTextContent("Top level");
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() => expect(api.moveCollection).toHaveBeenCalledWith("acme", "col-runbooks", ""));
    await waitFor(() => expect(screen.getByRole("button", { name: "New collection" })).toHaveFocus());
    cleanup();
    await hold("Engineering");
    expect(screen.queryByText("Drop here for the top level")).toBeNull();
  });

  it("holds nothing when the drag ends before the tick that holds it", async () => {
    tree({ rows: NEST, current: "col-ingest" });
    const dt = dataTransfer();
    fireEvent.dragStart(row("Runbooks"), { dataTransfer: dt });
    fireEvent.dragEnd(row("Runbooks"), { dataTransfer: dt });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(Object.values(drops()).filter(Boolean)).toEqual([]);
    expect(screen.queryByText("Drop here for the top level")).toBeNull();
  });

  it("carries Loom's own drag type, never text a field would take", async () => {
    const dt = await hold("Runbooks");
    // The link's own address and text are cleared first.
    expect(dt.clearData).toHaveBeenCalled();
    expect(dt.setData).toHaveBeenCalledWith(DRAG_TYPE, "move");
    expect(dt.setData).not.toHaveBeenCalledWith("text/plain", expect.anything());
  });

  it("leaves the focus where the Modal hands it when Move is closed without moving", async () => {
    const dt = await hold("Runbooks");
    // A press on the row's link focuses it before the drag starts.
    const dragged = screen.getByRole("link", { name: "Runbooks" });
    dragged.focus();
    fireEvent.dragOver(row("Ops"), { dataTransfer: dt });
    fireEvent.drop(row("Ops"), { dataTransfer: dt });
    const dialog = await screen.findByRole("dialog", { name: "Move Runbooks" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.moveCollection).not.toHaveBeenCalled();
    await waitFor(() => expect(dragged).toHaveFocus());
  });

  it("hands focus back to New collection's open form after a move to the top level", async () => {
    api.moveCollection.mockResolvedValue({ namespace: "col-runbooks", parent: "", moved: true });
    const { update } = tree({ rows: NEST, current: "col-ingest" });
    await userEvent.click(screen.getByRole("button", { name: "New collection" }));
    const field = screen.getByRole("textbox", { name: /^New collection/ });
    // A press on the row's link focuses it before the drag starts.
    screen.getByRole("link", { name: "Runbooks" }).focus();
    const dt = dataTransfer();
    fireEvent.dragStart(row("Runbooks"), { dataTransfer: dt });
    const top = await screen.findByText("Drop here for the top level");
    fireEvent.dragOver(top, { dataTransfer: dt });
    fireEvent.drop(top, { dataTransfer: dt });
    const dialog = await screen.findByRole("dialog", { name: "Move Runbooks" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    await waitFor(() => expect(api.moveCollection).toHaveBeenCalledWith("acme", "col-runbooks", ""));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // The list read again draws Runbooks at the top: its old row is gone.
    update({ rows: NEST.map((c) => (c.namespace === "col-runbooks" ? { ...c, parent: "" } : c)) });
    await waitFor(() => expect(field).toHaveFocus());
  });

  it("drags only a collection the person administers, and not their own files", () => {
    tree({ rows: NEST, current: "col-ingest" });
    expect(row("Runbooks")).toHaveAttribute("draggable", "true");
    expect(row("Ingest")).not.toHaveAttribute("draggable");
    expect(row("My files")).not.toHaveAttribute("draggable");
    expect(row("All documents")).not.toHaveAttribute("draggable");
  });

  it("keeps the dialog open with what to do when the move fails", async () => {
    api.moveCollection.mockRejectedValue(new Error("network down"));
    const dt = await hold("Runbooks");
    fireEvent.dragOver(row("Ops"), { dataTransfer: dt });
    fireEvent.drop(row("Ops"), { dataTransfer: dt });
    const dialog = await screen.findByRole("dialog", { name: "Move Runbooks" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Move" }));
    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    expect(toast.toast).not.toHaveBeenCalled();
  });

  it("marks only the places the page says take the documents held, and hands it the drop", () => {
    const onDrop = vi.fn();
    const { update } = tree({ rows: NEST, current: "col-ingest" });
    expect(Object.values(drops()).filter(Boolean)).toEqual([]);
    update({ documentDrop: { targets: new Set(["bench", "col-ops"]), onDrop } });
    expect(drops()).toMatchObject({ "Namespace bench": "ok", Ops: "ok", "My files": null, Ingest: null });
    expect(screen.queryByText("Drop here for the top level")).toBeNull();
    fireEvent.drop(row("My files"), { dataTransfer: dataTransfer() });
    expect(onDrop).not.toHaveBeenCalled();
    fireEvent.dragOver(row("Namespace bench"), { dataTransfer: dataTransfer() });
    expect(row("Namespace bench")).toHaveAttribute("data-drop", "over");
    fireEvent.drop(row("Namespace bench"), { dataTransfer: dataTransfer() });
    expect(onDrop).toHaveBeenCalledWith("bench");
    update({ documentDrop: null });
    expect(Object.values(drops()).filter(Boolean)).toEqual([]);
  });
});
