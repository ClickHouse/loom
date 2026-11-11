/**
 * Where a collection sits: the path to it from the top, each ancestor a link, and in it a
 * parent the caller cannot read shown as a dots button, which gives its address when tapped;
 * the phone's back button; and the collections inside the one shown, or the top-level ones.
 */
import { cleanup, fireEvent, render, screen, within } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CollectionEntry } from "../api";
import { axeViolations } from "../test/axe";
import { treeOrder } from "./LibraryCollections";
import {
  CollectionBack,
  CollectionPath,
  CollectionsInside,
  collectionPath,
  collectionsInside,
  sharedWithMe,
} from "./LibraryHierarchy";

const entry = (namespace: string, over: Partial<CollectionEntry> = {}): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "admin",
  description: "",
  ...over,
});

const ROWS = [
  entry("col-team", { title: "Team" }),
  entry("col-q3", { parent: "col-team", title: "Q3" }),
  entry("col-q3-interviews", { parent: "col-q3", title: "Interviews" }),
  entry("col-shared", { parent: "col-hidden", title: "Shared with me" }),
  entry("col-me-1a2b", { personal: true }),
];
const byName = new Map(ROWS.map((c) => [c.namespace, c]));

afterEach(cleanup);

describe("collectionPath", () => {
  it("stops where the walk repeats", () => {
    const rows = [entry("col-a", { parent: "col-b" }), entry("col-b", { parent: "col-a" })];
    const path = collectionPath(rows[0], new Map(rows.map((c) => [c.namespace, c])));
    expect(path.map((p) => ("hidden" in p ? `hidden:${p.hidden}` : p.namespace))).toEqual(["col-b", "col-a"]);
  });
  it("walks up to the top, stopping at a parent the caller cannot read", () => {
    expect(collectionPath(ROWS[2], byName).map((p) => ("hidden" in p ? `hidden:${p.hidden}` : p.namespace))).toEqual([
      "col-team",
      "col-q3",
      "col-q3-interviews",
    ]);
    expect(collectionPath(ROWS[3], byName).map((p) => ("hidden" in p ? `hidden:${p.hidden}` : p.namespace))).toEqual([
      "hidden:col-hidden",
      "col-shared",
    ]);
  });
});

describe("CollectionPath", () => {
  it("says a parent missing from a list that stopped early is not listed, not unreadable", () => {
    render(<CollectionPath current={ROWS[3]} rows={ROWS} truncated onPick={() => {}} />);
    expect(screen.getByRole("button", { name: "A collection not listed here" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "A collection you can't open" })).toBeNull();
  });

  it("keeps the parent and folds the ones above it, and gives one the caller cannot read no link", async () => {
    const onPick = vi.fn();
    const { container } = render(<CollectionPath current={ROWS[2]} rows={ROWS} onPick={onPick} />);
    const nav = screen.getByRole("navigation", { name: "Collection path" });
    // The select this path leads into names the current one.
    expect(within(nav).queryByText("Interviews")).toBeNull();
    expect(within(nav).getByRole("link", { name: "Q3" })).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("button", { name: "1 more above" }));
    const team = await screen.findByRole("link", { name: "Team" });
    // A link to its address: a modified click (a new tab) is the browser's.
    expect(team.getAttribute("href")).toMatch(/collection=col-team/);
    fireEvent.click(team, { ctrlKey: true });
    expect(onPick).not.toHaveBeenCalled();
    fireEvent.click(team);
    expect(onPick).toHaveBeenCalledWith("col-team");
    expect(await axeViolations(container)).toEqual([]);
    cleanup();
    render(<CollectionPath current={ROWS[3]} rows={ROWS} onPick={onPick} />);
    const hidden = screen.getByRole("navigation", { name: "Collection path" });
    expect(within(hidden).queryByRole("link", { name: /col-hidden/ })).toBeNull();
    // A tap opens what it is, by its address, as About this place names it.
    fireEvent.click(within(hidden).getByRole("button", { name: "A collection you can't open" }));
    expect(
      await screen.findByText("col-hidden: a collection you can't open. Its owner can share it with you."),
    ).toBeInTheDocument();
  });

  it("folds a top the caller cannot read with the ones above the parent, saying what it is", async () => {
    const rows = [
      entry("col-b", { parent: "col-hidden", title: "B" }),
      entry("col-c", { parent: "col-b", title: "C" }),
      entry("col-d", { parent: "col-c", title: "D" }),
    ];
    render(<CollectionPath current={rows[2]} rows={rows} onPick={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "2 more above" }));
    expect(await screen.findByText(/^col-hidden: a collection you can't open/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "B" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "C" })).toBeInTheDocument();
  });

  it("leads a top-level collection back to the place that lists it, and shows nothing without one", () => {
    const open = vi.fn();
    render(
      <CollectionPath
        current={ROWS[0]}
        rows={ROWS}
        home={{ label: "My files", collection: "col-me", open }}
        onPick={() => {}}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Collection path" });
    fireEvent.click(within(nav).getByRole("link", { name: "My files" }));
    expect(open).toHaveBeenCalled();
    expect(within(nav).queryByText("Team")).toBeNull();
    cleanup();
    render(<CollectionPath current={ROWS[0]} rows={ROWS} onPick={() => {}} />);
    expect(screen.queryByRole("navigation")).toBeNull();
  });
});

describe("CollectionsInside", () => {
  it("lists equal titles in the order the select does", () => {
    const rows = [entry("col-notes-ab12", { title: "notes" }), entry("col-notes", { title: "Notes" })];
    expect(collectionsInside(rows, null).map((c) => c.namespace)).toEqual(treeOrder(rows).map(({ c }) => c.namespace));
  });

  it("counts repeated titles over every collection listed, not only the ones inside", () => {
    const rows = [
      entry("col-team", { title: "Team" }),
      entry("col-notes", { parent: "col-team", title: "Notes" }),
      entry("col-notes-ab12", { title: "Notes" }),
    ];
    render(<CollectionsInside rows={rows} current={rows[0]} onPick={() => {}} />);
    expect(screen.getByRole("link", { name: "Notes (col-notes)" })).toBeInTheDocument();
  });

  it("lists a collection's children, and on My files the top-level ones", () => {
    expect(collectionsInside(ROWS, ROWS[0]).map((c) => c.namespace)).toEqual(["col-q3"]);
    // Top level: no personal one, and one whose parent the caller cannot read sits there.
    expect(collectionsInside(ROWS, ROWS[4]).map((c) => c.namespace)).toEqual(["col-shared", "col-team"]);
    expect(collectionsInside(ROWS, null).map((c) => c.namespace)).toEqual(["col-shared", "col-team"]);
  });

  it("opens a collection from its row, shows six, then fifty more at a time, and nothing when empty", () => {
    const onPick = vi.fn();
    const many = Array.from({ length: 60 }, (_, i) => entry(`col-k${i}`, { parent: "col-team", title: `Kid ${i}` }));
    render(<CollectionsInside rows={[ROWS[0], ...many]} current={ROWS[0]} onPick={onPick} />);
    const section = screen.getByRole("region", { name: /Collections/ });
    expect(within(section).getAllByRole("link", { name: /^Kid / })).toHaveLength(6);
    const more = within(section).getByRole("button", { name: "Show 50 more" });
    more.focus();
    fireEvent.click(more);
    expect(within(section).getAllByRole("link", { name: /^Kid / })).toHaveLength(56);
    // Its button stays (four are left): the focus stays on it.
    expect(document.activeElement).toBe(within(section).getByRole("button", { name: "Show 4 more" }));
    // The last step's button goes: the focus moves to the first collection it added.
    fireEvent.click(within(section).getByRole("button", { name: "Show 4 more" }));
    expect(within(section).getAllByRole("link", { name: /^Kid / })).toHaveLength(60);
    expect(document.activeElement).toBe(within(section).getAllByRole("link", { name: /^Kid / })[56]);
    expect(within(section).queryByRole("button", { name: /^Show \d+ more$/ })).toBeNull();
    fireEvent.click(within(section).getByRole("button", { name: "Show fewer" }));
    expect(within(section).getAllByRole("link", { name: /^Kid / })).toHaveLength(6);
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: /Collections/ }));
    const kid = within(section).getByRole("link", { name: "Kid 0" });
    expect(kid.getAttribute("href")).toMatch(/collection=col-k0/);
    fireEvent.click(kid, { metaKey: true });
    expect(onPick).not.toHaveBeenCalled();
    fireEvent.click(kid);
    expect(onPick).toHaveBeenCalledWith("col-k0");
    cleanup();
    render(<CollectionsInside rows={ROWS} current={ROWS[2]} onPick={onPick} />);
    expect(screen.queryByRole("region", { name: /Collections/ })).toBeNull();
  });

  it("lists the collections shared with the caller apart, each with where it sits", () => {
    const rows = [
      entry("col-lib", { title: "Library" }),
      // Shared at the top: no path.
      entry("col-r", { title: "Research", role: "viewer" }),
      // A child inherits its parents' grants, so a shared one inside one's own shows only when
      // a token caps the role below the inherited one.
      entry("col-claimed", { parent: "col-lib", title: "Claimed", role: "viewer" }),
      entry("col-r-notes", { parent: "col-r", title: "Notes", role: "viewer" }),
      entry("col-far", { parent: "col-hidden", title: "Far", role: "writer" }),
      // Shared as admin: the caller's to manage, so with their own, here or under a parent the
      // caller cannot read.
      entry("col-ops", { title: "Ops" }),
      entry("col-run", { parent: "col-hidden", title: "Runbooks" }),
      entry("col-me-1a2b", { personal: true, role: "admin" }),
    ];
    // A shared one's children are inside it, not listed again.
    expect(sharedWithMe(rows).map((c) => c.namespace)).toEqual(["col-claimed", "col-far", "col-r"]);
    expect(collectionsInside(rows, null).map((c) => c.namespace)).toEqual(["col-lib", "col-ops", "col-run"]);
    render(<CollectionsInside rows={rows} current={null} onPick={() => {}} />);
    const shared = screen.getByRole("region", { name: /Shared with you/ });
    expect(within(shared).getByRole("link", { name: "Research" })).toBeInTheDocument();
    expect(within(shared).getByRole("link", { name: "Research" }).parentElement?.textContent).toBe("ResearchViewer");
    expect(within(shared).getByText("in Library")).toBeInTheDocument();
    expect(within(shared).getByText("inside a collection you can't open")).toBeInTheDocument();
    expect(within(shared).queryByRole("link", { name: "Ops" })).toBeNull();
    const own = screen.getByRole("region", { name: /^Collections/ });
    // Shared as admin under a parent the caller cannot read: its place is said too.
    expect(within(own).getByRole("link", { name: "Runbooks" }).parentElement).toHaveTextContent(
      "inside a collection you can't open",
    );
    expect(within(own).queryByRole("link", { name: "Research" })).toBeNull();
    cleanup();
    // A list that stopped early: the missing parent may be one the caller can open.
    render(<CollectionsInside rows={rows} truncated current={null} onPick={() => {}} />);
    expect(
      within(screen.getByRole("region", { name: /Shared with you/ })).getByText("inside a collection not listed here"),
    ).toBeInTheDocument();
    cleanup();
    // Inside a collection, only its children: no "Shared with you".
    render(<CollectionsInside rows={rows} current={rows[0]} onPick={() => {}} />);
    expect(screen.queryByRole("region", { name: /Shared with you/ })).toBeNull();
  });

  it("tells two collections with one title apart by their address", () => {
    const rows = [entry("col-notes", { title: "Notes" }), entry("col-notes-ab12", { title: "Notes" })];
    render(<CollectionsInside rows={rows} current={null} onPick={() => {}} />);
    expect(screen.getByRole("link", { name: "Notes (col-notes)" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notes (col-notes-ab12)" })).toBeInTheDocument();
  });
});

describe("CollectionBack, the path on a phone", () => {
  it("goes back to the parent", () => {
    const onPick = vi.fn();
    render(<CollectionBack current={ROWS[2]} rows={ROWS} onPick={onPick} />);
    fireEvent.click(screen.getByRole("button", { name: "Back to Q3" }));
    expect(onPick).toHaveBeenCalledWith("col-q3");
  });

  it("goes back from a top-level collection to the place that lists it, and is absent without one", () => {
    const open = vi.fn();
    render(
      <CollectionBack
        current={ROWS[0]}
        rows={ROWS}
        home={{ label: "My files", collection: "col-me", open }}
        onPick={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Back to My files" }));
    expect(open).toHaveBeenCalled();
    cleanup();
    const { container } = render(<CollectionBack current={ROWS[0]} rows={ROWS} onPick={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("goes back to the place that lists it when its parent cannot be read", () => {
    const open = vi.fn();
    render(
      <CollectionBack
        current={ROWS[3]}
        rows={ROWS}
        home={{ label: "My files", collection: "col-me", open }}
        onPick={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Back to My files" }));
    expect(open).toHaveBeenCalled();
  });

  it("says what a parent the caller cannot read is when tapped, with no place to go back to", async () => {
    render(<CollectionBack current={ROWS[3]} rows={ROWS} onPick={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Back: a collection you can't open" }));
    expect(await screen.findByText(/^col-hidden: a collection you can't open/)).toBeInTheDocument();
  });
});
