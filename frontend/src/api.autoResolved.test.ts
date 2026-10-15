import { afterEach, describe, expect, it, vi } from "vitest";
import {
  UNSUPERSEDED_MAX_PAGES,
  autoResolveReasonText,
  hasAutoResolved,
  listAutoResolved,
  listUnsuperseded,
  restoreMemory,
} from "./api";
import { isRecord } from "./lib/guards";

// The Auto-resolved tab reads `memory.auto_supersede` audit rows (one per write and
// path, each listing up to 200 retired rows) and `memory.unsupersede` rows (one per
// restore). Both are flattened client-side.

function stubAudit(pages: Array<{ rows: Array<{ ts: string; subject: string }>; next_cursor?: string | null }>) {
  const bodies: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      const body: unknown = JSON.parse(String(init?.body));
      bodies.push(isRecord(body) ? body : {});
      const next = pages.shift() ?? { rows: [] };
      return new Response(JSON.stringify(next), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return bodies;
}

const entry = (id: string, extra: Record<string, unknown> = {}) => ({
  memory_id: id,
  memory_key: `key_${id}`,
  agent: "a",
  user_id: "u",
  session_id: "s",
  superseded_by: `new_${id}`,
  ...extra,
});

describe("listAutoResolved", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("flattens each audit row's retired list, newest row first, and skips malformed entries", async () => {
    const bodies = stubAudit([
      {
        rows: [
          {
            ts: "2026-10-02T00:00:00Z",
            subject: JSON.stringify({
              reason: "same_key_judge",
              count: 3,
              retired: [entry("a", { label: "elaboration", superseded_by_key: "k" }), { memory_key: "no_id" }, "x"],
            }),
          },
          { ts: "2026-10-01T00:00:00Z", subject: "{not json" },
          {
            ts: "2026-10-01T00:00:00Z",
            subject: JSON.stringify({
              reason: "refines",
              retired: [entry("b", { agent: "" }), entry("c", { unconfirmed: "true" })],
            }),
          },
          { ts: "2026-09-30T00:00:00Z", subject: JSON.stringify({ reason: "refines", retired: "nope" }) },
        ],
        next_cursor: "c2",
      },
    ]);
    const out = await listAutoResolved({ org: "o", namespace: "n" });
    expect(out.next_cursor).toBe("c2");
    expect(out.rows.map((r) => [r.memory_id, r.reason, r.ts])).toEqual([
      ["a", "same_key_judge", "2026-10-02T00:00:00Z"],
      ["b", "refines", "2026-10-01T00:00:00Z"],
      ["c", "refines", "2026-10-01T00:00:00Z"],
    ]);
    expect(out.rows.map((r) => r.unconfirmed)).toEqual([undefined, undefined, true]);
    expect(out.rows[0]).toMatchObject({
      label: "elaboration",
      superseded_by_key: "k",
      memory_key: "key_a",
      superseded_by: "new_a",
    });
    // An empty identity part reads as the sentinel the restore expects.
    expect(out.rows[1]?.agent).toBe("-");
    const body = bodies[0] ?? {};
    expect(body.event_type).toBe("memory.auto_supersede");
    expect(body.namespace).toBe("n");
    expect(body.cursor).toBeUndefined();
    // A 30-day bound by default.
    const since = Date.parse(String(body.since));
    expect(Date.now() - since).toBeGreaterThan(29 * 86_400_000);
    expect(Date.now() - since).toBeLessThan(31 * 86_400_000);
  });

  it("asks for the next page with the cursor", async () => {
    const bodies = stubAudit([{ rows: [], next_cursor: null }]);
    const out = await listAutoResolved({ org: "o", namespace: "n" }, { cursor: "c2" });
    expect(bodies[0]?.cursor).toBe("c2");
    expect(out).toEqual({ rows: [], next_cursor: null });
  });
});

describe("hasAutoResolved", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads one record and counts it only if it holds a retirement the tab can show", async () => {
    const shown = JSON.stringify({ reason: "refines", retired: [entry("a")] });
    const malformed = JSON.stringify({ reason: "refines", retired: [{ memory_key: "no_id" }] });
    const bodies = stubAudit([
      { rows: [{ ts: "2026-10-02T00:00:00Z", subject: shown }] },
      { rows: [{ ts: "2026-10-02T00:00:00Z", subject: malformed }] },
    ]);
    expect(await hasAutoResolved({ org: "o", namespace: "n" })).toBe(true);
    // The Auto-resolved tab drops an entry with no id or key: so does the sidebar.
    expect(await hasAutoResolved({ org: "o", namespace: "n" })).toBe(false);
    expect(bodies[0]).toMatchObject({
      org: "o",
      namespace: "n",
      event_type: "memory.auto_supersede",
      limit: 1,
      fields: ["ts", "subject"],
    });
    const since = Date.parse(String(bodies[0]?.["since"]));
    expect(Date.now() - since).toBeGreaterThan(29 * 24 * 3600 * 1000);
    expect(Date.now() - since).toBeLessThan(31 * 24 * 3600 * 1000);
  });
});

describe("listUnsuperseded", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the newest restore per memory id since the given time", async () => {
    const bodies = stubAudit([
      {
        rows: [
          { ts: "2026-10-01T00:00:00Z", subject: JSON.stringify({ memory_id: "m1" }) },
          { ts: "2026-10-03T00:00:00Z", subject: JSON.stringify({ memory_id: "m1" }) },
          { ts: "2026-10-02T00:00:00Z", subject: JSON.stringify({ memory_id: "m2" }) },
          { ts: "2026-10-02T00:00:00Z", subject: JSON.stringify({ memory_key: "no_id" }) },
          { ts: "2026-10-02T00:00:00Z", subject: "null" },
        ],
      },
    ]);
    const out = await listUnsuperseded({ org: "o", namespace: "n" }, { since: "2026-09-01T00:00:00Z" });
    expect([...out.entries()]).toEqual([
      ["m1", "2026-10-03T00:00:00Z"],
      ["m2", "2026-10-02T00:00:00Z"],
    ]);
    expect(bodies[0]).toMatchObject({ event_type: "memory.unsupersede", since: "2026-09-01T00:00:00Z" });
  });

  it("follows next_cursor, and stops after a bounded number of pages", async () => {
    const rec = (id: string, ts: string) => ({ ts, subject: JSON.stringify({ memory_id: id }) });
    let bodies = stubAudit([
      { rows: [rec("m1", "2026-10-03T00:00:00Z")], next_cursor: "p2" },
      { rows: [rec("m2", "2026-10-02T00:00:00Z")], next_cursor: null },
    ]);
    let out = await listUnsuperseded({ org: "o", namespace: "n" }, { since: "2026-09-01T00:00:00Z" });
    expect([...out.keys()]).toEqual(["m1", "m2"]);
    expect(bodies.map((b) => b.cursor)).toEqual([undefined, "p2"]);
    vi.unstubAllGlobals();

    bodies = stubAudit(
      Array.from({ length: 10 }, (_, i) => ({
        rows: [rec(`m${i}`, "2026-10-01T00:00:00Z")],
        next_cursor: `p${i + 1}`,
      })),
    );
    out = await listUnsuperseded({ org: "o", namespace: "n" }, { since: "2026-09-01T00:00:00Z" });
    expect(bodies).toHaveLength(UNSUPERSEDED_MAX_PAGES);
    expect(out.size).toBe(UNSUPERSEDED_MAX_PAGES);
  });
});

describe("autoResolveReasonText", () => {
  it("has one wording per reason, the judge's elaboration told apart", () => {
    expect(autoResolveReasonText("same_key_judge", "contradiction")).toBe(
      "Replaced by a later statement of the same attribute",
    );
    expect(autoResolveReasonText("same_key_judge", "elaboration")).toBe(
      "Replaced by a later, fuller statement of the same attribute",
    );
    expect(autoResolveReasonText("newest_wins_contradiction")).toBe(autoResolveReasonText("same_key_newest_wins"));
    expect(autoResolveReasonText("toString")).toBeNull();
  });
});

describe("restoreMemory", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends if_superseded_by only when set", async () => {
    const bodies = stubAudit([{ rows: [] }, { rows: [] }, { rows: [] }]);
    await restoreMemory({ org: "o", namespace: "n", agent: "a" }, "k", { if_superseded_by: "m-new" });
    await restoreMemory({ org: "o", namespace: "n", agent: "a" }, "k", { if_superseded_by: "" });
    await restoreMemory({ org: "o", namespace: "n", agent: "a" }, "k");
    expect(bodies[0]?.["if_superseded_by"]).toBe("m-new");
    expect(bodies[1]).not.toHaveProperty("if_superseded_by");
    expect(bodies[2]).not.toHaveProperty("if_superseded_by");
    for (const b of bodies) {
      expect(b).not.toHaveProperty("if_version");
      expect(b).not.toHaveProperty("memory_id");
    }
  });
});
