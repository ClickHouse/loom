// A collection's sharing in its header: who can see it in words, Share for its admins, See
// who for anyone else, the person's own files as theirs alone, and the read's states.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import type { CollectionEntry, GrantEntry } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { PHONE_QUERY } from "../lib/useMedia";
import { axeViolations } from "../test/axe";
import { CollectionSharingLine, type GrantsAt, ShareButton, whoCanSee } from "./CollectionSharing";

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
const words = (grants: GrantsAt[], over = {}) =>
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
      how: "3 people, 1 group and 1 agent",
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
    expect(w.how).toBe("2 people · Inherited from top · Also everyone who can see Hidden");
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
    expect(w.how).toBe("1 person · Anything added here is shared with them");
  });

  it("counts a service account as an agent and names it as one", () => {
    const w = words([{ namespace: "col-a", rows: [grant("ada"), grant("sa:reporter-a1b2c3"), grant("bob")] }]);
    expect(w.who).toBe("You, bob and Reporter bot");
    expect(w.how).toBe("1 person and 1 agent");
  });

  it("concludes nothing from a read cut short: no Only you, and counts or more", () => {
    const cut = words([{ namespace: "col-a", rows: [grant("ada")], cut: true }]);
    expect(cut.onlyYou).toBe(false);
    expect(cut.who).toBe("You and more");
    const some = words([{ namespace: "col-a", rows: [grant("ada"), grant("bob")], cut: true }]);
    expect(some).toMatchObject({ who: "You, bob and more", how: "1 person, or more" });
  });

  it("names the parent when no grant the person reads names anyone", () => {
    expect(words([{ namespace: "col-a", rows: [] }], { above: "Hidden" }).who).toBe("Everyone who can see Hidden");
  });
});

describe("CollectionSharingLine", () => {
  beforeEach(() => {
    useStore.setState({ whoami: { authenticated: true, subject: "ada", org: "acme", roles: [] } });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  const show = (collection: CollectionEntry, byName = new Map([[collection.namespace, collection]])) =>
    render(<CollectionSharingLine org="acme" collection={collection} byName={byName} version={0} />);
  const line = () => screen.getByRole("region", { name: "Who can see it" });

  it("names who can see it and how many, and passes axe", async () => {
    api.listGrants.mockResolvedValue({ rows: [grant("ada", { role: "admin" }), grant("group:eng")] });
    const { container } = show(entry("col-a"));
    expect(await screen.findByText("You and group eng")).toBeInTheDocument();
    expect(line()).toHaveTextContent("You and group eng · 1 group");
    expect(await axeViolations(container)).toEqual([]);
  });

  it("reads the grants of the parents the person can read, and names one they cannot", async () => {
    const top = entry("col-top", { title: "Engineering", parent: "col-secret" });
    const mid = entry("col-a", { parent: "col-top" });
    api.listGrants.mockImplementation(async ({ namespace }: { namespace: string }) => ({
      rows: namespace === "col-top" ? [grant("group:eng")] : [grant("ada")],
    }));
    show(
      mid,
      new Map([
        [mid.namespace, mid],
        [top.namespace, top],
      ]),
    );
    expect(await screen.findByText("You and group eng")).toBeInTheDocument();
    expect(line()).toHaveTextContent(
      "You and group eng · 1 group · Inherited from Engineering · Also everyone who can see a collection you can't open",
    );
    expect(line()).not.toHaveTextContent("col-secret");
    expect(api.listGrants.mock.calls.map((c) => c[0].namespace)).toEqual(["col-a", "col-top"]);
  });

  it("says a parent missing from a list cut short is not listed here", async () => {
    const c = entry("col-a", { parent: "col-later" });
    api.listGrants.mockResolvedValue({ rows: [grant("ada")] });
    render(
      <CollectionSharingLine org="acme" collection={c} byName={new Map([[c.namespace, c]])} truncated version={0} />,
    );
    expect(await screen.findByText(/Also everyone who can see a collection not listed here/)).toBeInTheDocument();
  });

  it("reads every page of the grants, so the oldest (an early everyone) still counts", async () => {
    api.listGrants.mockImplementation(async (_id: unknown, opts: { cursor?: string | null }) =>
      opts.cursor
        ? { rows: [grant("everyone")], next_cursor: null, truncated: false }
        : { rows: [grant("ada"), grant("bob")], next_cursor: "p2", truncated: true },
    );
    show(entry("col-a"));
    expect(await screen.findByText("Everyone in acme")).toBeInTheDocument();
    expect(api.listGrants.mock.calls.map((c) => c[1].cursor ?? null)).toEqual([null, "p2"]);
    expect(api.listGrants.mock.calls[0][1].limit).toBe(500);
  });

  it("stops after twenty pages and says or more", async () => {
    let n = 0;
    api.listGrants.mockImplementation(async () => ({ rows: [grant(`p${n++}`)], next_cursor: "more", truncated: true }));
    show(entry("col-a"));
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
    const { rerender } = render(<CollectionSharingLine org="acme" collection={open} byName={byName} version={0} />);
    expect(await screen.findByText("Everyone in acme")).toBeInTheDocument();
    api.listGrants.mockReturnValue(new Promise(() => {}));
    rerender(<CollectionSharingLine org="acme" collection={closed} byName={byName} version={0} />);
    // The first paint for the new one, before its read starts, is the placeholder.
    expect(screen.queryByText("Everyone in acme")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading who can see it" })).toBeInTheDocument();
  });

  it("keeps the words, marked busy, while the same collection is read again", async () => {
    api.listGrants.mockResolvedValueOnce({ rows: [grant("ada"), grant("bob")] });
    const c = entry("col-a");
    const byName = new Map([[c.namespace, c]]);
    const { rerender } = render(<CollectionSharingLine org="acme" collection={c} byName={byName} version={0} />);
    expect(await screen.findByText("You and bob")).toBeInTheDocument();
    api.listGrants.mockReturnValue(new Promise(() => {}));
    rerender(<CollectionSharingLine org="acme" collection={c} byName={byName} version={1} />);
    await waitFor(() => expect(line()).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByText("You and bob")).toBeInTheDocument();
  });

  it("says My files are the person's alone, with no Share and no read", async () => {
    const { container } = show(entry("col-me-1a2b", { personal: true }));
    expect(line()).toHaveTextContent("Only you · Your own files: no one else's grants reach them");
    expect(api.listGrants).not.toHaveBeenCalled();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows a placeholder while the grants are read", async () => {
    api.listGrants.mockReturnValue(new Promise(() => {}));
    const { container } = show(entry("col-a"));
    expect(screen.getByRole("status", { name: "Loading who can see it" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("says a failed read failed, with its request id and Retry, which reads again", async () => {
    api.listGrants.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "UNAVAILABLE", message: "down", requestId: "req-42" }),
    );
    api.listGrants.mockResolvedValue({ rows: [grant("ada"), grant("bob")] });
    const { container } = show(entry("col-a"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Couldn't read who can see it: .*req-42/);
    expect(await axeViolations(container)).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("You and bob")).toBeInTheDocument();
    expect(api.listGrants).toHaveBeenCalledTimes(2);
  });

  it("says who can see it is not shown to a person refused the grants, with no Retry", async () => {
    api.listGrants.mockRejectedValue(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT", message: "no" }));
    show(entry("col-a", { role: "viewer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Who can see it isn't shown to you: /);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("reads again when the members dialog closes", async () => {
    api.listGrants.mockResolvedValue({ rows: [grant("ada")] });
    const c = entry("col-a");
    const byName = new Map([[c.namespace, c]]);
    const { rerender } = render(<CollectionSharingLine org="acme" collection={c} byName={byName} version={0} />);
    expect(await screen.findByText("Only you")).toBeInTheDocument();
    api.listGrants.mockResolvedValue({ rows: [grant("ada"), grant("bob")] });
    rerender(<CollectionSharingLine org="acme" collection={c} byName={byName} version={1} />);
    await waitFor(() => expect(screen.getByText("You and bob")).toBeInTheDocument());
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
