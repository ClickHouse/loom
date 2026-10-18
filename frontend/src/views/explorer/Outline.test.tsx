import { render, screen, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Outline, type OutlineProps } from "./Outline";
import type { XNode } from "./model";

// The walk's outline is a tree described by both lines above it: the visible counts
// summary and the one line naming its keys. Each row's entity name is its primary line at
// 14 px; under it, muted at 12 px, "hop 1 · co-mentioned 20×". A tree of more than 20 rows
// shows the first 20 and "Show all N".

function node(entity: string, over: Partial<XNode> = {}): XNode {
  return {
    entity,
    hop: 1,
    via_co_occur: 20,
    via_relation: "",
    via_direction: "",
    via_provenance: "",
    valid_from: "",
    valid_to: "",
    community_id: null,
    path: ["loom", entity],
    parent: "loom",
    origin: "walk",
    ...over,
  };
}

function outline(order: XNode[] = [], props: Partial<OutlineProps> = {}) {
  return (
    <Outline
      model={{ seed: "loom", seedCommunityId: null, nodes: new Map(order.map((n) => [n.entity, n])), truncated: false }}
      visible={{ nodes: order, hiddenUnder: new Map(), filteredOut: 0 }}
      children={new Map([["loom", order]])}
      order={order}
      collapsed={new Set()}
      pinned={new Set()}
      selection={null}
      typed={false}
      hasCommunities={false}
      communityLabel={() => ""}
      summary="1 node · 0 edges"
      expanding={null}
      onSelect={() => {}}
      onWalk={() => {}}
      onPath={() => {}}
      onExpand={() => {}}
      onCollapse={() => {}}
      onPin={() => {}}
      {...props}
    />
  );
}

function mount(order: XNode[] = [], props: Partial<OutlineProps> = {}) {
  return render(outline(order, props));
}

describe("Outline", () => {
  it("describes the tree by its counts summary and its keys", () => {
    mount();
    const tree = screen.getByRole("tree", { name: "Walk outline" });
    expect(tree).toHaveAccessibleDescription(/^1 node · 0 edges Arrow keys move and fold/);
  });

  it("a row: the entity name at 14 px, then 'hop 1 · co-mentioned 20×' muted at 12 px", () => {
    mount([node("mergetree")]);
    const row = screen.getByRole("treeitem", { name: /^mergetree ·/ });
    const name = within(row).getByText("mergetree");
    expect(name).toHaveClass("text-sm");
    expect(name).not.toHaveClass("font-mono");
    const meta = within(row).getByText(/^hop 1 · co-mentioned 20×/);
    expect(meta.closest(".text-xs")).toHaveClass("text-text-muted");
  });

  it("over 20 rows: the first 20 and Show all N, which shows the rest", async () => {
    const order = Array.from({ length: 30 }, (_, i) => node(`e${i}`));
    mount(order);
    expect(screen.getAllByRole("treeitem")).toHaveLength(20);
    await userEvent.click(screen.getByRole("button", { name: "Show all 31" }));
    expect(screen.getAllByRole("treeitem")).toHaveLength(31);
    // The button went with the cap: focus is on the first row it hid.
    expect(screen.getAllByRole("treeitem")[20]).toHaveFocus();
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("20 rows or fewer show whole, and a selected row past the 20th shows them all", () => {
    const { unmount } = mount(Array.from({ length: 19 }, (_, i) => node(`e${i}`)));
    expect(screen.getAllByRole("treeitem")).toHaveLength(20);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    unmount();
    mount(
      Array.from({ length: 30 }, (_, i) => node(`e${i}`)),
      { selection: { kind: "node", entity: "e25" } },
    );
    expect(screen.getAllByRole("treeitem")).toHaveLength(31);
  });

  it("stays whole once the selection went past the 20th, when it moves back up", () => {
    const order = Array.from({ length: 30 }, (_, i) => node(`e${i}`));
    const view = mount(order, { selection: { kind: "node", entity: "e25" } });
    expect(screen.getAllByRole("treeitem")).toHaveLength(31);
    view.rerender(outline(order, { selection: { kind: "node", entity: "e3" } }));
    expect(screen.getAllByRole("treeitem")).toHaveLength(31);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  // Past the 20-row cap, the tree keeps its one tab stop: a move to a row not drawn draws it
  // and focuses it, and the cap never comes back over the focused row.
  it("draws and focuses a row past the cap that a key moves to", async () => {
    const user = userEvent.setup();
    const hop1 = Array.from({ length: 25 }, (_, i) => node(`n${i}`));
    const deep = node("deep", { hop: 2, parent: "n18", path: ["loom", "n18", "deep"] });
    const order = [...hop1, deep];
    mount(order, {
      children: new Map([
        ["loom", hop1],
        ["n18", [deep]],
      ]),
    });
    const tree = screen.getByRole("tree", { name: "Walk outline" });
    expect(within(tree).queryByRole("treeitem", { name: /^deep/ })).toBeNull();
    within(tree).getByRole("treeitem", { name: /^n18 / }).focus();
    await user.keyboard("{ArrowRight}");
    const row = await within(tree).findByRole("treeitem", { name: /^deep/ });
    expect(row).toHaveFocus();
    expect(row).toHaveAttribute("tabindex", "0");
  });
});
