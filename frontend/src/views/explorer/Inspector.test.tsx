import { act, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../test/axe";
import { listFacts, searchMemory } from "../../api";
import { Inspector } from "./Inspector";
import { fromTraverse } from "./model";
import { expandNotice } from "./notices";

// The inspector carries one polite live region for the last expand's result. It is
// present (empty) before there is anything to say, it reads the line under the node it is
// about, it names the node when another is selected, and the pane passes axe with it
// showing.

const facts: { rows: unknown[] } = { rows: [] };
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listFacts: vi.fn(async () => facts),
    searchMemory: vi.fn(async () => ({ results: [], embedding_model: "test" })),
  };
});

const identity = { org: "dev", namespace: "bench", agent: "*" };
const model = fromTraverse({
  seed: "logical",
  seed_community_id: null,
  truncated: false,
  nodes: [
    {
      entity: "mergetree",
      hop: 1,
      cumulative_weight: 0,
      via_co_occur: 12,
      via_relation: "",
      via_direction: "",
      community_id: null,
      path: ["logical", "mergetree"],
    },
    {
      entity: "keeper",
      hop: 1,
      cumulative_weight: 0,
      via_co_occur: 4,
      via_relation: "",
      via_direction: "",
      community_id: null,
      path: ["logical", "keeper"],
    },
  ],
});

function inspector(props: Partial<Parameters<typeof Inspector>[0]> = {}) {
  return (
    <Inspector
      identity={identity}
      model={model}
      selection={{ kind: "node", entity: "mergetree" }}
      taxonomy={null}
      hasCommunities={false}
      communityLabel={() => "unclustered"}
      asOf=""
      typed={false}
      hops={2}
      provenance={new Set()}
      memoryTypes={new Set()}
      tags={new Set()}
      pinned={new Set()}
      collapsed={new Set()}
      hasChildren={() => false}
      expanding={null}
      onWalk={() => undefined}
      onPath={() => undefined}
      onSelect={() => undefined}
      onExpand={() => undefined}
      onPin={() => undefined}
      onCollapse={() => undefined}
      onOpenMemory={() => undefined}
      {...props}
    />
  );
}

function mount(props: Partial<Parameters<typeof Inspector>[0]> = {}) {
  return render(inspector(props));
}

/** The notice region: the pane's loaders are status regions too, but not polite live ones. */
function noticeRegion() {
  const region = screen.getAllByRole("status").find((el) => el.getAttribute("aria-live") === "polite");
  if (!region) throw new Error("no notice region");
  return region;
}

describe("Inspector notice region", () => {
  it("exists, empty, before any expand", () => {
    mount();
    const region = noticeRegion();
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("");
  });

  it("reads a result that changed nothing under the selected node, and passes axe", async () => {
    const { container } = mount({ notice: expandNotice({ entity: "mergetree", added: 0, known: 3, capped: false }) });
    expect(noticeRegion()).toHaveTextContent("Expanded: 0 new within one hop · 3 already shown");
    expect(noticeRegion().textContent).not.toContain("mergetree");
    expect(screen.getByRole("complementary", { name: "Inspector" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("names the node when the selection is elsewhere or empty", async () => {
    const notice = expandNotice({ entity: "keeper", added: 7, known: 0, capped: false });
    const { container, unmount } = mount({ notice });
    expect(noticeRegion()).toHaveTextContent("keeper — Expanded: +7");
    expect(await axeViolations(container)).toEqual([]);
    unmount();
    const empty = mount({ notice, selection: null });
    expect(noticeRegion()).toHaveTextContent("keeper — Expanded: +7");
    expect(screen.getByRole("heading", { name: "Reading the graph" })).toBeInTheDocument();
    expect(await axeViolations(empty.container)).toEqual([]);
  });

  it("a failed expand is read in the danger tone", () => {
    mount({ notice: { entity: "mergetree", line: "Expand failed: offline", toast: "x", tone: "error" } });
    const region = noticeRegion();
    expect(region).toHaveTextContent("Expand failed: offline");
    expect(region.className).toContain("text-danger");
  });
});

describe("Inspector links", () => {
  it("draws its in-text actions as Click UI links at the pane's 12 px", async () => {
    const onSelect = vi.fn();
    mount({ selection: { kind: "node", entity: "mergetree" }, onSelect });
    const reached = screen.getAllByRole("button", { name: "co-mentioned 12× with logical" });
    for (const b of reached) expect(b.className).toMatch(/\blink_size_sm/);
    reached[0].click();
    expect(onSelect).toHaveBeenCalled();
  });
});

// A list of more than 20 shows the first 20 and "Show all N": an edge reads
// its rows in both directions, up to 24.
describe("Inspector lists", () => {
  it("an edge's rows over 20 show the first 20 and Show all N", async () => {
    // Each direction answers its own rows, as the server does, so no row is drawn twice under
    // one key.
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(listFacts).mockImplementation(async (_id, opts = {}) => ({
      rows: Array.from({ length: 12 }, (_, i) => ({
        subject: opts.subject ?? "logical",
        relation: "mentions",
        object: `${opts.object ?? "mergetree"}-${i}`,
        co_occur: 1,
        provenance: "",
        last_seen: "",
        stale: false,
        source_keys: [],
        sources: [],
      })),
      truncated: false,
    }));
    const view = mount({ selection: { kind: "edge", parent: "logical", child: "mergetree" } });
    // Both directions: 24 rows.
    await waitFor(() => expect(screen.getByRole("button", { name: "Show all 24" })).toBeInTheDocument(), {
      timeout: 2000,
    });
    const list = () => screen.getAllByRole("listitem").filter((li) => li.textContent?.includes("mentions"));
    expect(list()).toHaveLength(20);
    await userEvent.click(screen.getByRole("button", { name: "Show all 24" }));
    expect(list()).toHaveLength(24);
    // The button went with the cap: focus is in the first row it hid.
    await waitFor(() => expect(list()[20].contains(document.activeElement)).toBe(true));
    // Another edge starts capped again.
    view.rerender(inspector({ selection: { kind: "edge", parent: "logical", child: "keeper" } }));
    await waitFor(() => expect(screen.getByText(/keeper-0/)).toBeInTheDocument(), { timeout: 2000 });
    expect(list()).toHaveLength(20);
    expect(screen.getByRole("button", { name: "Show all 24" })).toBeInTheDocument();
    expect(error.mock.calls.filter((c) => String(c[0]).includes("same key"))).toEqual([]);
    error.mockRestore();
    vi.mocked(listFacts).mockImplementation(async () => facts as never);
  });
});

describe("Inspector hints", () => {
  it("says how each typed fact was found, in the words its badge uses", async () => {
    mount();
    const about = await screen.findByRole("button", { name: "About Typed facts" }, { timeout: 2000 });
    act(() => about.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "with how each was found (extracted, inferred or from a facet), when it was last seen and the source rows behind it.",
    );
  });
});

describe("Inspector failed reads", () => {
  it("says the facts and the memories failed to load, never that there are none, and Retry reads again", async () => {
    vi.mocked(listFacts).mockRejectedValueOnce(new Error("down"));
    vi.mocked(searchMemory).mockRejectedValueOnce(new Error("down"));
    mount();
    expect(await screen.findByText("Couldn't load the facts", {}, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.getByText("Couldn't load the memories")).toBeInTheDocument();
    expect(screen.queryByText(/No typed fact names this entity/)).toBeNull();
    const before = vi.mocked(listFacts).mock.calls.length;
    screen.getAllByRole("button", { name: "Retry" })[0].click();
    await waitFor(() => expect(vi.mocked(listFacts).mock.calls.length).toBeGreaterThan(before), { timeout: 2000 });
    await waitFor(() => expect(screen.queryByText("Couldn't load the facts")).toBeNull());
  });

  it("reads the selection again in another namespace, keeping nothing from the last one", async () => {
    const before = vi.mocked(listFacts).mock.calls.length;
    const view = mount();
    // This mount's own read, settled, before the switch.
    await waitFor(() => expect(vi.mocked(listFacts).mock.calls.length).toBeGreaterThan(before), { timeout: 2000 });
    expect(vi.mocked(listFacts).mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "bench" });
    expect(await screen.findByText(/No typed fact names this entity/)).toBeInTheDocument();
    const read = vi.mocked(listFacts).mock.calls.length;
    view.rerender(inspector({ identity: { ...identity, namespace: "other" } }));
    // The last namespace's answer goes; the panel reads the new one.
    await waitFor(() => expect(screen.queryByText(/No typed fact names this entity/)).toBeNull());
    await waitFor(() => expect(vi.mocked(listFacts).mock.calls.length).toBeGreaterThan(read), { timeout: 2000 });
    expect(vi.mocked(listFacts).mock.calls.at(-1)?.[0]).toMatchObject({ namespace: "other" });
  });

  it("reads again when a new model lands while the selection's read is out", async () => {
    vi.mocked(listFacts).mockImplementationOnce(
      (_id, opts) =>
        new Promise((_, reject) =>
          opts?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
        ),
    );
    const before = vi.mocked(listFacts).mock.calls.length;
    const view = mount();
    await waitFor(() => expect(vi.mocked(listFacts).mock.calls.length).toBe(before + 1), { timeout: 2000 });
    view.rerender(inspector({ model: { ...model, nodes: new Map(model.nodes) } }));
    expect(await screen.findByText(/No typed fact names this entity/, {}, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.queryByText("Loading facts")).toBeNull();
  });

  it("keeps a failed read's error when an Expand or a re-walk hands it a new model for the same selection", async () => {
    vi.mocked(listFacts).mockRejectedValueOnce(new Error("down"));
    const view = mount();
    expect(await screen.findByText("Couldn't load the facts", {}, { timeout: 2000 })).toBeInTheDocument();
    view.rerender(inspector({ model: { ...model, nodes: new Map(model.nodes) } }));
    // Past the panel's 250 ms beat before a read, so a read it started would have begun.
    await new Promise((r) => setTimeout(r, 300));
    expect(screen.getByText("Couldn't load the facts")).toBeInTheDocument();
    expect(screen.queryByText("Loading facts")).toBeNull();
  });
});
