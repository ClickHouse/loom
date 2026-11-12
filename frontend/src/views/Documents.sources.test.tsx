import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { PHONE_QUERY } from "../lib/useMedia";
import { Documents } from "./Documents";

// The Library's Sources section: the sources of the place in view, with Add for its
// admins only.

const api = vi.hoisted(() => ({
  listKnowledge: vi.fn(async (..._args: unknown[]) => ({ rows: [], next_cursor: null })),
  listCollections: vi.fn(async (): Promise<unknown> => ({ collections: [], next_cursor: null })),
}));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), ...api }));
const sources = vi.hoisted(() => ({ listKnowledgeSources: vi.fn() }));
vi.mock("../api/knowledgeSources", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/knowledgeSources")>()),
  ...sources,
}));

const whoami: WhoAmI = { authenticated: true, subject: "alice", org: "acme", roles: ["WRITER"], namespaces: ["team"] };
const mine = (role: MyNamespaceEntry["role"]): MyNamespaceEntry[] => [
  { namespace: "team", role, status: "active", kind: "memory" } as never,
];

function show(role: MyNamespaceEntry["role"] | null) {
  useStore.setState({
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    view: "knowledge",
    whoami,
    myNamespaces: role ? mine(role) : [],
  });
  render(<Documents />);
}

// The section opens from its button in the header's actions.
async function openSources(): Promise<void> {
  await userEvent.setup().click(await screen.findByRole("button", { name: /^Sources/ }));
}

const LISTED = {
  enabled: true,
  sources: [
    {
      name: "team-notes",
      kind: "gdrive",
      config: { folder_ids: ["1AbCdEfGhIjKlMnOp"] },
      created_by: "alice",
      created_at: "2026-01-01T00:00:00Z",
      sync: {
        requested_at: "",
        last_synced_at: "2026-01-01T00:10:00Z",
        status: "ok",
        documents_seen: 4,
        changed: 4,
        removed: 0,
        unconverted: 0,
        unconverted_reasons: {},
        skipped: 0,
        error: "",
      },
    },
  ],
};

describe("Library sources", () => {
  beforeEach(() => {
    resetNamespaceRoleCache();
    sources.listKnowledgeSources.mockResolvedValue(LISTED);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    history.replaceState(null, "", "/");
    cleanup();
    vi.clearAllMocks();
    useStore.setState({ identity: null, whoami: null, myNamespaces: null });
  });

  it("lists the namespace's sources and offers Add to its admins", async () => {
    show("admin");
    await openSources();
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    expect(sources.listKnowledgeSources).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, expect.anything());
    expect(screen.getByRole("button", { name: "Add a source" })).toBeInTheDocument();
  });

  it.each(["viewer", "writer", null] as const)("offers no Add to a role of %s", async (role) => {
    show(role);
    await openSources();
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a source" })).toBeNull();
  });

  it.each([
    ["admin", true],
    ["viewer", false],
  ] as const)("on a collection, takes the collection's own role (%s)", async (role, add) => {
    api.listCollections.mockResolvedValue({
      collections: [{ namespace: "col-plans", parent: "", personal: false, role, description: "", title: "Plans" }],
      next_cursor: null,
    });
    history.replaceState(null, "", "/library?collection=col-plans");
    // The header namespace's role is the opposite, so only the collection's can decide.
    show(add ? "viewer" : "admin");
    await openSources();
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    await waitFor(() =>
      expect(sources.listKnowledgeSources).toHaveBeenLastCalledWith(
        { org: "acme", namespace: "col-plans" },
        expect.anything(),
      ),
    );
    expect(!!screen.queryByRole("button", { name: "Add a source" })).toBe(add);
  });

  it("offers no Add on a viewer's collection to a token with an admin claim", async () => {
    api.listCollections.mockResolvedValue({
      collections: [
        { namespace: "col-plans", parent: "", personal: false, role: "viewer", description: "", title: "Plans" },
      ],
      next_cursor: null,
    });
    history.replaceState(null, "", "/library?collection=col-plans");
    show("admin");
    useStore.setState({ whoami: { ...whoami, roles: ["ADMIN"] } });
    await waitFor(() =>
      expect(sources.listKnowledgeSources).toHaveBeenLastCalledWith(
        { org: "acme", namespace: "col-plans" },
        expect.anything(),
      ),
    );
    await openSources();
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a source" })).toBeNull();
  });

  it("shows no Sources section on a place that takes no source and holds none", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ enabled: true, sources: [], accepts_sources: false });
    useStore.setState({
      identity: { org: "acme", namespace: "alice-notes", agent: "loom-ui" },
      view: "knowledge",
      whoami,
      myNamespaces: [{ namespace: "alice-notes", role: "admin", status: "active", kind: "memory" } as never],
    });
    render(<Documents />);
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalled());
    await act(async () => {
      await sources.listKnowledgeSources.mock.results[0]?.value;
    });
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a source" })).toBeNull();
  });

  it("shows no Sources button while the list loads", async () => {
    sources.listKnowledgeSources.mockReturnValue(new Promise(() => {}));
    show("admin");
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
  });

  it("still lists the active sources of a place that takes no new one", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, accepts_sources: false });
    show("admin");
    await openSources();
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    expect(screen.getByText(/takes no new document sources/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add a source" })).toBeNull();
  });

  it("shows an admin the Sources section and Add on an empty place that takes sources", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, sources: [] });
    show("admin");
    await openSources();
    expect(await screen.findByRole("heading", { name: "Sources" })).toBeInTheDocument();
    expect(screen.getByText(/No document sources\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a source" })).toBeInTheDocument();
  });

  it("shows an admin the list's error under the Sources heading", async () => {
    sources.listKnowledgeSources.mockRejectedValue(new Error("boom"));
    show("admin");
    await openSources();
    expect(await screen.findByText(/Couldn't list the document sources/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sources" })).toBeInTheDocument();
  });

  it("shows a viewer no Sources section when the list cannot be read", async () => {
    sources.listKnowledgeSources.mockRejectedValue(new Error("boom"));
    show("viewer");
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalled());
    await act(async () => {
      await sources.listKnowledgeSources.mock.results[0]?.value.catch(() => {});
    });
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
  });

  it.each([
    ["*", "memory"],
    ["", "memory"],
    ["code-loom", "code"],
  ])("shows no Sources section and lists none on the no-place view of %j", async (ns, kind) => {
    useStore.setState({
      identity: { org: "acme", namespace: ns, agent: "loom-ui" },
      view: "knowledge",
      whoami: { ...whoami, roles: ["ADMIN"] },
      myNamespaces: ns ? [{ namespace: ns, role: "admin", status: "active", kind } as never] : [],
    });
    render(<Documents />);
    await act(async () => {});
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
    expect(sources.listKnowledgeSources).not.toHaveBeenCalled();
  });

  it("makes no source read on a person's own collection", async () => {
    api.listCollections.mockResolvedValue({
      collections: [
        { namespace: "col-me-1", parent: "", personal: true, role: "admin", description: "", title: "Mine" },
      ],
      next_cursor: null,
    });
    history.replaceState(null, "", "/library?collection=col-me-1");
    show("admin");
    await waitFor(() => expect(api.listKnowledge.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "col-me-1" }));
    expect(sources.listKnowledgeSources).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
  });

  it("shows a viewer no Sources section on a place with no source", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, sources: [] });
    show("viewer");
    await waitFor(() => expect(sources.listKnowledgeSources).toHaveBeenCalled());
    await act(async () => {
      await sources.listKnowledgeSources.mock.results[0]?.value;
    });
    expect(screen.queryByRole("button", { name: /^Sources/ })).toBeNull();
  });

  it("tells a viewer a closed place takes no new source, with no retire hint or menu", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, accepts_sources: false });
    show("viewer");
    await openSources();
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    expect(screen.getByText(/takes no new document sources\./)).toBeInTheDocument();
    expect(screen.queryByText(/retire these/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Actions for team-notes" })).toBeNull();
  });

  it("offers only Check and Retire on a source of a place that takes no new one", async () => {
    sources.listKnowledgeSources.mockResolvedValue({ ...LISTED, accepts_sources: false });
    show("admin");
    await openSources();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for team-notes" }));
    const menu = await screen.findByRole("menu", { name: "Actions for team-notes" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Check", "Retire"]);
  });

  it("keeps the list closed, counted on its button, until it is pressed, and closes again", async () => {
    show("viewer");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Sources (1)" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("team-notes")).toBeNull();
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
    // The heading takes the focus, so the list is next in the tab order.
    expect(screen.getByRole("heading", { name: "Sources" })).toHaveFocus();
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("team-notes")).toBeNull();
  });

  it("opens the list below the header, not inside its actions", async () => {
    show("viewer");
    await openSources();
    const section = (await screen.findByRole("heading", { name: "Sources" })).closest("section");
    const button = screen.getByRole("button", { name: "Sources (1)" });
    expect(section).not.toBeNull();
    expect(section?.contains(button)).toBe(false);
    expect(document.querySelector("[data-page-header]")?.contains(section as Node)).toBe(false);
  });

  it("is an icon button named with the count on a phone, and still opens the list", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === PHONE_QUERY,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    show("viewer");
    const button = await screen.findByRole("button", { name: "Sources (1)" });
    expect(button).not.toHaveTextContent("Sources");
    await userEvent.setup().click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("team-notes")).toBeInTheDocument();
  });
});
