// A collection's sharing in its header: who can see it in words, Share for its admins, See
// who for anyone else, the person's own files as theirs alone, and the read's states.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import type { CollectionEntry, GrantEntry } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { axeViolations } from "../test/axe";
import { CollectionSharingLine, ShareButton, whoCanSee } from "./CollectionSharing";

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
const words = (grants: { namespace: string; rows: GrantEntry[] }[], over = {}) =>
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
      "You and group eng · 1 group · Inherited from Engineering · Also everyone who can see col-secret",
    );
    expect(api.listGrants.mock.calls.map((c) => c[0].namespace)).toEqual(["col-a", "col-top"]);
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

  it("says a failed read failed, with Retry, which reads again", async () => {
    api.listGrants.mockRejectedValueOnce(new ApiError({ status: 503, code: "UNAVAILABLE", message: "down" }));
    api.listGrants.mockResolvedValue({ rows: [grant("ada"), grant("bob")] });
    const { container } = show(entry("col-a"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Couldn't read who can see it: /);
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
  afterEach(cleanup);

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
