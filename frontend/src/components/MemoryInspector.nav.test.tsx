import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { readBackLabel, readPlace } from "../lib/place";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { invalidateShared } from "../lib/sharedReads";
import { MemoryInspector } from "./MemoryInspector";

// Reading something keeps you where you are. An entity opens a popover in the
// drawer — the memories that also mention it swap the drawer in place, its neighbours are
// listed, and "Open in Graph" is a new history entry with a way back. The source
// conversation replays in a dialog over the drawer. The footer keeps Edit, Ask about this
// and Delete and folds the rest into one More menu; it wraps on a narrow drawer.

const DETAIL: MemoryDetail = {
  found: true,
  value: "the public API allows 100 requests a minute",
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  tags: [],
  status: "active",
  agent: "claude-desktop",
  entities: ["rate limiting", "public api"],
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    // The server's shape: `results`, each with the entities it shares with this memory.
    memoryRelated: vi.fn(async () => ({
      results: [
        { memory_key: "api/quota", shared_entities: ["rate limiting"], overlap_count: 1 },
        { memory_key: "api/keys", shared_entities: ["public api"], overlap_count: 1 },
        { memory_key: "runbooks/429", shared_entities: ["Rate Limiting", "public api"], overlap_count: 2 },
      ],
    })),
    relatedFacts: vi.fn(async () => ({
      entity: "rate limiting",
      related: [
        { entity: "token bucket", co_occur: 4, role: "both" },
        { entity: "redis", co_occur: 2, role: "both" },
      ],
    })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({
      found: true,
      source_kind: "conversation",
      thread_exists: true,
      thread_id: "t-42",
      thread_title: "",
      agent: "claude-code",
      links: [
        { memory_key: "api/limits-v1", relation: "derived_from", direction: "out", label: "" },
        { memory_key: "api/limits-v3", relation: "supersedes", direction: "in", label: "" },
      ],
    })),
    getConversationThread: vi.fn(async () => ({
      found: true,
      thread_id: "t-42",
      title: "Rate limits chat",
      agent: "claude-code",
      msg_count: 1,
      updated_at: "2026-09-18T09:00:00Z",
      messages: [
        {
          msg_id: "m1",
          agent: "claude-code",
          role: "user",
          content: "what is our rate limit?",
          ts: "2026-09-18T09:00:00Z",
        },
      ],
      next_cursor: null,
    })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
vi.mock("./SessionBriefPanel", () => ({ SessionBriefPanel: () => null }));
import { getConversationThread, getMemory, memoryRelated, relatedFacts } from "../api";

const writer: WhoAmI = {
  authenticated: true,
  subject: "w@example.com",
  org: "dev",
  roles: ["WRITER"],
  namespaces: ["ns"],
};
const mine: MyNamespaceEntry[] = [
  {
    namespace: "ns",
    role: "writer",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
  },
];
const identity = { org: "dev", namespace: "ns", agent: "loom-ui" };

async function open(props: { onWalkEntity?: (e: string) => void; onInspectMemory?: (k: string) => void } = {}) {
  render(
    <MemoryInspector
      identity={identity}
      memoryKey="api/rate-limit"
      onClose={() => {}}
      onChanged={() => {}}
      {...props}
    />,
  );
  await screen.findByText("the public API allows 100 requests a minute");
}

const chip = (name: string) => screen.getByRole("link", { name: `Entity ${name}` });

describe("an entity on a memory opens in place", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    invalidateShared();
    sessionStorage.clear();
    vi.mocked(getMemory).mockResolvedValue(DETAIL);
    history.replaceState(null, "", "/memories?memory=api%2Frate-limit");
    useStore.setState({
      authed: true,
      whoami: writer,
      myNamespaces: mine,
      identity,
      view: "memories",
      inspectedMemoryKey: "api/rate-limit",
    });
  });
  afterEach(() => cleanup());

  // An entity is a link to its graph that opens a summary in place: it says it has one,
  // and whether it is open.
  it("draws the entity as a link that says it opens a summary", async () => {
    await open();
    const link = chip("rate limiting");
    expect(link).toHaveAttribute("href", expect.stringContaining("rate"));
    expect(link).toHaveAttribute("aria-haspopup", "dialog");
    expect(link).toHaveAttribute("aria-expanded", "false");
  });

  it("after a navigation from the drawer, closing it leaves the focus on the new page's h1, not the opener", async () => {
    vi.mocked(getMemory).mockResolvedValue({ ...DETAIL, tags: ["ops"] });
    const inspector = (
      <MemoryInspector identity={identity} memoryKey="api/rate-limit" onClose={() => {}} onChanged={() => {}} />
    );
    const page = (drawer: boolean) => (
      <>
        <main id="main">
          <h1 tabIndex={-1}>Memories</h1>
          <button type="button">Opener</button>
        </main>
        {drawer && inspector}
      </>
    );
    const { rerender } = render(page(false));
    screen.getByRole("button", { name: "Opener" }).focus();
    rerender(page(true));
    await screen.findByText("the public API allows 100 requests a minute");
    const seq = useStore.getState().navSeq;
    // A tag searches Memories by it: a navigation, whose arrival focuses the page's h1.
    await userEvent.setup().click(screen.getByRole("button", { name: "ops" }));
    expect(useStore.getState().navSeq).toBeGreaterThan(seq);
    // The navigation closes the drawer, and its arrival focuses the new page's h1 at once;
    // the drawer's own focus return comes after, and must leave it there.
    rerender(page(false));
    const h1 = screen.getByRole("heading", { level: 1, name: "Memories" });
    h1.focus();
    await new Promise((r) => setTimeout(r, 50));
    expect(h1).toHaveFocus();
    expect(screen.getByRole("button", { name: "Opener" })).not.toHaveFocus();
  });

  it("shows a tag by its name, with no '#' glyph", async () => {
    vi.mocked(getMemory).mockResolvedValue({ ...DETAIL, tags: ["ops"] });
    await open();
    expect(screen.getByRole("button", { name: "ops" }).textContent).toBe("ops");
  });

  it("on the Graph, a mention opens in the page's own drawer, not the app's", async () => {
    const onInspectMemory = vi.fn();
    await open({ onInspectMemory });
    await userEvent.click(chip("rate limiting"));
    const pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    await userEvent.click(await within(pop).findByRole("button", { name: "api/quota" }));
    expect(onInspectMemory).toHaveBeenCalledWith("api/quota");
    expect(useStore.getState().inspectedMemoryKey).not.toBe("api/quota");
  });

  it("on the Graph, a redirect follows into the page's own drawer", async () => {
    vi.mocked(getMemory).mockResolvedValue({ ...DETAIL, status: "duplicate", canonical_key: "api/limits" });
    const onInspectMemory = vi.fn();
    await open({ onInspectMemory });
    await userEvent.click(screen.getByTestId("redirect-badge"));
    expect(onInspectMemory).toHaveBeenCalledWith("api/limits");
    expect(useStore.getState().inspectedMemoryKey).not.toBe("api/limits");
  });

  it("a click opens a popover with the memories that mention it and its neighbours, focus on the first link", async () => {
    await open();
    await userEvent.click(chip("rate limiting"));
    const pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    // This memory and the two related rows that share this entity (case aside).
    await within(pop).findByText("Mentioned in 3 memories");
    expect(within(pop).getByRole("button", { name: "api/quota" })).toBeInTheDocument();
    expect(within(pop).getByRole("button", { name: "runbooks/429" })).toBeInTheDocument();
    expect(within(pop).queryByRole("button", { name: "api/keys" })).toBeNull();
    expect(await within(pop).findByText("token bucket")).toBeInTheDocument();
    expect(within(pop).getByText("redis")).toBeInTheDocument();
    expect(memoryRelated).toHaveBeenCalledWith(identity, "api/rate-limit", 100);
    expect(relatedFacts).toHaveBeenCalledWith({ org: "dev", namespace: "ns" }, "rate limiting", { top_k: 5 });
    // The footer link is a Click UI icon, not a glyph, and points at the walk.
    const graph = within(pop).getByRole("link", { name: "Open in Graph" });
    expect(graph).toHaveAttribute("href", "/graph?namespace=ns&seed=rate+limiting");
    expect(graph.textContent).not.toContain("→");
    await waitFor(() => expect(document.activeElement).toBe(within(pop).getByRole("button", { name: "api/quota" })));
  });

  it("a cut-off read that holds no other mention says 'at least', never 'only in this memory'", async () => {
    vi.mocked(memoryRelated).mockResolvedValueOnce({
      results: Array.from({ length: 100 }, (_, i) => ({
        memory_key: `other/${i}`,
        shared_entities: ["public api"],
        overlap_count: 1,
      })),
    } as never);
    await open();
    await userEvent.click(chip("rate limiting"));
    const pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    expect(await within(pop).findByText("Mentioned in at least 1 memory")).toBeInTheDocument();
    expect(within(pop).queryByText("Mentioned only in this memory")).toBeNull();
  });

  it("says a failed read failed, never that the entity is mentioned only here or has no neighbours", async () => {
    vi.mocked(memoryRelated).mockRejectedValueOnce(new Error("down"));
    vi.mocked(relatedFacts).mockRejectedValueOnce(new Error("down"));
    await open();
    await userEvent.click(chip("rate limiting"));
    const pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    // Each line says why, not only that it failed.
    expect(await within(pop).findByText("Couldn't load the memories that mention it: down")).toBeInTheDocument();
    expect(await within(pop).findByText("Couldn't load neighbours: down")).toBeInTheDocument();
    expect(within(pop).queryByText("Mentioned only in this memory")).toBeNull();
    expect(within(pop).queryByText("None recorded")).toBeNull();
  });

  it("reads memory.related once for the memory, however many entity popovers open", async () => {
    await open();
    await userEvent.click(chip("rate limiting"));
    let pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    await within(pop).findByText("Mentioned in 3 memories");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Entity rate limiting" })).toBeNull());
    await userEvent.click(chip("public api"));
    pop = await screen.findByRole("dialog", { name: "Entity public api" });
    // Filtered for this entity from the same read: api/keys and runbooks/429 share it.
    await within(pop).findByText("Mentioned in 3 memories");
    expect(within(pop).getByRole("button", { name: "api/keys" })).toBeInTheDocument();
    expect(within(pop).queryByRole("button", { name: "api/quota" })).toBeNull();
    await userEvent.keyboard("{Escape}");
    await userEvent.click(chip("rate limiting"));
    await within(await screen.findByRole("dialog", { name: "Entity rate limiting" })).findByText(
      "Mentioned in 3 memories",
    );
    expect(vi.mocked(memoryRelated).mock.calls.filter((c) => c[2] === 100)).toHaveLength(1);
  });

  it("a mentioned memory swaps the drawer in place", async () => {
    await open();
    await userEvent.click(chip("rate limiting"));
    const pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    await userEvent.click(await within(pop).findByRole("button", { name: "api/quota" }));
    expect(useStore.getState().inspectedMemoryKey).toBe("api/quota");
    expect(useStore.getState().view).toBe("memories");
    // The open memory is the page's URL, without a new entry.
    expect(new URLSearchParams(location.search).get("memory")).toBe("api/quota");
  });

  it("Escape closes the popover, gives the focus back to the chip and leaves the drawer open", async () => {
    await open();
    await userEvent.click(chip("rate limiting"));
    await screen.findByRole("dialog", { name: "Entity rate limiting" });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Entity rate limiting" })).toBeNull());
    expect(document.activeElement).toBe(chip("rate limiting"));
    expect(screen.getByRole("dialog", { name: "api/rate-limit" })).toBeInTheDocument();
  });

  it("Open in Graph pushes the walk, remembering the memory and the chip for Back", async () => {
    await open();
    const before = history.length;
    await userEvent.click(chip("rate limiting"));
    const pop = await screen.findByRole("dialog", { name: "Entity rate limiting" });
    history.replaceState(history.state, "", "/memories?memory=api%2Frate-limit");
    const leaving = { state: null as unknown };
    const push = history.pushState.bind(history);
    const spy = vi.spyOn(history, "pushState").mockImplementation((data, unused, url) => {
      leaving.state = history.state;
      push(data, unused, url);
    });
    await userEvent.click(within(pop).getByRole("link", { name: "Open in Graph" }));
    spy.mockRestore();
    expect(history.length).toBe(before + 1);
    expect(location.pathname + location.search).toBe("/graph?namespace=ns&seed=rate+limiting");
    expect(useStore.getState().view).toBe("graph");
    expect(readBackLabel(history.state)).toBe("memory api/rate-limit");
    expect(readPlace(leaving.state)).toMatchObject({
      inspected: "api/rate-limit",
      focusKey: "entity:rate limiting",
    });
  });

  it("already on the Graph, Open in Graph walks there instead", async () => {
    const walk = vi.fn();
    await open({ onWalkEntity: walk });
    await userEvent.click(chip("public api"));
    const pop = await screen.findByRole("dialog", { name: "Entity public api" });
    await userEvent.click(within(pop).getByRole("link", { name: "Open in Graph" }));
    expect(walk).toHaveBeenCalledWith("public api");
    expect(useStore.getState().view).toBe("memories");
  });

  it("a cmd- or ctrl-click keeps the anchor's new tab and opens no popover", async () => {
    await open();
    const user = userEvent.setup();
    await user.keyboard("{Control>}");
    await user.click(chip("rate limiting"));
    await user.keyboard("{/Control}");
    expect(screen.queryByRole("dialog", { name: "Entity rate limiting" })).toBeNull();
    expect(chip("rate limiting")).toHaveAttribute("href", "/graph?namespace=ns&seed=rate+limiting");
  });
});

describe("the drawer's provenance and source conversation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    sessionStorage.clear();
    vi.mocked(getMemory).mockResolvedValue(DETAIL);
    history.replaceState(null, "", "/memories?memory=api%2Frate-limit");
    useStore.setState({ authed: true, whoami: writer, myNamespaces: mine, identity, view: "memories" });
  });
  afterEach(() => cleanup());

  it("a link's direction is an icon with words for a screen reader, not an arrow glyph", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Provenance/ }));
    const out = await screen.findByRole("button", { name: /api\/limits-v1/ });
    expect(out).toHaveTextContent("Links to");
    expect(out.textContent).not.toMatch(/[→←]/);
    expect(screen.getByRole("button", { name: /api\/limits-v3/ })).toHaveTextContent("Linked from");
  });

  it("View source conversation replays the thread over the drawer, with a link to Conversations", async () => {
    await open();
    await userEvent.click(screen.getByRole("button", { name: /Provenance/ }));
    await userEvent.click(await screen.findByRole("button", { name: "View source conversation" }));
    const replay = await screen.findByRole("dialog", { name: "Rate limits chat" });
    expect(await within(replay).findByText("what is our rate limit?")).toBeInTheDocument();
    expect(getConversationThread).toHaveBeenCalledWith({ org: "dev", namespace: "ns" }, "*", "t-42", expect.anything());
    // The drawer is still there under it (hidden from AT while the replay is modal), and
    // the view did not change.
    expect(screen.getByText("the public API allows 100 requests a minute")).toBeInTheDocument();
    expect(useStore.getState().view).toBe("memories");
    const link = within(replay).getByRole("link", { name: "Open in Conversations" });
    expect(link).toHaveAttribute("href", "/conversations?namespace=ns&thread=t-42");
    await userEvent.click(link);
    expect(useStore.getState().view).toBe("sessions");
    expect(location.pathname + location.search).toBe("/conversations?namespace=ns&thread=t-42");
  });
});

describe("the drawer footer: one row, two on a narrow drawer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    sessionStorage.clear();
    vi.mocked(getMemory).mockResolvedValue({ ...DETAIL, verification: "human", verified_by: "alice" });
    useStore.setState({ authed: true, whoami: writer, myNamespaces: mine, identity, view: "memories" });
  });
  afterEach(() => cleanup());

  it("keeps Edit and Delete, and folds a writer's other actions into More, an admin's among them unavailable", async () => {
    await open();
    const footer = screen.getByRole("contentinfo");
    expect(
      within(footer)
        .getAllByRole("button")
        .map((b) => b.getAttribute("aria-label") || b.textContent),
    ).toEqual(["Edit", "Ask the assistant about this memory", "More actions", "Delete"]);
    expect(footer).toHaveTextContent("Verified by alice");
    await userEvent.click(within(footer).getByRole("button", { name: "More actions" }));
    const menu = await screen.findByRole("menu", { name: "More actions" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((i) => i.textContent),
    ).toEqual([
      "Pin",
      "Remove verification",
      "Copy to…",
      "Lock…",
      "Share with every agent",
      "Stop sharing",
      "Rename",
      "Copy",
      "This helped",
      "This misled",
      "Mark as wrong…",
    ]);
  });
});
