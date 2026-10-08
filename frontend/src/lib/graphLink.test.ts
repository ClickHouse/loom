import { beforeEach, describe, expect, it } from "vitest";
import { graphSeedHref, onGraphLinkClick, openGraphAt } from "./graphLink";
import { ALL_VIEWS, useStore } from "./state";
import { MAIN } from "../layout/Sidebar";

// The Graph left the sidebar and is reached from an entity on something already open; the
// route stays resolvable for deep links.

describe("graph as a drill-down", () => {
  beforeEach(() => {
    history.replaceState(null, "", "/me/memories?stray=1");
    useStore.setState({ view: "memories" });
  });

  it("is not a sidebar destination but still a resolvable view", () => {
    expect(MAIN.map((m) => m.id)).not.toContain("graph");
    expect(ALL_VIEWS).toContain("graph");
  });

  it("builds the seed walk href, lower-cased and encoded", () => {
    expect(graphSeedHref("ClickHouse Cloud")).toBe("/me/graph?seed=clickhouse+cloud");
    expect(graphSeedHref("  ")).toBe("/me/graph");
  });

  it("opens the Explorer on the entity in place, carrying only the seed", () => {
    openGraphAt("Alice");
    expect(useStore.getState().view).toBe("graph");
    expect(window.location.pathname).toBe("/me/graph");
    const q = new URLSearchParams(window.location.search);
    expect(q.get("seed")).toBe("alice");
    expect(q.get("stray")).toBeNull();
  });

  it("a modified click leaves the anchor to the browser", () => {
    let prevented = false;
    const handler = onGraphLinkClick("alice");
    handler({
      metaKey: true,
      ctrlKey: false,
      shiftKey: false,
      button: 0,
      preventDefault: () => {
        prevented = true;
      },
    });
    expect(prevented).toBe(false);
    expect(useStore.getState().view).toBe("memories");
    handler({
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      button: 0,
      preventDefault: () => {
        prevented = true;
      },
    });
    expect(prevented).toBe(true);
    expect(useStore.getState().view).toBe("graph");
  });
});
