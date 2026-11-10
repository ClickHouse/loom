// The Library shows the header's namespace or one of the collections the caller reads: a
// tree with each one's description, a menu that shares or deletes it, and New collection.
import { repeatedTitles } from "../lib/collections";
import { act, cleanup, fireEvent, render, screen, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { PHONE_QUERY } from "../lib/useMedia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CollectionEntry } from "../api";
import { useStore } from "../lib/state";
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
  error: null,
  reload: vi.fn(),
  expected: "",
  ...over,
});

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

  it("keeps collections a cycle in the tree cuts off from the top", () => {
    const rows = [entry("col-a", { parent: "col-b" }), entry("col-b", { parent: "col-a" })];
    expect(treeOrder(rows).map(({ c }) => c.namespace)).toEqual(["col-a", "col-b"]);
  });
});

describe("LibraryCollections", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("names the namespace when no collection is shown, and passes axe", async () => {
    const { container } = render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read(ROWS)}
        current={null}
        onPick={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("Namespace team");
    expect(screen.queryByRole("button", { name: /Actions for/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New collection" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows the collection shown with its role, and its menu for an admin", () => {
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read(ROWS)}
        current={ROWS[1]}
        onPick={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-q3 · Quarter plans");
    expect(screen.getByText("Admin")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for col-q3" })).toBeInTheDocument();
  });

  it("gives a viewer Members and no Delete", async () => {
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read(ROWS)}
        current={ROWS[0]}
        onPick={() => undefined}
      />,
    );
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
      <LibraryCollections
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
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === PHONE_QUERY,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    const rows = [
      ROWS[1],
      entry("col-q3-k", { parent: "col-q3", title: "Kid" }),
      entry("col-q3-k-1", { parent: "col-q3-k" }),
    ];
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read(rows)}
        current={rows[2]}
        onPick={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Back to Kid" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Collection path" })).toBeNull();
  });

  it("gives the user's own collection no menu", () => {
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read(ROWS)}
        current={ROWS[2]}
        onPick={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("My files");
    expect(screen.queryByRole("button", { name: /Actions for/ })).not.toBeInTheDocument();
  });

  it("says when the collections cannot be read, and reads them again on request", () => {
    const collections = read(null, { error: new Error("offline") });
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={collections}
        current={null}
        onPick={() => undefined}
      />,
    );
    expect(screen.getByText(/Couldn't load collections/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load collections again" }));
    expect(collections.reload).toHaveBeenCalled();
  });
});

describe("the path's place above the top", () => {
  it("is the header's namespace, else My files, else none", () => {
    const onPick = vi.fn();
    const at = (namespace: string | null, rows: CollectionEntry[], current: CollectionEntry) =>
      render(
        <LibraryCollections
          org="acme"
          namespace={namespace}
          collections={read(rows)}
          current={current}
          onPick={onPick}
        />,
      );
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
    // The header's namespace is itself a collection: the user's own files are the place above.
    at("col-q3", ROWS, ROWS[0]);
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
    vi.clearAllMocks();
    vi.unstubAllGlobals();
    history.replaceState(null, "", "/");
  });

  it("lists the collections inside below the documents, six again when another is opened", async () => {
    const kids = (parent: string) =>
      Array.from({ length: 10 }, (_, i) => entry(`${parent}-k${i}`, { parent, title: `${parent} kid ${i}` }));
    api.listCollections.mockResolvedValue({
      collections: [ROWS[1], ...kids("col-q3"), ...kids("col-q3-k0")],
      next_cursor: null,
    });
    history.replaceState(null, "", "/library?collection=col-q3");
    render(<Documents />);
    const section = await screen.findByRole("region", { name: /Collections/ });
    fireEvent.click(within(section).getByRole("button", { name: "Show 4 more" }));
    expect(within(section).getAllByRole("link", { name: /^col-q3 kid / })).toHaveLength(10);
    fireEvent.click(within(section).getByRole("link", { name: "col-q3 kid 0" }));
    await settle();
    expect(within(screen.getByRole("region", { name: /Collections/ })).getAllByRole("link")).toHaveLength(6);
  });

  it("lists no collections inside on a phone, where the select lists them", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === PHONE_QUERY,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
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

  it("offers a viewer no Add document and no write on a document", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    api.listKnowledge.mockResolvedValue({ rows: [{ source_uri: "a.md", title: "A", tags: [] }], next_cursor: null });
    history.replaceState(null, "", "/library?collection=col-plans");
    render(<Documents />);
    await settle();
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-plans");
    expect(screen.queryByRole("button", { name: "Add document" })).not.toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for A" }), { button: 0, ctrlKey: false });
    await settle();
    expect(screen.getByRole("menuitem", { name: "Preview" })).toBeInTheDocument();
    for (const write of ["Replace content", "Edit details", "Delete"])
      expect(screen.queryByRole("menuitem", { name: write })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Select A" })).not.toBeInTheDocument();
    await act(async () => {
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    });
    api.getKnowledge.mockResolvedValue({ found: true, content: "text", source_uri: "a.md" });
    fireEvent.click(screen.getByRole("button", { name: "Preview A" }));
    await settle();
    expect(screen.getByRole("dialog", { name: "A" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit content" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Re-upload" })).not.toBeInTheDocument();
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
    useStore.setState({ myNamespaces: null });
  });

  it("while the list is read, or when it fails, says no collection is missing", async () => {
    api.listCollections.mockRejectedValue(new Error("offline"));
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    expect(screen.queryByText("No collection yet", { selector: "h2, h3, p, div" })).not.toBeInTheDocument();
    expect(screen.getByText("Pick a collection", { selector: "h2, h3, p, div" })).toBeInTheDocument();
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
    useStore.setState({ myNamespaces: null });
  });

  it("with no namespace and no collection, says so and points at Spaces", async () => {
    api.listCollections.mockResolvedValue({ collections: [], next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).not.toHaveBeenCalled();
    expect(screen.getByText("No collection yet", { selector: "h2, h3, p, div" })).toBeInTheDocument();
    expect(screen.getByText(/Create a collection above/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browse and join spaces" })).toBeInTheDocument();
  });

  it("lists a header namespace that is a collection once, with its role", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "col-plans", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    expect(screen.getByRole("button", { name: "Show the documents of" })).toHaveTextContent("col-plans");
    expect(screen.getByText("Viewer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add document" })).not.toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    const menu = within(screen.getByRole("dialog"));
    expect(menu.queryByText(/^Namespace /)).not.toBeInTheDocument();
    expect(menu.getAllByText(/col-plans/)).toHaveLength(1);
  });

  it("offers no write on a header namespace that is a collection until its role is known", async () => {
    api.listCollections.mockReturnValue(new Promise(() => undefined));
    api.listKnowledge.mockResolvedValue({
      rows: [{ source_uri: "a", title: "A", chunk_count: 1, updated_at: null, tags: [] }],
      next_cursor: null,
    });
    useStore.setState({ identity: { org: "acme", namespace: "col-team", agent: "loom-ui" }, myNamespaces: null });
    render(<Documents />);
    await settle();
    expect(api.listKnowledge).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Add document" })).not.toBeInTheDocument();
    cleanup();
    // The namespace menu says writer: the writes are offered at once.
    useStore.setState({
      whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["VIEWER"] } as never,
      myNamespaces: [{ namespace: "col-team", role: "writer", status: "active" } as never],
    });
    render(<Documents />);
    await settle();
    expect(screen.getByRole("button", { name: "Add document" })).toBeInTheDocument();
    cleanup();
    // A claim-only row is read by the token's claim (viewer here), and a pending one as none.
    for (const row of [
      { namespace: "col-team", role: "jwt", status: "active" },
      { namespace: "col-team", role: "writer", status: "pending" },
    ]) {
      useStore.setState({ myNamespaces: [row as never] });
      render(<Documents />);
      await settle();
      expect(screen.queryByRole("button", { name: "Add document" }), JSON.stringify(row)).not.toBeInTheDocument();
      cleanup();
    }
    useStore.setState({ myNamespaces: null, whoami: null });
  });

  it("keeps New collection open when the list confirms the collection the page is on", async () => {
    let answer: (v: unknown) => void = () => undefined;
    api.listCollections.mockReturnValue(
      new Promise((r) => {
        answer = r;
      }),
    );
    useStore.setState({ identity: { org: "acme", namespace: "col-q3", agent: "loom-ui" } });
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
    useStore.setState({ myNamespaces: null });
  });

  it("keeps the whole description in an item for the search, past the cut", () => {
    const long = entry("col-long", { description: `${"x ".repeat(50)}checklist` });
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read([long])}
        current={null}
        onPick={() => undefined}
      />,
    );
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
    render(
      <LibraryCollections
        org="acme"
        namespace="team"
        collections={read(ROWS)}
        current={null}
        onPick={() => undefined}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    expect(within(screen.getByRole("dialog")).getByRole("textbox")).toBeInTheDocument();
  });

  it("reads nothing more once the header's own collection is deleted here", async () => {
    api.listCollections
      .mockResolvedValueOnce({ collections: ROWS, next_cursor: null })
      .mockResolvedValue({ collections: ROWS.filter((c) => c.namespace !== "col-q3"), next_cursor: null });
    api.deleteCollection.mockResolvedValue({ deleted: true, erasure_status: "completed" });
    api.listKnowledge.mockResolvedValue({
      rows: [{ source_uri: "a", title: "A", chunk_count: 1, updated_at: null, tags: [] }],
      next_cursor: null,
    });
    useStore.setState({ identity: { org: "acme", namespace: "col-q3", agent: "loom-ui" } });
    render(<Documents />);
    await settle();
    expect(screen.getByRole("button", { name: "Preview A" })).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for col-q3" }), { button: 0, ctrlKey: false });
    await settle();
    confirm.next = true;
    const entries = history.length;
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete collection" }));
    await settle();
    confirm.next = false;
    // Leaving what is gone replaces the entry: Back does not return to it.
    expect(history.length).toBe(entries);
    expect(screen.queryByRole("button", { name: "Preview A" })).not.toBeInTheDocument();
    expect(screen.getByText(/col-q3 was deleted/)).toBeInTheDocument();
  });

  it("picking the header's own collection shows it as the header's namespace, keeping the header's New", async () => {
    api.listCollections.mockResolvedValue({ collections: ROWS, next_cursor: null });
    useStore.setState({ identity: { org: "acme", namespace: "col-q3", agent: "loom-ui" } });
    history.replaceState(null, "", "/library?collection=col-plans");
    render(<Documents />);
    await settle();
    expect(useStore.getState().libraryElsewhere).toBe(true);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Show the documents of" }), { button: 0, ctrlKey: false });
    fireEvent.click(screen.getByRole("button", { name: "Show the documents of" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByText(/^col-q3/));
    await settle();
    expect(new URLSearchParams(location.search).get("collection")).toBeNull();
    expect(useStore.getState().libraryElsewhere).toBe(false);
  });
});
