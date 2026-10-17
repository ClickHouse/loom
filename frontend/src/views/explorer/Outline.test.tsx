import { render, screen } from "../../test/rtl";
import { describe, expect, it } from "vitest";
import { Outline } from "./Outline";

// The walk's outline is a tree described by both lines above it: the visible counts
// summary and the one line naming its keys.

describe("Outline", () => {
  it("describes the tree by its counts summary and its keys", () => {
    render(
      <Outline
        model={{ seed: "loom", seedCommunityId: null, nodes: new Map(), truncated: false }}
        visible={{ nodes: [], hiddenUnder: new Map(), filteredOut: 0 }}
        children={new Map()}
        order={[]}
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
      />,
    );
    const tree = screen.getByRole("tree", { name: "Walk outline" });
    expect(tree).toHaveAccessibleDescription(/^1 node · 0 edges Arrow keys move and fold/);
  });
});
