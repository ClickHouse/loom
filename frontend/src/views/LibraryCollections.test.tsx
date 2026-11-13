// The Library shows the header's namespace or one of the collections the caller reads: a
// tree with each one's description, a menu that shares or deletes it, and New collection.
import { useState, type ComponentProps } from "react";
import { repeatedTitles } from "../lib/collections";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { PHONE_QUERY } from "../lib/useMedia";
import { TREE_PAGE_MIN } from "../lib/usePageRoom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionEntry } from "../api";
import { useStore } from "../lib/state";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { arriveAt } from "../lib/place";
import { NEW_COLLECTION_FOCUS, placeTree } from "./LibraryTree";
import { axeViolations } from "../test/axe";
import { Documents } from "./Documents";
import {
  LibraryCollections,
  collectionLabel,
  treeOrder,
  useReadableCollections,
  type ReadableCollections,
} from "./LibraryCollections";

const api = vi.hoisted(() => ({
  listCollections: vi.fn(),
  deleteCollection: vi.fn(),
  createCollection: vi.fn(),
  listKnowledge: vi.fn(),
  getKnowledge: vi.fn(),
  fetchKnowledgePreview: vi.fn(),
  knowledgeFormats: vi.fn(),
  uploadKnowledgeFile: vi.fn(),
  ingestKnowledge: vi.fn(),
  knowledgeIngestStatus: vi.fn(),
  deleteKnowledge: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const confirm = vi.hoisted(() => ({ next: false }));
vi.mock("../components/ConfirmDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/ConfirmDialog")>()),
  confirmAction: async () => confirm.next,
}));
const toast = vi.hoisted(() => vi.fn());
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast,
}));

const entry = (namespace: string, over: Partial<CollectionEntry> = {}): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "admin",
  description: "",
  ...over,
});

const ROWS = [
  entry("col-plans", { parent: "col-q3", role: "viewer" }),
  entry("col-q3", { description: "Quarter plans" }),
  entry("col-me-1a2b", { personal: true }),
];

const read = (rows: CollectionEntry[] | null, over: Partial<ReadableCollections> = {}): ReadableCollections => ({
  rows,
  truncated: false,
  kept: rows && { rows, truncated: false },
  refreshing: false,
  error: null,
  reload: vi.fn(),
  expected: "",
  ...over,
});

/** A window where only `query` matches. */
function matchOnly(query: string) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: q === query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
}

describe("collection names and order", () => {
  it("shows a title in place of the name, and both where two collections share the title", () => {
    const titled = entry("col-q3-plans", { title: "Q3 Plans", description: "Quarter plans" });
    expect(collectionLabel(titled)).toBe("Q3 Plans · Quarter plans");
    const rows = [entry("col-notes", { title: "Notes" }), entry("col-notes-ab12", { title: "notes" })];
    const repeated = repeatedTitles(rows);
    expect(rows.map((c) => collectionLabel(c, repeated))).toEqual(["Notes (col-notes)", "notes (col-notes-ab12)"]);
  });

  it("takes two titles that differ only by a character drawn as nothing for one, showing their addresses", () => {
    const rows = [entry("col-fin", { title: "Finance" }), entry("col-fin-ab12", { title: "Finance\ufe0f" })];
    expect(rows.map((c) => collectionLabel(c, repeatedTitles(rows)))).toEqual([
      "Finance (col-fin)",
      "Finance\ufe0f (col-fin-ab12)",
    ]);
  });

  it("shows the addresses of titles that look alike but are two (a joiner between letters)", () => {
    const rows = [entry("col-pay", { title: "Payroll" }), entry("col-pay-ab12", { title: "Pay\u200droll" })];
    expect(rows.map((c) => collectionLabel(c, repeatedTitles(rows)))).toEqual([
      "Payroll (col-pay)",
      "Pay\u200droll (col-pay-ab12)",
    ]);
  });

  it("orders siblings by what they show", () => {
    const rows = [entry("col-a", { title: "Zeta" }), entry("col-z", { title: "Alpha" })];
    expect(treeOrder(rows).map(({ c }) => c.namespace)).toEqual(["col-z", "col-a"]);
  });

  it("says what a collection holds beside its name, and the user's own is My files", () => {
    expect(collectionLabel(entry("col-q3", { description: "Quarter plans" }))).toBe("col-q3 · Quarter plans");
    expect(collectionLabel(entry("col-bare"))).toBe("col-bare");
    expect(collectionLabel(entry("col-me-1a2b", { personal: true }))).toBe("My files");
    // A long description is cut; the name never is.
    const long = collectionLabel(entry("col-long", { description: "x".repeat(500) }));
    expect(long).toBe(`col-long · ${"x".repeat(79)}…`);
    // Cut by character: an emoji at the cut stays whole.
    const emoji = collectionLabel(entry("col-e", { description: `${"x".repeat(78)}📘${"y".repeat(10)}` }));
    expect(emoji).toBe(`col-e · ${"x".repeat(78)}📘…`);
    const family = "👨‍👩‍👧";
    const joined = collectionLabel(entry("col-f", { description: `${"x".repeat(78)}${family}${"y".repeat(10)}` }));
    expect(joined).toBe(`col-f · ${"x".repeat(78)}${family}…`);
  });

  it("puts each parent before its children, the user's own first, and an unreadable parent's child at the top", () => {
    const rows = [...ROWS, entry("col-orphan", { parent: "col-hidden" })];
    expect(treeOrder(rows).map(({ c, depth }) => [c.namespace, depth])).toEqual([
      ["col-me-1a2b", 0],
      ["col-orphan", 0],
      ["col-q3", 0],
      ["col-plans", 1],
    ]);
  });

  it("keeps collections a cycle in the tree cuts off, starting it where the tree does", () => {
    const rows = [entry("col-a", { parent: "col-b" }), entry("col-b", { parent: "col-a" })];
    expect(treeOrder(rows).map(({ c, depth }) => [c.namespace, depth])).toEqual([
      ["col-b", 0],
      ["col-a", 1],
    ]);
    const tree = placeTree(rows);
    expect(tree.mine.map((c) => c.namespace)).toEqual(["col-b"]);
    expect(tree.children.get("col-b")?.map((c) => c.namespace)).toEqual(["col-a"]);
  });
});

/** The picker as Documents mounts it: the New collection dialog's open state is the page's. */
function Picker(
  props: Omit<
    ComponentProps<typeof LibraryCollections>,
    "creating" | "onCreatingChange" | "onCreated" | "repeated" | "byName"
  >,
) {
  const [creating, setCreating] = useState(false);
  const rows = props.collections.rows ?? [];
  return (
    <LibraryCollections
      {...props}
      repeated={repeatedTitles(rows)}
      byName={new Map(rows.map((c) => [c.namespace, c]))}
      creating={creating}
      onCreatingChange={setCreating}
      onCreated={vi.fn()}
    />
  );
}

describe("LibraryCollections", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("names the namespace when no collection is shown, and passes axe", async () => {
    const { container } = render(
      <Picker org="acme" namespace="team" collections={read(ROWS)} current={null} onPick={() => undefined} />,
    );
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("Namespace team");
    expect(screen.queryByRole("button", { name: /Actions for/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New collection" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows the collection shown with its role, and its menu for an admin", () => {
    render(<Picker org="acme" namespace="team" collections={read(ROWS)} current={ROWS[1]} onPick={() => undefined} />);
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-q3 · Quarter plans");
    expect(screen.getByText("Admin")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for col-q3" })).toBeInTheDocument();
  });

  it("gives a viewer Members and no Delete", async () => {
    render(<Picker org="acme" namespace="team" collections={read(ROWS)} current={ROWS[0]} onPick={() => undefined} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for col-plans" }), { button: 0, ctrlKey: false });
    await settle();
    expect(screen.getByRole("menuitem", { name: "Members" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete collection" })).not.toBeInTheDocument();
    // What the place is, and the role, behind the info button beside the select: a press
    // (a tap on a phone) opens it.
    await userEvent.click(screen.getByRole("button", { name: "About this place" }));
    expect(await screen.findByText(/^Viewer of a collection: .*everyone who reaches col-q3/)).toBeInTheDocument();
  });

  it("closes Members when another collection is shown, so a grant never lands on it", async () => {
    const view = (current: CollectionEntry) => (
      <Picker
        org="acme"
        namespace="team"
        collections={read(ROWS)}
        current={current}
        place={current.namespace}
        onPick={() => undefined}
      />
    );
    const { rerender } = render(view(ROWS[1]));
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for col-q3" }), { button: 0, ctrlKey: false });
    await settle();
    fireEvent.click(screen.getByRole("menuitem", { name: "Members" }));
    await settle();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    rerender(view(ROWS[0]));
    await settle();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Back to the first does not open it again by itself.
    rerender(view(ROWS[1]));
    await settle();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("on a phone shows the path as one back button and leaves the collections inside to the select", () => {
    matchOnly(PHONE_QUERY);
    const rows = [
      ROWS[1],
      entry("col-q3-k", { parent: "col-q3", title: "Kid" }),
      entry("col-q3-k-1", { parent: "col-q3-k" }),
    ];
    render(<Picker org="acme" namespace="team" collections={read(rows)} current={rows[2]} onPick={() => undefined} />);
    expect(screen.getByRole("button", { name: "Back to Kid" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Collection path" })).toBeNull();
  });

  it("gives the user's own collection no menu", () => {
    render(<Picker org="acme" namespace="team" collections={read(ROWS)} current={ROWS[2]} onPick={() => undefined} />);
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("My files");
    expect(screen.queryByRole("button", { name: /Actions for/ })).not.toBeInTheDocument();
  });

  it("says when the collections cannot be read, and reads them again on request", () => {
    const collections = read(null, { error: new Error("offline") });
    render(<Picker org="acme" namespace="team" collections={collections} current={null} onPick={() => undefined} />);
    expect(screen.getByText(/Couldn't load collections/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load collections again" }));
    expect(collections.reload).toHaveBeenCalled();
  });
});

describe("the path's place above the top", () => {
  it("is the header's namespace, else My files, else none", () => {
    const onPick = vi.fn();
    const at = (namespace: string | null, rows: CollectionEntry[], current: CollectionEntry) =>
      render(<Picker org="acme" namespace={namespace} collections={read(rows)} current={current} onPick={onPick} />);
    at("team", ROWS, ROWS[1]);
    const namespaceLink = within(screen.getByRole("navigation", { name: "Collection path" })).getByRole("link", {
      name: "Namespace team",
    });
    // Its address names no collection, so a new tab opens the namespace, not the one left.
    expect(namespaceLink.getAttribute("href")).not.toMatch(/collection=/);
    fireEvent.click(namespaceLink, { ctrlKey: true });
    expect(onPick).not.toHaveBeenCalled();
    fireEvent.click(namespaceLink);
    expect(onPick).toHaveBeenLastCalledWith(null);
    cleanup();
    // No namespace in the header: the user's own files are the place above.
    at(null, ROWS, ROWS[0]);
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Collection path" })).getByRole("link", { name: "My files" }),
    );
    expect(onPick).toHaveBeenLastCalledWith("col-me-1a2b");
    cleanup();
    at(null, [ROWS[1]], ROWS[1]);
    expect(screen.queryByRole("navigation", { name: "Collection path" })).toBeNull();
  });
});

describe("useReadableCollections", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  function Probe() {
    const c = useReadableCollections("acme");
    return <p data-testid="probe">{c.rows === null ? "reading" : c.rows.map((r) => r.namespace).join(",")}</p>;
  }

  it("asks again for a collection just created until the list names it", async () => {
    let probe: ReadableCollections | null = null;
    function Holder() {
      probe = useReadableCollections("acme");
      return <p data-testid="names">{probe.rows?.map((r) => r.namespace).join(",") ?? "reading"}</p>;
    }
    api.listCollections.mockResolvedValue({ collections: [entry("col-a")], next_cursor: null });
    render(<Holder />);
    await settle();
    // A replica that has not caught up answers once without it.
    api.listCollections
      .mockResolvedValueOnce({ collections: [entry("col-a")], next_cursor: null })
      .mockResolvedValue({ collections: [entry("col-a"), entry("col-new")], next_cursor: null });
    await act(async () => probe?.reload("col-new"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1200));
    });
    await settle();
    expect(screen.getByTestId("names")).toHaveTextContent("col-a,col-new");
    // The page tells "just created, not listed yet" apart from "not yours".
    expect((probe as ReadableCollections | null)?.expected).toBe("col-new");
  });

  it("lists a collection two pages both return once", async () => {
    api.listCollections
      .mockResolvedValueOnce({ collections: [entry("col-a"), entry("col-b")], next_cursor: "col-b" })
      .mockResolvedValueOnce({ collections: [entry("col-b"), entry("col-c")], next_cursor: null });
    render(<Probe />);
    await settle();
    expect(screen.getByTestId("probe")).toHaveTextContent("col-a,col-b,col-c");
  });

  it("keeps the last answer while the list is read again", async () => {
    let probe: ReadableCollections | null = null;
    function Holder() {
      probe = useReadableCollections("acme");
      return null;
    }
    api.listCollections.mockResolvedValue({ collections: [entry("col-a")], next_cursor: null });
    render(<Holder />);
    await settle();
    api.listCollections.mockReturnValue(new Promise(() => {}));
    act(() => probe?.reload());
    const now = probe as ReadableCollections | null;
    expect(now?.rows).toBeNull();
    expect(now?.kept?.rows.map((c) => c.namespace)).toEqual(["col-a"]);
  });

  it("reads every page", async () => {
    api.listCollections
      .mockResolvedValueOnce({ collections: [entry("col-a")], next_cursor: "col-a" })
      .mockResolvedValueOnce({ collections: [entry("col-b")], next_cursor: null });
    render(<Probe />);
    await settle();
    expect(screen.getByTestId("probe")).toHaveTextContent("col-a,col-b");
    expect(api.listCollections.mock.calls[1][1]).toMatchObject({ cursor: "col-a" });
  });
});

/** The <main> App draws, the page's container: made when missing. */
function inMain(): HTMLElement {
  const found = document.getElementById("main");
  if (found) return found;
  const main = Object.assign(document.createElement("main"), { id: "main" });
  document.body.append(main);
  return main;
}

/** <main>, measured `width` px wide (room for the tree by default), where the page looks for
 *  it; the function it returns narrows it. */
function resizable(width = TREE_PAGE_MIN + 64) {
  const observers: Array<() => void> = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(f: () => void) {
        observers.push(f);
      }
      observe() {}
      disconnect() {}
    },
  );
  const rect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.id === "main" ? ({ width, height: 800 } as DOMRect) : rect.call(this);
  });
  inMain();
  return () => {
    width = TREE_PAGE_MIN - 136;
    for (const f of observers) f();
  };
}

async function startCreate() {
  let answer: (v: unknown) => void = () => {};
  api.createCollection.mockReturnValue(new Promise((resolve) => (answer = resolve)));
  api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
  history.replaceState(null, "", "/library?collection=col-q3");
  // In <main>, as App draws it: an arrival focuses the page's h1 there.
  render(<Documents />, { container: inMain() });
  const tree = await screen.findByRole("navigation", { name: "Library places" });
  await within(tree).findByRole("link", { name: "col-q3" });
  fireEvent.click(within(tree).getByRole("button", { name: "New collection" }));
  fireEvent.change(screen.getByRole("textbox", { name: /^New collection in/ }), { target: { value: "Q4" } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  return (r: unknown) => act(async () => answer(r));
}
const q4 = { namespace: "col-q4", parent: "col-q3", created: true, title: "Q4" };

describe("the Library on a collection", () => {
  beforeEach(() => {
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    api.fetchKnowledgePreview.mockResolvedValue(null);
    useStore.setState({
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "knowledge",
      pendingDocSourceUri: null,
    });
  });
  afterEach(() => {
    cleanup();
    document.getElementById("main")?.remove();
    useStore.setState({ myNamespaces: null });
    localStorage.clear();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    history.replaceState(null, "", "/");
  });

  it("lists the top level's collections below the documents, and a collection's as chips only", async () => {
    const kids = (parent: string) =>
      Array.from({ length: 10 }, (_, i) => entry(`${parent}-k${i}`, { parent, title: `${parent} kid ${i}` }));
    api.listCollections.mockResolvedValue({
      collections: [ROWS[1], ...kids("col-q3"), ...kids("col-q3-k0")],
      next_cursor: null,
    });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    history.replaceState(null, "", "/library");
    const { container } = render(<Documents />);
    const section = await screen.findByRole("region", { name: /Collections/ });
    // After the documents: they start where the list rules want them.
    await waitFor(() => expect(container.querySelector("[data-doc-row]")).not.toBeNull());
    const row = container.querySelector("[data-doc-row]") as Element;
    expect(row.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(section).getByRole("link", { name: "col-q3" }));
    await settle();
    // Inside a collection the chips on its path's line list what is inside, once.
    expect(screen.queryByRole("region", { name: /Collections/ })).toBeNull();
    // The first six, then Show more for the rest, with the focus on the first it adds.
    const chips = screen.getByRole("navigation", { name: "Collections in col-q3" });
    expect(within(chips).getAllByRole("link", { name: /^col-q3 kid / })).toHaveLength(6);
    fireEvent.click(within(chips).getByRole("button", { name: "Show 4 more" }));
    expect(within(chips).getAllByRole("link", { name: /^col-q3 kid / })).toHaveLength(10);
    expect(within(chips).getAllByRole("link")[6]).toHaveFocus();
    expect(within(chips).queryByRole("button")).toBeNull();
    // The next place starts at six again.
    fireEvent.click(within(chips).getByRole("link", { name: "col-q3 kid 0" }));
    await settle();
    const next = screen.getByRole("navigation", { name: "Collections in col-q3 kid 0" });
    expect(within(next).getAllByRole("link")).toHaveLength(6);
    expect(within(next).getByRole("button", { name: "Show 4 more" })).toBeInTheDocument();
  });

  it("lists no collections inside on a phone, where the select lists them", async () => {
    matchOnly(PHONE_QUERY);
    api.listCollections.mockResolvedValue({
      collections: [ROWS[1], entry("col-q3-k", { parent: "col-q3" })],
      next_cursor: null,
    });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-q3");
    expect(screen.queryByRole("region", { name: /Collections/ })).toBeNull();
  });

  it("names no place in the browser tab while a linked collection is read", async () => {
    api.listCollections.mockReturnValue(new Promise(() => {}));
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    expect(document.title).toBe("Library · Loom");
  });

  it("reads a linked collection only once collection.list names it", async () => {
    let answer: (v: unknown) => void = () => undefined;
    api.listCollections.mockReturnValue(
      new Promise((r) => {
        answer = r;
      }),
    );
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).not.toHaveBeenCalled();
    await act(async () => answer({ collections: ROWS, next_cursor: null }));
    await settle();
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-q3" });
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-q3");
  });

  it("shows the namespace for a linked collection the caller cannot read, or when the list fails", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    history.replaceState(null, "", "/library?collection=col-secret");
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).toHaveBeenCalled();
    expect(api.listKnowledge.mock.calls.every(([id]) => id.namespace === "team")).toBe(true);
    expect(new URLSearchParams(location.search).get("collection")).toBeNull();
    cleanup();
    vi.clearAllMocks();
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    api.listCollections.mockRejectedValue(new Error("offline"));
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).toHaveBeenCalled();
    expect(api.listKnowledge.mock.calls.every(([id]) => id.namespace === "team")).toBe(true);
    // The link is kept for "Load collections again"; only a list without it drops it.
    expect(new URLSearchParams(location.search).get("collection")).toBe("col-q3");
    expect(screen.getByText(/Couldn't load collections/)).toBeInTheDocument();
  });

  const folderDrop = (name: string, files: string[]) => ({
    dataTransfer: {
      items: [
        {
          kind: "file",
          getAsFile: () => null,
          webkitGetAsEntry: () => ({
            isFile: false,
            isDirectory: true,
            name,
            createReader: () => {
              const left = [
                files.map((n) => ({
                  isFile: true,
                  isDirectory: false,
                  name: n,
                  file: (ok: (f: File) => void) => ok(new File([n], n)),
                })),
                [],
              ];
              return { readEntries: (ok: (es: unknown[]) => void) => ok(left.shift() ?? []) };
            },
          }),
        },
      ],
      files: [],
    },
  });
  const MARKDOWN = {
    formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
    max_upload_bytes: 1_000_000,
    refused_extensions: [],
    default_format: "text",
  };

  it("lists the collections a folder upload made once its dialog closes", async () => {
    const q4 = entry("col-q4", { parent: "col-q3", title: "Q4" });
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.knowledgeFormats.mockResolvedValue(MARKDOWN);
    api.createCollection.mockImplementation(async () => {
      api.listCollections.mockResolvedValue({ collections: [...ROWS, q4], next_cursor: null });
      return { namespace: "col-q4", parent: "col-q3", created: true, title: "Q4" };
    });
    api.uploadKnowledgeFile.mockImplementation(async (_id: unknown, f: File) => ({ upload_id: f.name }));
    api.ingestKnowledge.mockImplementation(async (_id: unknown, o: { source_uri: string }) => ({
      ingestion_id: o.source_uri,
    }));
    api.knowledgeIngestStatus.mockImplementation(async (_id: unknown, job: string) => ({
      found: true,
      ingestion_id: job,
      source_uri: job,
      state: "ready",
      revision: 1,
    }));
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add document" }));
    fireEvent.drop(screen.getByTestId("add-doc-drop"), folderDrop("Q4", ["a.md"]));
    const upload = await screen.findByRole("button", { name: "Upload 1 file to col-q3" });
    await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
    await user.click(upload);
    const reads = api.listCollections.mock.calls.length;
    await user.click(await screen.findByRole("button", { name: "Done" }));
    await waitFor(() => expect(api.listCollections.mock.calls.length).toBeGreaterThan(reads));
    expect(
      await within(await screen.findByRole("navigation", { name: "Collections in col-q3" })).findByRole("link", {
        name: "Q4",
      }),
    ).toBeInTheDocument();
  });

  it("keeps the documents and the dialog's place while a folder upload reads the collections again", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    api.knowledgeFormats.mockResolvedValue(MARKDOWN);
    api.createCollection.mockImplementation(async () => {
      // The read the upload asks for after it made its collection stays in flight.
      api.listCollections.mockReturnValue(new Promise(() => {}));
      return { namespace: "col-q4", parent: "col-q3", created: true, title: "Q4" };
    });
    api.uploadKnowledgeFile.mockReturnValue(new Promise(() => {}));
    history.replaceState(null, "", "/library?collection=col-q3");
    const { container } = render(<Documents />);
    await settle();
    const docReads = api.listKnowledge.mock.calls.length;
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add document" }));
    fireEvent.drop(screen.getByTestId("add-doc-drop"), folderDrop("Q4", ["a.md"]));
    const upload = await screen.findByRole("button", { name: "Upload 1 file to col-q3" });
    await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
    const listReads = api.listCollections.mock.calls.length;
    await user.click(upload);
    await waitFor(() => expect(api.listCollections.mock.calls.length).toBeGreaterThan(listReads));
    await settle();
    expect(screen.getByRole("dialog", { name: /col-q3/ })).toBeInTheDocument();
    expect(container.querySelector("[data-doc-row]")).not.toBeNull();
    expect(api.listKnowledge.mock.calls.length).toBe(docReads);
    expect(new URLSearchParams(location.search).get("collection")).toBe("col-q3");
  });

  it("keeps the documents when the dialog of a folder upload into a collection closes", async () => {
    const q4 = entry("col-q4", { parent: "col-q3", title: "Q4" });
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    api.knowledgeFormats.mockResolvedValue(MARKDOWN);
    api.createCollection.mockImplementation(async () => {
      api.listCollections.mockResolvedValue({ collections: [...ROWS, q4], next_cursor: null });
      return { namespace: "col-q4", parent: "col-q3", created: true, title: "Q4" };
    });
    api.uploadKnowledgeFile.mockImplementation(async (_id: unknown, f: File) => ({ upload_id: f.name }));
    api.ingestKnowledge.mockImplementation(async (_id: unknown, o: { source_uri: string }) => ({
      ingestion_id: o.source_uri,
    }));
    api.knowledgeIngestStatus.mockImplementation(async (_id: unknown, job: string) => ({
      found: true,
      ingestion_id: job,
      source_uri: job,
      state: "ready",
      revision: 1,
    }));
    history.replaceState(null, "", "/library?collection=col-q3");
    const { container } = render(<Documents />);
    await settle();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add document" }));
    fireEvent.drop(screen.getByTestId("add-doc-drop"), folderDrop("Q4", ["b.md"]));
    const upload = await screen.findByRole("button", { name: "Upload 1 file to col-q3" });
    await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
    await user.click(upload);
    const done = await screen.findByRole("button", { name: "Done" });
    await settle();
    const docReads = api.listKnowledge.mock.calls.length;
    await user.click(done);
    await settle();
    expect(container.querySelector("[data-doc-row]")).not.toBeNull();
    expect(api.listKnowledge.mock.calls.length).toBe(docReads);
    expect(new URLSearchParams(location.search).get("collection")).toBe("col-q3");
  });

  it("waits on close for the collection a folder upload made, keeping the documents", async () => {
    const q4 = entry("col-q4", { parent: "col-q3", title: "Q4" });
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    api.knowledgeFormats.mockResolvedValue(MARKDOWN);
    api.createCollection.mockResolvedValue({ namespace: "col-q4", parent: "col-q3", created: true, title: "Q4" });
    api.uploadKnowledgeFile.mockImplementation(async (_id: unknown, f: File) => ({ upload_id: f.name }));
    api.ingestKnowledge.mockImplementation(async (_id: unknown, o: { source_uri: string }) => ({
      ingestion_id: o.source_uri,
    }));
    api.knowledgeIngestStatus.mockImplementation(async (_id: unknown, job: string) => ({
      found: true,
      ingestion_id: job,
      source_uri: job,
      state: "ready",
      revision: 1,
    }));
    history.replaceState(null, "", "/library?collection=col-q3");
    const { container } = render(<Documents />);
    await settle();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add document" }));
    fireEvent.drop(screen.getByTestId("add-doc-drop"), folderDrop("Q4", ["b.md"]));
    const upload = await screen.findByRole("button", { name: "Upload 1 file to col-q3" });
    await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
    await user.click(upload);
    const done = await screen.findByRole("button", { name: "Done" });
    await settle();
    // A replica that has not caught up answers the first read without it.
    api.listCollections
      .mockResolvedValueOnce({ collections: ROWS, next_cursor: null })
      .mockResolvedValue({ collections: [...ROWS, q4], next_cursor: null });
    const docReads = api.listKnowledge.mock.calls.length;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await user.click(done);
      // Past the retry's wait.
      await act(() => vi.advanceTimersByTimeAsync(1100));
      const chips = await screen.findByRole("navigation", { name: "Collections in col-q3" });
      expect(await within(chips).findByRole("link", { name: "Q4" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
    expect(container.querySelector("[data-doc-row]")).not.toBeNull();
    expect(api.listKnowledge.mock.calls.length).toBe(docReads);
  });

  it("says on an empty collection that a folder can be uploaded there", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    expect(await screen.findByText("This collection is empty")).toBeInTheDocument();
    expect(screen.getByText(/text file, or a folder of them: the server extracts each/)).toBeInTheDocument();
  });

  it("says folders wait for the list when the Library's collections failed to load", async () => {
    // Add document waits for the role; auth off answers it at once.
    useStore.setState({ whoami: { authenticated: false } });
    api.listCollections.mockRejectedValue(new Error("offline"));
    api.knowledgeFormats.mockResolvedValue(MARKDOWN);
    render(<Documents />);
    await settle();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add document" }));
    fireEvent.drop(screen.getByTestId("add-doc-drop"), folderDrop("Q4", ["a.md"]));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "1 folder was not read (folders are taken once the Library has listed its collections",
    );
  });

  it("takes no folder until the Library has listed its collections", async () => {
    // Add document waits for the role; auth off answers it at once.
    useStore.setState({ whoami: { authenticated: false } });
    api.listCollections.mockReturnValue(new Promise(() => {}));
    api.knowledgeFormats.mockResolvedValue(MARKDOWN);
    render(<Documents />);
    await settle();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Add document" }));
    fireEvent.drop(screen.getByTestId("add-doc-drop"), folderDrop("Q4", ["a.md"]));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "1 folder was not read (folders are taken once the Library has listed its collections",
    );
  });

  it("keeps an upload going when the page moves to a namespace with no documents", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    api.knowledgeFormats.mockResolvedValue({
      formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
      max_upload_bytes: 1_000_000,
      refused_extensions: [],
      default_format: "text",
    });
    api.uploadKnowledgeFile.mockImplementation(async (_id: unknown, f: File) => ({ upload_id: f.name }));
    api.ingestKnowledge.mockImplementation(async (_id: unknown, o: { source_uri: string }) => ({
      ingestion_id: o.source_uri,
    }));
    // The first three take a while; the fourth is sent once one of them ends.
    let finish: () => void = () => {};
    const held = new Promise<void>((r) => {
      finish = r;
    });
    api.knowledgeIngestStatus.mockImplementation(async (_id: unknown, job: string) => {
      await held;
      return { found: true, ingestion_id: job, source_uri: job, state: "ready", revision: 1 };
    });
    useStore.setState({
      identity: { org: "acme", namespace: "code-repo", agent: "loom-ui" },
      myNamespaces: [{ namespace: "code-repo", kind: "code" } as never],
    });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    const user = userEvent.setup({ applyAccept: false });
    await user.click(screen.getByRole("button", { name: "Add document" }));
    await user.upload(
      screen.getByLabelText("Choose a file to upload"),
      ["a.md", "b.md", "c.md", "d.md"].map((n) => new File([n], n)),
    );
    const upload = await screen.findByRole("button", { name: "Upload 4 files to col-q3" });
    await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
    await user.click(upload);
    await waitFor(() => expect(api.ingestKnowledge).toHaveBeenCalledTimes(3));
    await act(async () => {
      history.replaceState(null, "", "/library");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await settle();
    expect(screen.getByRole("heading", { name: "Add documents to col-q3" })).toBeInTheDocument();
    await act(async () => finish());
    await waitFor(() => expect(api.ingestKnowledge).toHaveBeenCalledTimes(4));
    expect(api.ingestKnowledge.mock.calls.every(([id]) => (id as { namespace: string }).namespace === "col-q3")).toBe(
      true,
    );
  });

  it("keeps an upload's dialog open, sending where it began, when the page shows another place", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    api.knowledgeFormats.mockResolvedValue({
      formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
      max_upload_bytes: 1_000_000,
      refused_extensions: [],
      default_format: "text",
    });
    api.uploadKnowledgeFile.mockImplementation(async (_id: unknown, f: File) => ({ upload_id: f.name }));
    api.ingestKnowledge.mockImplementation(async (_id: unknown, o: { source_uri: string }) => ({
      ingestion_id: o.source_uri,
    }));
    api.knowledgeIngestStatus.mockReturnValue(new Promise(() => {})); // still processing
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    const user = userEvent.setup({ applyAccept: false });
    await user.click(screen.getByRole("button", { name: "Add document" }));
    await user.upload(screen.getByLabelText("Choose a file to upload"), [
      new File(["a"], "a.md"),
      new File(["b"], "b.md"),
    ]);
    const upload = await screen.findByRole("button", { name: "Upload 2 files to col-q3" });
    await waitFor(() => expect(upload).not.toHaveAttribute("aria-disabled"));
    await user.click(upload);
    await waitFor(() => expect(api.ingestKnowledge).toHaveBeenCalledTimes(2));
    await act(async () => {
      history.replaceState(null, "", "/library");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await settle();
    expect(screen.getByRole("heading", { name: "Add documents to col-q3" })).toBeInTheDocument();
    expect(api.ingestKnowledge.mock.calls.every(([id]) => (id as { namespace: string }).namespace === "col-q3")).toBe(
      true,
    );
  });

  it("names an upload's destination with its address when another collection shares its title", async () => {
    api.listCollections.mockResolvedValue({
      collections: [entry("col-notes", { title: "Notes" }), entry("col-notes-ab12", { title: "Notes" })],
      next_cursor: null,
    });
    api.listKnowledge.mockResolvedValue({ rows: [], next_cursor: null });
    api.knowledgeFormats.mockResolvedValue({
      formats: [{ format: "markdown", extensions: ["md"], media_types: [], available: true }],
      max_upload_bytes: 1_000_000,
      refused_extensions: [],
      default_format: "text",
    });
    history.replaceState(null, "", "/library?collection=col-notes");
    render(<Documents />);
    await settle();
    const user = userEvent.setup({ applyAccept: false });
    await user.click(screen.getByRole("button", { name: "Add document" }));
    await user.upload(screen.getByLabelText("Choose a file to upload"), [
      new File(["a"], "a.md"),
      new File(["b"], "b.md"),
    ]);
    expect(await screen.findByRole("button", { name: "Upload 2 files to Notes (col-notes)" })).toBeInTheDocument();
  });

  it("an error from another place does not send a readable collection back", async () => {
    const { ApiError } = await import("../lib/errors");
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }) => {
      if (id.namespace === "team") throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" });
      return { rows: [], next_cursor: null };
    });
    history.replaceState(null, "", "/library");
    render(<Documents />);
    await settle();
    await act(async () => {
      history.replaceState(null, "", "/library?collection=col-q3");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await settle();
    expect(api.listCollections).toHaveBeenCalledTimes(1);
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-q3" });
  });

  it("opens a document picked in the header search in the namespace, while a collection is shown", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }) => ({
      rows: [{ source_uri: "a.md", title: id.namespace === "team" ? "Team doc" : "Collection doc", tags: [] }],
      next_cursor: null,
    }));
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "a.md" });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-q3" });
    await act(async () => useStore.getState().openDocument("a.md"));
    await settle();
    expect(new URLSearchParams(location.search).get("collection")).toBeNull();
    expect(api.getKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "team" });
    expect(screen.getByRole("dialog", { name: "Team doc" })).toBeInTheDocument();
  });

  it("ignores a ?collection= that is no collection's name", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    history.replaceState(null, "", "/library?collection=Your%20session%20expired%20-%20sign%20in%20at%20evil");
    render(<Documents />);
    await settle();
    expect(screen.queryByText(/Your session expired/)).not.toBeInTheDocument();
    expect(api.listKnowledge.mock.calls.every(([id]) => id.namespace === "team")).toBe(true);
  });

  it("leaves a collection the list still names after a refusal, once, with no loop", async () => {
    const { ApiError } = await import("../lib/errors");
    // One replica has the revoke, another not yet: the list keeps naming it.
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }) => {
      if (id.namespace === "col-q3") throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" });
      return { rows: [], next_cursor: null };
    });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    await settle();
    expect(api.listCollections).toHaveBeenCalledTimes(2);
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "team" });
    expect(new URLSearchParams(location.search).get("collection")).toBeNull();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("col-q3 is not among the collections"), "info");
  });

  it("keeps a linked document for its collection while the list fails, and opens it there once it answers", async () => {
    api.listCollections
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }) => ({
      rows: [{ source_uri: "a.md", title: id.namespace === "team" ? "Team doc" : "Collection doc", tags: [] }],
      next_cursor: null,
    }));
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "a.md" });
    history.replaceState(null, "", "/library?collection=col-q3&doc=a.md");
    useStore.getState().bootView("knowledge");
    render(<Documents />);
    await settle();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(new URLSearchParams(location.search).get("doc")).toBe("a.md");
    fireEvent.click(screen.getByRole("button", { name: "Load collections again" }));
    await settle();
    await settle();
    expect(screen.getByRole("dialog", { name: "Collection doc" })).toBeInTheDocument();
    expect(api.getKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-q3" });
  });

  it("shows a viewer Add document, the row menu's writes and the preview's unavailable", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    history.replaceState(null, "", "/library?collection=col-plans");
    render(<Documents />);
    await settle();
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-plans");
    const needs = /^Needs writer on .+\. An admin of it can grant that\.$/;
    expect(screen.getByRole("button", { name: "Add document" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "Add document" })).toHaveAccessibleDescription(needs);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for A" }), { button: 0, ctrlKey: false });
    await settle();
    expect(screen.getByRole("menuitem", { name: "Preview" })).toBeInTheDocument();
    for (const write of ["Replace content", "Edit details", "Delete"]) {
      const item = screen.getByRole("menuitem", { name: write });
      expect(item).toHaveAttribute("aria-disabled", "true");
      expect(item).toHaveAccessibleDescription(needs);
    }
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit details" }));
    await settle();
    expect(api.deleteKnowledge).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Select A" })).not.toBeInTheDocument();
    await act(async () => {
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    });
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "a.md" });
    fireEvent.click(screen.getByRole("button", { name: "Preview A" }));
    await settle();
    expect(screen.getByRole("dialog", { name: "A" })).toBeInTheDocument();
    for (const name of ["Edit content", "Re-upload"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-disabled", "true");
      expect(screen.getByRole("button", { name })).toHaveAccessibleDescription(needs);
    }
  });

  it("closes a preview when Back shows another place, so nothing opened for one is written to the other", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "a.md" });
    history.replaceState(null, "", "/library");
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Preview A" }));
    await settle();
    expect(screen.getByRole("dialog", { name: "A" })).toBeInTheDocument();
    await act(async () => {
      history.replaceState(null, "", "/library?collection=col-q3");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await settle();
    expect(screen.queryByRole("dialog", { name: "A" })).not.toBeInTheDocument();
  });

  it("leaves a collection refused while shown, for the namespace", async () => {
    const { ApiError } = await import("../lib/errors");
    api.listCollections
      .mockResolvedValueOnce({ collections: ROWS, next_cursor: null })
      .mockResolvedValue({ collections: [], next_cursor: null });
    api.listKnowledge.mockImplementation(async (id: { namespace: string }) => {
      if (id.namespace === "col-q3") throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" });
      return { rows: [], next_cursor: null };
    });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    await settle();
    expect(api.listCollections).toHaveBeenCalledTimes(2);
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "team" });
  });

  it("on a code namespace lists the top-level collections under the empty page", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    useStore.setState({
      identity: { org: "acme", namespace: "code-repo", agent: "loom-ui" },
      myNamespaces: [{ namespace: "code-repo", kind: "code" } as never],
    });
    render(<Documents />);
    const section = await screen.findByRole("region", { name: /Collections/ });
    expect(within(section).getAllByRole("link").length).toBeGreaterThan(0);
  });

  it("beside the tree, on a code namespace names no place and asks only for a pick", async () => {
    resizable();
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    useStore.setState({
      identity: { org: "acme", namespace: "code-repo", agent: "loom-ui" },
      myNamespaces: [{ namespace: "code-repo", kind: "code" } as never],
    });
    render(<Documents />);
    await settle();
    const tree = screen.getByRole("navigation", { name: "Library places" });
    expect(within(tree).getByRole("link", { name: "My files" })).not.toHaveAttribute("aria-current");
    // My files is in the tree only: nothing beside the page says it is shown.
    expect(screen.getAllByText("My files").every((el) => tree.contains(el))).toBe(true);
    expect(
      screen.getByText(
        "code-repo holds no documents. Pick a collection, or a memory namespace from the namespace picker.",
      ),
    ).toBeInTheDocument();
  });

  describe("a create answered after the tree is gone", () => {
    it("still opens it when a resize took the tree away", async () => {
      const narrow = resizable();
      const answer = await startCreate();
      act(() => narrow());
      expect(screen.queryByRole("navigation", { name: "Library places" })).toBeNull();
      api.listCollections.mockResolvedValue({
        collections: [...ROWS, entry("col-q4", { parent: "col-q3", title: "Q4" })],
        next_cursor: null,
      });
      const reads = api.listCollections.mock.calls.length;
      await answer(q4);
      await settle();
      expect(api.listCollections.mock.calls.length).toBeGreaterThan(reads);
      expect(new URLSearchParams(location.search).get("collection")).toBe("col-q4");
      // The move asks for New collection, which went with the tree: the arrival App makes
      // then focuses the page's h1, as for any other move.
      expect(useStore.getState().arriveFocus).toBe(NEW_COLLECTION_FOCUS);
      act(() => arriveAt(null, useStore.getState().arriveFocus));
      await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "Library" })).toHaveFocus());
    });

    it("moves nothing once the person left the Library and came back, and lists what was made", async () => {
      resizable();
      const answer = await startCreate();
      const at = location.search;
      // App mounts a view afresh on each visit: leaving closes this page, coming back opens another.
      cleanup();
      render(<Documents />, { container: inMain() });
      await settle();
      api.listCollections.mockResolvedValue({
        collections: [...ROWS, entry("col-q4", { parent: "col-q3", title: "Q4" })],
        next_cursor: null,
      });
      const reads = api.listCollections.mock.calls.length;
      await answer(q4);
      await settle();
      expect(location.search).toBe(at);
      // The page shown reads the list again once, and lists the new collection.
      expect(api.listCollections.mock.calls.length).toBe(reads + 1);
      const tree = screen.getByRole("navigation", { name: "Library places" });
      expect(within(tree).getByRole("link", { name: "Q4" })).toBeInTheDocument();
      expect(toast).not.toHaveBeenCalledWith(expect.stringMatching(/col-q4/), "info");
    });

    it("moves nothing once the page shows another org", async () => {
      resizable();
      const answer = await startCreate();
      api.listCollections.mockReturnValue(new Promise(() => {}));
      act(() => useStore.setState({ identity: { org: "other", namespace: "team", agent: "loom-ui" } }));
      await settle();
      const at = location.search;
      await answer(q4);
      await settle();
      // Nothing opens; the other org's list reads again, as after any write.
      expect(location.search).toBe(at);
      expect(api.listCollections).toHaveBeenLastCalledWith("other", expect.anything());
    });
  });

  describe("where <main> can be measured", () => {
    it.each([
      [TREE_PAGE_MIN + 64, true],
      [TREE_PAGE_MIN - 136, false],
    ] as const)("at %i px of <main> draws the tree: %s", async (width, tree) => {
      resizable(width);
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      history.replaceState(null, "", "/library");
      render(<Documents />);
      await settle();
      expect(screen.queryAllByRole("navigation", { name: "Library places" })).toHaveLength(tree ? 1 : 0);
      expect(screen.queryAllByRole("button", { name: "Show the documents of" })).toHaveLength(tree ? 0 : 1);
    });
  });

  describe("refreshes", () => {
    it("stays on the collection shown when a refresh's answer leaves it out", async () => {
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
      history.replaceState(null, "", "/library?collection=col-q3");
      const { container } = render(<Documents />);
      await settle();
      // A replica that lags answers once without it, then with it.
      api.listCollections
        .mockResolvedValueOnce({ collections: ROWS.filter((c) => c.namespace !== "col-q3"), next_cursor: null })
        .mockResolvedValue({ collections: ROWS, next_cursor: null });
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        act(() => invalidateSharedCollections());
        // Past the retry's wait.
        await act(() => vi.advanceTimersByTimeAsync(1100));
        await waitFor(() => expect(api.listCollections.mock.calls.length).toBeGreaterThanOrEqual(3));
        await act(() => vi.advanceTimersByTimeAsync(100));
      } finally {
        vi.useRealTimers();
      }
      expect(new URLSearchParams(location.search).get("collection")).toBe("col-q3");
      expect(toast).not.toHaveBeenCalledWith(expect.stringMatching(/not among the collections/), "info");
      expect(container.querySelector("[data-doc-row]")).not.toBeNull();
    });

    it("leaves the collection shown once three refresh answers in a row leave it out", async () => {
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      history.replaceState(null, "", "/library?collection=col-q3");
      render(<Documents />);
      await settle();
      const reads = api.listCollections.mock.calls.length;
      api.listCollections.mockResolvedValue({
        collections: ROWS.filter((c) => c.namespace !== "col-q3"),
        next_cursor: null,
      });
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        act(() => invalidateSharedCollections());
        // Past both retries' waits.
        await act(() => vi.advanceTimersByTimeAsync(2100));
        await act(() => vi.advanceTimersByTimeAsync(100));
      } finally {
        vi.useRealTimers();
      }
      expect(api.listCollections.mock.calls.length).toBe(reads + 3);
      expect(toast).toHaveBeenCalledWith(
        expect.stringMatching(/col-q3 is not among the collections you can read now/),
        "info",
      );
      expect(new URLSearchParams(location.search).get("collection")).toBeNull();
    });

    it("waits for a created collection in its own org only", async () => {
      resizable();
      const answer = await startCreate();
      api.listCollections.mockResolvedValue({
        collections: [...ROWS, entry("col-q4", { parent: "col-q3", title: "Q4" })],
        next_cursor: null,
      });
      await answer(q4);
      await settle();
      api.listCollections.mockClear();
      api.listCollections.mockResolvedValue({ collections: [entry("col-b", { title: "B" })], next_cursor: null });
      act(() => useStore.setState({ identity: { org: "other", namespace: "team", agent: "loom-ui" } }));
      // The other org's first answer is the answer: nothing there is waited for.
      const tree = screen.getByRole("navigation", { name: "Library places" });
      expect(await within(tree).findByRole("link", { name: "B" }, { timeout: 500 })).toBeInTheDocument();
      expect(api.listCollections.mock.calls.filter(([o]) => o === "other")).toHaveLength(1);
      expect(tree).not.toHaveAttribute("aria-busy");
    });

    it("reads once for a delete after a create, not waiting again for what was made", async () => {
      resizable();
      const answer = await startCreate();
      const withQ4 = [...ROWS, entry("col-q4", { parent: "col-q3", title: "Q4" })];
      api.listCollections.mockResolvedValue({ collections: withQ4, next_cursor: null });
      await answer(q4);
      await settle();
      expect(new URLSearchParams(location.search).get("collection")).toBe("col-q4");
      // Delete it: the list read after no longer holds it, and that is the answer.
      api.deleteCollection.mockResolvedValue({ deleted: true, erasure_status: "completed" });
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for Q4" }), { button: 0, ctrlKey: false });
      await settle();
      confirm.next = true;
      const reads = api.listCollections.mock.calls.length;
      fireEvent.click(screen.getByRole("menuitem", { name: "Delete collection" }));
      await settle();
      confirm.next = false;
      expect(api.listCollections.mock.calls.length).toBe(reads + 1);
      const tree = screen.getByRole("navigation", { name: "Library places" });
      expect(tree).not.toHaveAttribute("aria-busy");
      expect(within(tree).queryByRole("link", { name: "Q4" })).toBeNull();
    });

    it("says it could not load the collections when the page's own read again fails", async () => {
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      api.createCollection.mockImplementation(async () => {
        // The read the page asks for once the collection is made fails.
        api.listCollections.mockRejectedValue(new Error("offline"));
        return { namespace: "col-q4", parent: "col-q3", created: true, title: "Q4" };
      });
      history.replaceState(null, "", "/library?collection=col-q3");
      render(<Documents />);
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "New collection" }));
      fireEvent.change(await screen.findByRole("textbox", { name: "Title" }), { target: { value: "Q4" } });
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Create" }));
      await settle();
      // The select, the path and the chips are empty while the page reads again: nothing to refresh.
      expect(await screen.findByText(/Couldn't load collections/)).toBeInTheDocument();
      expect(screen.queryByText(/Couldn't refresh collections/)).toBeNull();
      cleanup();
      // Beside the tree, which still draws the last answer, it is a refresh that failed.
      resizable();
      const answer = await startCreate();
      api.listCollections.mockRejectedValue(new Error("offline"));
      await answer(q4);
      await settle();
      expect(await screen.findByText(/Couldn't refresh collections/)).toBeInTheDocument();
    });

    it("marks the tree busy only while it reads again for a create, not after that read failed", async () => {
      resizable();
      const answer = await startCreate();
      let fail: (e: Error) => void = () => {};
      api.listCollections.mockReturnValue(new Promise((_, reject) => (fail = reject)));
      await answer(q4);
      const tree = screen.getByRole("navigation", { name: "Library places" });
      expect(tree).toHaveAttribute("aria-busy", "true");
      await act(async () => fail(new Error("offline")));
      await settle();
      expect(tree).not.toHaveAttribute("aria-busy");
    });
    it("keeps the tree's rows, marked busy, while a write elsewhere reads the list again", async () => {
      resizable();
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      history.replaceState(null, "", "/library?collection=col-q3");
      render(<Documents />);
      const tree = await screen.findByRole("navigation", { name: "Library places" });
      await within(tree).findByRole("link", { name: "col-q3" });
      expect(tree).not.toHaveAttribute("aria-busy");
      let answer: (v: unknown) => void = () => {};
      api.listCollections.mockReturnValue(new Promise((resolve) => (answer = resolve)));
      act(() => invalidateSharedCollections());
      await settle();
      expect(tree).toHaveAttribute("aria-busy", "true");
      expect(within(tree).getByRole("link", { name: "col-q3" })).toHaveAttribute("aria-current", "page");
      await act(async () => answer({ collections: ROWS, next_cursor: null }));
      await settle();
      expect(tree).not.toHaveAttribute("aria-busy");
    });
    it("says it could not refresh the collections when a write's read again fails, keeping the list", async () => {
      resizable();
      api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
      history.replaceState(null, "", "/library?collection=col-q3");
      render(<Documents />);
      const tree = await screen.findByRole("navigation", { name: "Library places" });
      await within(tree).findByRole("link", { name: "col-q3" });
      api.listCollections.mockRejectedValue(new Error("offline"));
      act(() => invalidateSharedCollections());
      await settle();
      expect(screen.getByText(/Couldn't refresh collections/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Load collections again" })).toBeInTheDocument();
      expect(within(tree).getByRole("link", { name: "col-q3" })).toHaveAttribute("aria-current", "page");
      expect(tree).not.toHaveAttribute("aria-busy");
    });
  });

  it("beside the tree, shows none of one org's collections after a switch to another", async () => {
    resizable();
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    history.replaceState(null, "", "/library");
    render(<Documents />);
    const tree = await screen.findByRole("navigation", { name: "Library places" });
    expect(await within(tree).findByRole("link", { name: "col-q3" })).toBeInTheDocument();
    fireEvent.click(within(tree).getByRole("button", { name: "Collections in col-q3" }));
    expect(localStorage.getItem("loom_library_expanded_acme_v1")).toBe('["col-q3"]');
    api.listCollections.mockReturnValue(new Promise(() => {}));
    act(() => useStore.setState({ identity: { org: "other", namespace: "team", agent: "loom-ui" } }));
    await settle();
    const after = screen.getByRole("navigation", { name: "Library places" });
    expect(within(after).queryByRole("link", { name: "col-q3" })).toBeNull();
    expect(within(after).queryByRole("link", { name: "My files" })).toBeNull();
    // Acme's folds stay acme's; the other org's tree opens nothing.
    expect(localStorage.getItem("loom_library_expanded_acme_v1")).toBe('["col-q3"]');
    expect(within(after).queryByRole("button", { expanded: true })).toBeNull();
  });

  it("on a code namespace reads only a collection picked, and lists no namespace to pick", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    useStore.setState({
      identity: { org: "acme", namespace: "code-repo", agent: "loom-ui" },
      myNamespaces: [{ namespace: "code-repo", kind: "code" } as never],
    });
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).not.toHaveBeenCalled();
    expect(screen.getByText("Pick a collection", { selector: "h2, h3, p, div" })).toBeInTheDocument();
    // Where a code namespace's data lives instead.
    expect(screen.getByRole("button", { name: "Open repositories" })).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    const menu = within(screen.getByRole("dialog"));
    expect(menu.queryByText(/^Namespace /)).not.toBeInTheDocument();
    fireEvent.click(menu.getByText(/col-q3/));
    await settle();
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-q3" });
  });

  it("with no namespace, opens one's own files in place of the page; with none, lists every collection", async () => {
    api.listCollections.mockResolvedValue({
      collections: [entry("col-a", { title: "Alpha" }), entry("col-me-1a2b", { personal: true })],
      next_cursor: null,
    });
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    const entries = window.history.length;
    render(<Documents />);
    await settle();
    expect(new URLSearchParams(window.location.search).get("collection")).toBe("col-me-1a2b");
    // A correction, not a pick: Back leaves the Library.
    expect(window.history.length).toBe(entries);
    expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-me-1a2b" });
    expect(screen.queryByText(/reaches no namespace/)).not.toBeInTheDocument();
    cleanup();
    window.history.replaceState(null, "", "/library");
    api.listCollections.mockResolvedValue({
      collections: [entry("col-z", { title: "Zeta" }), entry("col-a", { title: "Alpha", role: "viewer" })],
      next_cursor: null,
    });
    render(<Documents />);
    await settle();
    // No files of its own (a token that is not a person's): the top level, where both lists show.
    expect(new URLSearchParams(window.location.search).get("collection")).toBeNull();
    expect(screen.getByText("Collections are shared document libraries. Pick a collection.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Find public namespaces to join" })).toBeNull();
    expect(
      within(screen.getByRole("region", { name: /^Collections/ })).getByRole("link", { name: "Zeta" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: /Shared with you/ })).getByRole("link", { name: "Alpha" }),
    ).toBeInTheDocument();
  });

  it("with no namespace, leaves a linked collection it cannot read for the one it opens, and says so", async () => {
    api.listCollections.mockResolvedValue({
      collections: [entry("col-me-1a2b", { personal: true })],
      next_cursor: null,
    });
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    history.replaceState(null, "", "/library?collection=col-secret");
    render(<Documents />);
    await settle();
    expect(toast).toHaveBeenCalledWith(
      "col-secret is not among the collections you can read now; showing My files.",
      "info",
    );
    expect(new URLSearchParams(window.location.search).get("collection")).toBe("col-me-1a2b");
    cleanup();
    // A list that stopped early: the link may be to a later one.
    api.listCollections.mockResolvedValue({
      collections: [entry("col-me-1a2b", { personal: true })],
      next_cursor: "more",
    });
    history.replaceState(null, "", "/library?collection=col-later");
    render(<Documents />);
    await settle();
    expect(toast).toHaveBeenCalledWith(
      "col-later is past the first collections listed here; showing My files.",
      "info",
    );
    expect(new URLSearchParams(window.location.search).get("collection")).toBe("col-me-1a2b");
  });

  it("says nothing of collections or namespaces while the list is read", async () => {
    api.listCollections.mockReturnValue(new Promise(() => {}));
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    // Not known yet: no "Pick a collection", no "reaches no namespace", no link to public namespaces to take back.
    expect(screen.queryByText("Pick a collection", { selector: "h2, h3, p, div" })).not.toBeInTheDocument();
    expect(screen.queryByText(/reaches no namespace/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Find public namespaces to join" })).not.toBeInTheDocument();
  });

  it("when the list fails, says no collection is missing", async () => {
    api.listCollections.mockRejectedValue(new Error("offline"));
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    expect(screen.queryByText("No collection yet", { selector: "h2, h3, p, div" })).not.toBeInTheDocument();
    expect(screen.getByText("Pick a collection", { selector: "h2, h3, p, div" })).toBeInTheDocument();
    // Not known to hold one: the page says what is missing, and offers to join a namespace.
    expect(screen.getByText("Your token reaches no namespace yet. Pick a collection.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find public namespaces to join" })).toBeInTheDocument();
  });

  it("on a tracker namespace of either kind, points at Patterns", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    for (const kind of ["qa", "tracker"]) {
      useStore.setState({
        identity: { org: "acme", namespace: "issues", agent: "loom-ui" },
        myNamespaces: [{ namespace: "issues", kind } as never],
      });
      render(<Documents />);
      await settle();
      expect(screen.getByRole("button", { name: "Open in Patterns" }), kind).toBeInTheDocument();
      cleanup();
    }
  });

  it("with no namespace and no collection, says so and points at Namespaces in your org", async () => {
    api.listCollections.mockResolvedValue({ collections: [], next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).not.toHaveBeenCalled();
    expect(screen.getByText("No collection yet", { selector: "h2, h3, p, div" })).toBeInTheDocument();
    expect(screen.getByText(/Create a collection\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find public namespaces to join" })).toBeInTheDocument();
    // New collection opens the picker's own create dialog in place.
    const state = screen.getByRole("heading", { name: "No collection yet" }).parentElement as HTMLElement;
    await userEvent.click(within(state).getByRole("button", { name: "New collection" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("leaves the collection deleted here for the header's namespace, replacing the entry", async () => {
    api.listCollections
      .mockResolvedValueOnce({ collections: ROWS, next_cursor: null })
      .mockResolvedValue({ collections: ROWS.filter((c) => c.namespace !== "col-q3"), next_cursor: null });
    api.deleteCollection.mockResolvedValue({ deleted: true, erasure_status: "completed" });
    api.listKnowledge.mockResolvedValue({
      rows: [{ source_uri: "a", title: "A", chunk_count: 1, updated_at: null, tags: [] }],
      next_cursor: null,
    });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for col-q3" }), { button: 0, ctrlKey: false });
    await settle();
    confirm.next = true;
    const entries = history.length;
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete collection" }));
    await settle();
    confirm.next = false;
    // Leaving what is gone replaces the entry: Back does not return to it.
    expect(history.length).toBe(entries);
    expect(new URLSearchParams(location.search).get("collection")).toBeNull();
  });

  it("keeps New collection open when the list confirms the collection the page is on", async () => {
    let answer: (v: unknown) => void = () => undefined;
    api.listCollections.mockReturnValue(
      new Promise((r) => {
        answer = r;
      }),
    );
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "New collection" }));
    await settle();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await act(async () => answer({ collections: ROWS, next_cursor: null }));
    await settle();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("tells the header while a collection is linked, so its New › Document steps aside", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    history.replaceState(null, "", "/library?collection=col-q3");
    const { unmount } = render(<Documents />);
    await settle();
    expect(useStore.getState().libraryElsewhere).toBe(true);
    unmount();
    expect(useStore.getState().libraryElsewhere).toBe(false);
  });

  it("gives the header's New back when the page falls back to the namespace, and not on a code one", async () => {
    api.listCollections.mockRejectedValue(new Error("offline"));
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    await settle();
    // The list failed: the page shows the namespace, where the header's New adds.
    expect(useStore.getState().libraryElsewhere).toBe(false);
    cleanup();
    history.replaceState(null, "", "/library");
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    useStore.setState({
      identity: { org: "acme", namespace: "code-repo", agent: "loom-ui" },
      myNamespaces: [{ namespace: "code-repo", kind: "code" } as never],
    });
    render(<Documents />);
    await settle();
    // A code namespace holds no documents: the header offers none to add there.
    expect(useStore.getState().libraryElsewhere).toBe(true);
  });

  it("keeps the whole description in an item for the search, past the cut", () => {
    const long = entry("col-long", { description: `${"x ".repeat(50)}checklist` });
    render(<Picker org="acme" namespace="team" collections={read([long])} current={null} onPick={() => undefined} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    const item = within(screen.getByRole("dialog")).getByText(/^col-long/);
    expect(item.textContent).toContain("checklist");
  });

  it("makes a pick a history entry, so Back returns to the place before it", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    history.replaceState(null, "", "/library?namespace=team");
    render(<Documents />);
    await settle();
    const before = history.length;
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByText(/^col-q3/));
    await settle();
    expect(history.length).toBe(before + 1);
    expect(new URLSearchParams(location.search).get("collection")).toBe("col-q3");
  });

  it("picking the place already shown moves nothing", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    history.replaceState(null, "", "/library?namespace=team&collection=col-q3&doc=d1");
    render(<Documents />);
    await settle();
    const before = history.length;
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByText(/^col-q3/));
    await settle();
    expect(history.length).toBe(before);
    expect(new URLSearchParams(location.search).get("collection")).toBe("col-q3");
  });

  it("finds a collection by typing in the select", async () => {
    render(<Picker org="acme" namespace="team" collections={read(ROWS)} current={null} onPick={() => undefined} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    expect(within(screen.getByRole("dialog")).getByRole("textbox")).toBeInTheDocument();
  });
});
