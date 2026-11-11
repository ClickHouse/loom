import { describe, expect, it } from "vitest";
import { PLANE_PAGES, planePage } from "./planes";

// Every plane mentor.brief reports (tests/unit/snapshots/mentor_planes.json, which the
// server's tests/unit/test_mentor_planes.py writes from mentor_rules.PLANES) is mapped to a
// page or explicitly to none: a plane the server adds fails here until it is mapped.
const snapshot = new URL("../../../../../tests/unit/snapshots/mentor_planes.json", import.meta.url);
const serverTree = new URL("../../../../../server/insights/app/mentor_rules.py", import.meta.url);

describe("the page each mentor plane links to", () => {
  it("maps every plane the server reports, and no other", async () => {
    // vitest runs on node; the frontend tsconfig carries no node types (lib/canCall.test.ts).
    // A URL, not its pathname: a checkout path with a space or a non-ASCII character stays a path.
    type Fs = { existsSync(p: URL): boolean; readFileSync(p: URL, enc: "utf8"): string };
    const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as Fs;
    // A frontend-only checkout has no server tree, so no snapshot; with the server tree
    // beside it, a missing snapshot (moved, renamed) fails here rather than passing unseen.
    if (!fs.existsSync(snapshot)) {
      expect(fs.existsSync(serverTree)).toBe(false);
      return;
    }
    const { planes } = JSON.parse(fs.readFileSync(snapshot, "utf8")) as { planes: string[] };
    expect(Object.keys(PLANE_PAGES).sort()).toEqual([...planes].sort());
  });

  it("links an admin page only for a namespace admin, and nothing for a plane with no page", () => {
    expect(planePage("conflicts", false)).toBeNull();
    expect(planePage("conflicts", true)).toMatchObject({ view: "admin:review_queue", page: "Review queue" });
    expect(planePage("erasure", true)).toBeNull();
    expect(planePage("tracker", false)).toMatchObject({ view: "code", arriveFocus: "trackers-heading" });
    expect(planePage("claims", false)).toMatchObject({ view: "memories", page: "Memories" });
    // Typed records live on Settings' Structure tab.
    expect(planePage("records", false)).toMatchObject({ view: "setup", query: { tab: "structure" } });
    expect(planePage("scratchpad", true)).toBeNull();
    expect(planePage("a later plane", true)).toBeNull();
    expect(planePage(null, true)).toBeNull();
  });
});
