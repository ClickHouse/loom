import { render, screen } from "../../test/rtl";
import { describe, expect, it, vi } from "vitest";
import { axeViolations } from "../../test/axe";
import { Inspector } from "./Inspector";
import { fromTraverse } from "./model";
import { expandNotice } from "./notices";

// The inspector carries one polite live region for the last expand's result. It is
// present (empty) before there is anything to say, it reads the line under the node it is
// about, it names the node when another is selected, and the pane passes axe with it
// showing.

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listFacts: vi.fn(async () => ({ rows: [] })),
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

function mount(props: Partial<Parameters<typeof Inspector>[0]> = {}) {
  return render(
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
    />,
  );
}

describe("Inspector notice region", () => {
  it("exists, empty, before any expand", () => {
    mount();
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("");
  });

  it("reads a result that changed nothing under the selected node, and passes axe", async () => {
    const { container } = mount({ notice: expandNotice({ entity: "mergetree", added: 0, known: 3, capped: false }) });
    expect(screen.getByRole("status")).toHaveTextContent("Expanded: 0 new within one hop · 3 already shown");
    expect(screen.getByRole("status").textContent).not.toContain("mergetree");
    expect(screen.getByRole("complementary", { name: "Inspector" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("names the node when the selection is elsewhere or empty", async () => {
    const notice = expandNotice({ entity: "keeper", added: 7, known: 0, capped: false });
    const { container, unmount } = mount({ notice });
    expect(screen.getByRole("status")).toHaveTextContent("keeper — Expanded: +7");
    expect(await axeViolations(container)).toEqual([]);
    unmount();
    const empty = mount({ notice, selection: null });
    expect(screen.getByRole("status")).toHaveTextContent("keeper — Expanded: +7");
    expect(screen.getByRole("heading", { name: "Reading the graph" })).toBeInTheDocument();
    expect(await axeViolations(empty.container)).toEqual([]);
  });

  it("a failed expand is read in the danger tone", () => {
    mount({ notice: { entity: "mergetree", line: "Expand failed: offline", toast: "x", tone: "error" } });
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("Expand failed: offline");
    expect(region.className).toContain("text-danger");
  });
});
