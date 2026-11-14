import { afterEach, describe, expect, it, vi } from "vitest";
import { NODE_CAP } from "./model";
import {
  absoluteUtc,
  communitiesCopy,
  COMMUNITIES_SHOWN,
  expandFailure,
  expandNotice,
  NO_COMMUNITIES_COPY,
} from "./notices";

// The words for a result are a pure function of the result, so the toast, the inspector
// line and the status line cannot drift apart.

describe("expandNotice", () => {
  it("says +N under the node when children were added", () => {
    const n = expandNotice({ entity: "logical", added: 7, known: 0, capped: false });
    expect(n.line).toBe("Expanded: +7");
    expect(n.toast).toBe("Expanded: +7 under logical");
    expect(n.tone).toBe("ok");
    expect(n.entity).toBe("logical");
  });

  it("a result that changed nothing still says so, with what was already shown", () => {
    const n = expandNotice({ entity: "logical", added: 0, known: 3, capped: false });
    expect(n.line).toBe("Expanded: 0 new within one hop · 3 already shown");
    expect(n.toast).toBe("Nothing new under logical within one hop · 3 already shown");
  });

  it("names the cap when it bit, with or without additions", () => {
    expect(expandNotice({ entity: "x", added: 0, known: 0, capped: true }).line).toBe(
      `Expanded: 0 new within one hop · strongest ${NODE_CAP} kept`,
    );
    expect(expandNotice({ entity: "x", added: 0, known: 0, capped: true }).toast).toBe(
      `No room under x · strongest ${NODE_CAP} kept`,
    );
    const some = expandNotice({ entity: "x", added: 4, known: 2, capped: true, cap: 50 });
    expect(some.line).toBe("Expanded: +4 · 2 already shown · strongest 50 kept");
    expect(some.toast).toBe("Expanded: +4 under x · 2 already shown · strongest 50 kept");
  });

  it("shortens a long entity in the toast but never in the line", () => {
    const long = "a-very-long-entity-name-that-goes-on-and-on";
    const n = expandNotice({ entity: long, added: 1, known: 0, capped: false });
    expect(n.toast).toContain("…");
    expect(n.toast.length).toBeLessThan(long.length + 20);
    expect(n.line).toBe("Expanded: +1");
  });

  it("a failure is an error-toned notice naming the node", () => {
    const n = expandFailure("beta", "server unreachable");
    expect(n.tone).toBe("error");
    expect(n.line).toBe("Expand failed: server unreachable");
    expect(n.toast).toBe("Expand of beta failed: server unreachable");
  });
});

describe("communitiesCopy", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("with none, is the full sentence", () => {
    expect(communitiesCopy(null)).toEqual({ kind: "none", line: NO_COMMUNITIES_COPY });
    expect(communitiesCopy({ communities: [], communities_computed_at: "" }).line).toBe(NO_COMMUNITIES_COPY);
    // The same facts as the Facets hint: only when an operator turned them on, every 6 hours,
    // for namespaces explored in the last 14 days.
    expect(NO_COMMUNITIES_COPY).toBe(
      "No communities yet. When an operator of this deployment has turned them on, they are computed every 6 hours for namespaces explored in the last 14 days; exploring this one counts.",
    );
  });

  it("with some, counts them and says when they were computed, relative and absolute", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-16T11:33:00Z"));
    const c = communitiesCopy({
      communities: [
        { community_id: 1, label: "a", size: 10, top_entities: [] },
        { community_id: 2, label: "b", size: 5, top_entities: [] },
      ],
      communities_computed_at: "2026-09-16 08:33:00",
    });
    expect(c.kind).toBe("some");
    if (c.kind !== "some") return;
    expect(c.line).toBe("2 communities · computed 3h ago");
    expect(c.absolute).toBe("2026-09-16 08:33 UTC");
  });

  it("at the requested count, says they are the largest, not all", () => {
    const comms = Array.from({ length: COMMUNITIES_SHOWN }, (_, i) => ({
      community_id: i,
      label: `c${i}`,
      size: 1,
      top_entities: [],
    }));
    const c = communitiesCopy({ communities: comms, communities_computed_at: "" });
    expect(c.kind).toBe("some");
    expect(c.line).toBe(`${COMMUNITIES_SHOWN} largest communities`);
    if (c.kind === "some") expect(c.absolute).toBeNull();
  });

  it("one community is singular", () => {
    expect(
      communitiesCopy({
        communities: [{ community_id: 1, label: "a", size: 1, top_entities: [] }],
        communities_computed_at: "",
      }).line,
    ).toBe("1 community");
  });

  it("absoluteUtc reads a CH datetime as UTC and rejects garbage", () => {
    expect(absoluteUtc("2026-09-15 08:33:12")).toBe("2026-09-15 08:33 UTC");
    expect(absoluteUtc("2026-09-15T08:33:12Z")).toBe("2026-09-15 08:33 UTC");
    expect(absoluteUtc("not a date")).toBeNull();
  });
});
