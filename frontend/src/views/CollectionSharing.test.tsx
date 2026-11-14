// A collection's sharing in its header: who can see it in words, Share for its admins, See
// who for anyone else, the person's own files as theirs alone, the read's states, and the
// members dialog with who else can see it through each parent.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import type { CollectionEntry, GrantEntry } from "../api";
import { ApiError } from "../lib/errors";
import { repeatedTitles } from "../lib/collections";
import { useStore } from "../lib/state";
import { PHONE_QUERY } from "../lib/useMedia";
import { axeViolations } from "../test/axe";
import { type ReadAt, ShareButton, useCollectionSharing, whoCanSee } from "./CollectionSharing";

const api = vi.hoisted(() => ({ listGrants: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));

const at = "2026-09-17T14:50:28Z";
const grant = (subject: string, over: Partial<GrantEntry> = {}): GrantEntry => ({
  subject,
  role: "viewer",
  status: "active",
  granted_at: at,
  granted_by: "ada",
  ...over,
});
const entry = (namespace: string, over: Partial<CollectionEntry> = {}): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "admin",
  description: "",
  ...over,
});

const ME = new Set(["ada"]);
const words = (grants: ReadAt[], over = {}) =>
  whoCanSee({ grants, me: ME, org: "acme", titleOf: (ns) => ns.replace("col-", ""), ...over });

describe("who can see a collection, in words", () => {
  it("is only you when the person's grant is the one there", () => {
    expect(words([{ namespace: "col-a", rows: [grant("ada", { role: "admin" })] }])).toEqual({
      who: "Only you",
      how: "Shared with no one else",
      onlyYou: true,
    });
  });

  it("names you first, then groups, people and agents, and counts the rest", () => {
    const rows = ["bob", "group:eng", "agent:helper", "carol", "dan"].map((s) => grant(s));
    expect(words([{ namespace: "col-a", rows: [grant("ada"), ...rows] }])).toEqual({
      who: "You, group eng, bob and 3 more",
      how: "4 people, 1 group and 1 agent",
      onlyYou: false,
    });
  });

  it("is the whole org when everyone holds a grant", () => {
    const w = words([
      { namespace: "col-a", rows: [grant("ada")] },
      { namespace: "col-top", rows: [grant("everyone")] },
    ]);
    expect(w.who).toBe("Everyone in acme");
    expect(w.how).toBe("Inherited from top");
  });

  it("says which parents the access comes from, and a parent the person cannot read", () => {
    const w = words(
      [
        { namespace: "col-a", rows: [grant("ada"), grant("bob")] },
        { namespace: "col-top", rows: [grant("bob"), grant("user:u1")] },
      ],
      { above: "Hidden" },
    );
    expect(w.who).toBe("You, bob and u1");
    expect(w.how).toBe("3 people · Inherited from top · Also everyone who can see Hidden");
  });

  it("leaves out pending requests, and counts a person's other spellings as them", () => {
    const w = words([
      {
        namespace: "col-a",
        rows: [grant("ada@example.com", { also: ["ada"] }), grant("eve", { status: "pending" })],
      },
    ]);
    expect(w.who).toBe("Only you");
  });

  it("says anything added to an empty collection is shared with them", () => {
    const w = words([{ namespace: "col-a", rows: [grant("ada"), grant("bob")] }], { empty: true });
    expect(w.how).toBe("2 people · Anything added here is shared with them");
  });

  it("counts a service account as an agent and names it as one", () => {
    const w = words([{ namespace: "col-a", rows: [grant("ada"), grant("sa:reporter-a1b2c3"), grant("bob")] }]);
    expect(w.who).toBe("You, bob and Reporter bot");
    expect(w.how).toBe("2 people and 1 agent");
  });

  it("concludes nothing from a read cut short: no Only you, and counts or more", () => {
    const cut = words([{ namespace: "col-a", rows: [grant("ada")], cut: true }]);
    expect(cut.onlyYou).toBe(false);
    expect(cut.who).toBe("You and more");
    const some = words([{ namespace: "col-a", rows: [grant("ada"), grant("bob")], cut: true }]);
    expect(some).toMatchObject({ who: "You, bob and more", how: "2 people, or more" });
  });

  it("counts the person among the people", () => {
    expect(words([{ namespace: "col-a", rows: [grant("ada"), grant("bob")] }])).toMatchObject({
      who: "You and bob",
      how: "2 people",
    });
  });

  it("names a parent whose grants could not be read, and concludes nothing from the rest", () => {
    const w = words([
      { namespace: "col-a", rows: [grant("ada")] },
      { namespace: "col-top", rows: [], failed: true },
    ]);
    expect(w.onlyYou).toBe(false);
    expect(w).toMatchObject({
      who: "You",
      how: "1 person · Also everyone who can see top, whose members couldn't be read",
    });
  });

  it("keeps each unseen parent's notice by place, not by its words", () => {
    // "Eng" is a prefix of "Engineering": the first unseen parent says who, the other keeps its notice.
    const w = whoCanSee({
      grants: [
        { namespace: "col-a", rows: [] },
        { namespace: "col-eng", rows: [], failed: true },
        { namespace: "col-engineering", rows: [], failed: true },
      ],
      me: ME,
      org: "acme",
      titleOf: (ns) => (ns === "col-eng" ? "Eng" : "Engineering"),
    });
    expect(w.who).toBe("Everyone who can see Eng");
    expect(w.how).toBe("Also everyone who can see Engineering, whose members couldn't be read");
    // Two parents of one title: the second keeps its notice.
    const twins = whoCanSee({
      grants: [
        { namespace: "col-a", rows: [] },
        { namespace: "col-n1", rows: [], failed: true },
        { namespace: "col-n2", rows: [], failed: true },
      ],
      me: ME,
      org: "acme",
      titleOf: () => "Notes",
    });
    expect(twins.how).toBe("Also everyone who can see Notes, whose members couldn't be read");
  });

  it("names the parent when no grant the person reads names anyone", () => {
    expect(words([{ namespace: "col-a", rows: [] }], { above: "Hidden" }).who).toBe("Everyone who can see Hidden");
  });
});

/** The header's three parts as Documents draws them. */
function Header(props: {
  collection: CollectionEntry;
  byName?: Map<string, CollectionEntry>;
  truncated?: boolean;
  onOpenPlace?: (c: string) => void;
}) {
  const byName = props.byName ?? new Map([[props.collection.namespace, props.collection]]);
  const { line, button, dialog } = useCollectionSharing({
    org: "acme",
    collection: props.collection,
    byName,
    repeated: repeatedTitles([...byName.values()]),
    truncated: props.truncated,
    place: props.collection.namespace,
    onOpenPlace: props.onOpenPlace ?? (() => {}),
  });
  return (
    <main>
      <h1>Library</h1>
      {line}
      {button}
      {dialog}
    </main>
  );
}

const pages = (rowsFor: (ns: string) => GrantEntry[]) =>
  api.listGrants.mockImplementation(async ({ namespace }: { namespace: string }) => ({ rows: rowsFor(namespace) }));

describe("the collection header", () => {
  beforeEach(() => {
    useStore.setState({ whoami: { authenticated: true, subject: "ada", org: "acme", roles: [] } });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  const line = () => screen.getByRole("region", { name: "Who can see it" });

  it("names who can see it and how many, and passes axe", async () => {
    api.listGrants.mockResolvedValue({ rows: [grant("ada", { role: "admin" }), grant("group:eng")] });
    const { container } = render(<Header collection={entry("col-a")} />);
    expect(await screen.findByText("You and group eng")).toBeInTheDocument();
    expect(line()).toHaveTextContent("You and group eng · 1 person and 1 group");
    expect(screen.getByRole("button", { name: "Share" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  const family = () => {
    const top = entry("col-top", { title: "Engineering", parent: "col-secret" });
    const mid = entry("col-a", { parent: "col-top" });
    return {
      mid,
      byName: new Map([
        [mid.namespace, mid],
        [top.namespace, top],
      ]),
    };
  };

  it("reads the grants of the parents the person can read, and words one they cannot", async () => {
    const { mid, byName } = family();
    pages((ns) => (ns === "col-top" ? [grant("group:eng")] : [grant("ada")]));
    render(<Header collection={mid} byName={byName} />);
    expect(await screen.findByText("You and group eng")).toBeInTheDocument();
    expect(line()).toHaveTextContent(
      "You and group eng · 1 person and 1 group · Inherited from Engineering · Also everyone who can see a collection you can't open",
    );
    expect(line()).not.toHaveTextContent("col-secret");
    expect(api.listGrants.mock.calls.map((c) => c[0].namespace)).toEqual(["col-a", "col-top"]);
  });

  it("words a parent missing from a list cut short as not listed here", async () => {
    api.listGrants.mockResolvedValue({ rows: [grant("ada")] });
    render(<Header collection={entry("col-a", { parent: "col-later" })} truncated />);
    expect(await screen.findByText(/Also everyone who can see a collection not listed here/)).toBeInTheDocument();
  });

  it("keeps the rest when a parent's grants fail, naming that parent, even when refused", async () => {
    const { mid, byName } = family();
    api.listGrants.mockImplementation(async ({ namespace }: { namespace: string }) => {
      if (namespace === "col-top") throw new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" });
      return { rows: [grant("ada"), grant("bob")] };
    });
    render(<Header collection={mid} byName={byName} />);
    expect(await screen.findByText("You and bob")).toBeInTheDocument();
    expect(line()).toHaveTextContent("Also everyone who can see Engineering, whose members couldn't be read");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reads every page of the grants, so the oldest (an early everyone) still counts", async () => {
    api.listGrants.mockImplementation(async (_id: unknown, opts: { cursor?: string | null }) =>
      opts.cursor
        ? { rows: [grant("everyone")], next_cursor: null }
        : { rows: [grant("ada"), grant("bob")], next_cursor: "p2" },
    );
    render(<Header collection={entry("col-a")} />);
    expect(await screen.findByText("Everyone in acme")).toBeInTheDocument();
    expect(api.listGrants.mock.calls.map((c) => c[1].cursor ?? null)).toEqual([null, "p2"]);
    expect(api.listGrants.mock.calls[0][1].limit).toBe(500);
  });

  it("stops after twenty pages and says or more", async () => {
    let n = 0;
    api.listGrants.mockImplementation(async () => ({ rows: [grant(`p${n++}`)], next_cursor: "more" }));
    render(<Header collection={entry("col-a")} />);
    expect(await screen.findByText(/and more/)).toBeInTheDocument();
    expect(line()).toHaveTextContent(", or more");
    expect(api.listGrants).toHaveBeenCalledTimes(20);
  });

  it("never shows one collection's words under another while the other is read", async () => {
    const open = entry("col-open");
    const closed = entry("col-closed");
    const byName = new Map([
      [open.namespace, open],
      [closed.namespace, closed],
    ]);
    api.listGrants.mockResolvedValueOnce({ rows: [grant("everyone")] });
    const { rerender } = render(<Header collection={open} byName={byName} />);
    expect(await screen.findByText("Everyone in acme")).toBeInTheDocument();
    api.listGrants.mockReturnValue(new Promise(() => {}));
    rerender(<Header collection={closed} byName={byName} />);
    // The first paint for the new one, before its read starts, is the placeholder.
    expect(screen.queryByText("Everyone in acme")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading who can see it" })).toBeInTheDocument();
  });

  it("says My files are the person's alone, with no Share and no read", async () => {
    const { container } = render(<Header collection={entry("col-me-1a2b", { personal: true })} />);
    expect(line()).toHaveTextContent("Only you · Your own files: no one else's grants reach them");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(api.listGrants).not.toHaveBeenCalled();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows a placeholder while the grants are read", async () => {
    api.listGrants.mockReturnValue(new Promise(() => {}));
    const { container } = render(<Header collection={entry("col-a")} />);
    expect(screen.getByRole("status", { name: "Loading who can see it" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("says a failed read failed, with its request id and Retry, which reads again with focus on the heading", async () => {
    api.listGrants.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "UNAVAILABLE", message: "down", requestId: "req-42" }),
    );
    api.listGrants.mockResolvedValue({ rows: [grant("ada"), grant("bob")] });
    const { container } = render(<Header collection={entry("col-a")} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Couldn't read who can see it: .*req-42/);
    expect(await axeViolations(container)).toEqual([]);
    const retry = screen.getByRole("button", { name: "Retry" });
    retry.focus();
    fireEvent.click(retry);
    expect(await screen.findByText("You and bob")).toBeInTheDocument();
    expect(api.listGrants).toHaveBeenCalledTimes(2);
    // Retry left with the error line: focus is on the page's heading, not the body.
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Library" }));
  });

  it("says a second failure with no request id in a fresh alert, after the placeholder", async () => {
    api.listGrants.mockRejectedValue(new ApiError({ status: 503, code: "UNAVAILABLE", message: "down" }));
    render(<Header collection={entry("col-a")} />);
    const first = await screen.findByRole("alert");
    let answer: (v: unknown) => void = () => {};
    api.listGrants.mockReturnValueOnce(new Promise((_, reject) => (answer = reject)));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading who can see it" })).toBeInTheDocument();
    answer(new ApiError({ status: 503, code: "UNAVAILABLE", message: "down" }));
    const second = await screen.findByRole("alert");
    expect(second).not.toBe(first);
    expect(second.textContent).toBe(first.textContent);
  });

  it("tells apart two parents of one title, in the line and as two dialog sections", async () => {
    const top = entry("col-notes", { title: "Notes" });
    const archive = entry("col-archive", { title: "Archive", parent: "col-notes" });
    const inner = entry("col-notes-2", { title: "Notes", parent: "col-archive" });
    const here = entry("col-here", { title: "This", parent: "col-notes-2", role: "writer" });
    const byName = new Map([top, archive, inner, here].map((c) => [c.namespace, c]));
    pages((ns) => (ns === "col-here" ? [grant("ada")] : [grant(`${ns}@example.com`)]));
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<Header collection={here} byName={byName} />);
    expect(
      await screen.findByText(/Inherited from Notes \(col-notes-2\), Archive and Notes \(col-notes\)/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "See who" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("region", { name: "Also can see it, through Archive" });
    expect(
      within(dialog).getByRole("region", { name: "Also can see it, through Notes (col-notes-2)" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("region", { name: "Also can see it, through Notes (col-notes)" }),
    ).toBeInTheDocument();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key/);
    errors.mockRestore();
  });

  it("says who can see it is not shown to a person refused the collection's grants, with no Retry", async () => {
    api.listGrants.mockRejectedValue(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" }));
    render(<Header collection={entry("col-a", { role: "viewer" })} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Who can see it isn't shown to you: /);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("lists the collection's own members, every page, and who else can see it through each parent", async () => {
    const { mid, byName } = family();
    const many = Array.from({ length: 150 }, (_, i) => grant(`m${i}@example.com`));
    api.listGrants.mockImplementation(
      async ({ namespace }: { namespace: string }, opts: { cursor?: string | null }) => {
        if (namespace === "col-top") return { rows: [grant("group:eng")] };
        // The collection's own grants in two pages of 100 and 51.
        return opts.cursor
          ? { rows: many.slice(99), next_cursor: null }
          : { rows: [grant("ada", { role: "admin" }), ...many.slice(0, 99)], next_cursor: "p2" };
      },
    );
    const onOpenPlace = vi.fn();
    // A writer's dialog: the rows without a role select each, which keeps 150 of them quick.
    render(<Header collection={{ ...mid, role: "writer" }} byName={byName} onOpenPlace={onOpenPlace} />);
    await screen.findByText(/^You, group eng/);
    fireEvent.click(screen.getByRole("button", { name: "See who" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("m149@example.com");
    expect(within(dialog).getByText("m0@example.com")).toBeInTheDocument();
    const through = within(dialog).getByRole("region", { name: "Also can see it, through Engineering" });
    expect(within(through).getByText("group:eng")).toBeInTheDocument();
    const hidden = within(dialog).getByRole("region", { name: "Also can see it, through a collection you can't open" });
    expect(hidden).toHaveTextContent("Who can see that collection isn't shown to you.");
    fireEvent.click(within(through).getByRole("button", { name: "Engineering" }));
    expect(onOpenPlace).toHaveBeenCalledWith("col-top");
  });

  it("shows an inherited-only collection's parents in the dialog, not No members yet alone", async () => {
    const { mid, byName } = family();
    pages((ns) => (ns === "col-top" ? [grant("group:eng"), grant("ada", { role: "admin" })] : []));
    render(<Header collection={{ ...mid, role: "writer" }} byName={byName} />);
    await screen.findByText(/^You and group eng/);
    fireEvent.click(screen.getByRole("button", { name: "See who" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/No one is named here; who can see it comes from/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/^No members yet/)).not.toBeInTheDocument();
    const through = within(dialog).getByRole("region", { name: "Also can see it, through Engineering" });
    expect(within(through).getByText("group:eng")).toBeInTheDocument();
    expect(await axeViolations(dialog)).toEqual([]);
  });

  it("reads the grants again when the members dialog closes, keeping the words busy meanwhile", async () => {
    api.listGrants.mockResolvedValue({ rows: [grant("ada", { role: "admin" })] });
    render(<Header collection={entry("col-a")} />);
    expect(await screen.findByText("Only you")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("ada");
    let answer: (v: unknown) => void = () => {};
    api.listGrants.mockReturnValue(new Promise((r) => (answer = r)));
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(line()).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByText("Only you")).toBeInTheDocument();
    answer({ rows: [grant("ada"), grant("bob")] });
    expect(await screen.findByText("You and bob")).toBeInTheDocument();
  });
});

describe("ShareButton", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("is its icon alone with the label as its tooltip on a phone", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q === PHONE_QUERY,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    render(<ShareButton collection={entry("col-a")} onOpen={() => {}} />);
    const button = screen.getByRole("button", { name: "Share" });
    expect(button).not.toHaveTextContent("Share");
    fireEvent.focus(button);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Share");
  });

  it("is Share for an admin and See who for anyone else, each opening the dialog", () => {
    const onOpen = vi.fn();
    const { rerender } = render(<ShareButton collection={entry("col-a")} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    rerender(<ShareButton collection={entry("col-a", { role: "writer" })} onOpen={onOpen} />);
    expect(screen.queryByRole("button", { name: "Share" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "See who" }));
    expect(onOpen).toHaveBeenCalledTimes(2);
  });
});
