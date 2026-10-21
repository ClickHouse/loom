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

// The Auto-resolved tab reads `memory.auto_retirements` (one row per retired memory) and
// `memory.unsupersede` audit rows (one per restore), the second flattened client-side.

// Pages of rows (`memory.auto_retirements`, `memory.statuses`, audit), answered in order.
function stubAudit(pages: Array<{ rows: Array<Record<string, unknown>>; next_cursor?: string | null }>) {
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

// A `memory.auto_retirements` row: the op flattens and scopes the records server-side.
const retirement = (id: string, extra: Record<string, unknown> = {}) => ({
  ts: "2026-10-02T00:00:00Z",
  reason: "conflict_resolver",
  memory_id: id,
  memory_key: `key_${id}`,
  agent: "a",
  user_id: "u",
  session_id: "s",
  superseded_by: `new_${id}`,
  superseded_by_key: "",
  label: "",
  unconfirmed: false,
  judge_verdict: "",
  judge_confidence: null,
  judge_model: "",
  judge_prompt_version: "",
  ...extra,
});

describe("listAutoResolved", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads memory.auto_retirements for the last 30 days, empty optional fields absent", async () => {
    const bodies = stubAudit([
      {
        rows: [
          retirement("a", { label: "update", superseded_by_key: "k" }),
          retirement("b", { unconfirmed: true, reason: "newest_wins_contradiction" }),
        ],
        next_cursor: "c2",
      },
      { rows: [] },
    ]);
    const out = await listAutoResolved({ org: "o", namespace: "n" });
    expect(out.next_cursor).toBe("c2");
    expect(out.rows.map((r) => [r.memory_id, r.reason, r.ts])).toEqual([
      ["a", "conflict_resolver", "2026-10-02T00:00:00Z"],
      ["b", "newest_wins_contradiction", "2026-10-02T00:00:00Z"],
    ]);
    expect(out.rows[0]).toMatchObject({ label: "update", superseded_by_key: "k", unconfirmed: undefined });
    expect(out.rows[1]).toMatchObject({ label: undefined, superseded_by_key: undefined, unconfirmed: true });
    const body = bodies[0] ?? {};
    expect(body).toMatchObject({ org: "o", namespace: "n", limit: 50 });
    expect(body.cursor).toBeUndefined();
    const since = Date.parse(String(body.since));
    expect(Date.now() - since).toBeGreaterThan(29 * 86_400_000);
    expect(Date.now() - since).toBeLessThan(31 * 86_400_000);
  });

  it("attaches each row's state now from memory.statuses", async () => {
    const bodies = stubAudit([
      { rows: [retirement("a"), retirement("b")] },
      { rows: [{ memory_id: "a", status: "superseded", superseded_by: "new_a" }] },
    ]);
    const out = await listAutoResolved({ org: "o", namespace: "n" });
    expect(bodies[1]).toMatchObject({ org: "o", namespace: "n", memory_ids: ["a", "b"] });
    expect(out.rows.map((r) => r.current)).toEqual([{ status: "superseded", superseded_by: "new_a" }, undefined]);
  });

  it("leaves the state unknown when memory.statuses fails", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        if (call === 1) {
          const body = JSON.stringify({ rows: [retirement("a")] });
          return new Response(body, { status: 200, headers: { "Content-Type": "application/json" } });
        }
        return new Response("{}", { status: 500 });
      }),
    );
    const out = await listAutoResolved({ org: "o", namespace: "n" });
    expect(out.rows).toHaveLength(1);
    expect(out.rows[0]?.current).toBeUndefined();
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

  it("reads the newest five and answers whether any row came back", async () => {
    const bodies = stubAudit([{ rows: [retirement("a")] }, { rows: [], next_cursor: "c2" }]);
    expect(await hasAutoResolved({ org: "o", namespace: "n" })).toBe(true);
    expect(await hasAutoResolved({ org: "o", namespace: "n" })).toBe(false);
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toMatchObject({ org: "o", namespace: "n", limit: 50 });
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

  it("reads a time without an offset as UTC, whatever the browser's zone", async () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    try {
      stubAudit([
        {
          rows: [
            { ts: "2026-10-03 00:00:00", subject: JSON.stringify({ memory_id: "m1" }) },
            { ts: "2026-10-03T03:00:00Z", subject: JSON.stringify({ memory_id: "m1" }) },
          ],
        },
      ]);
      const out = await listUnsuperseded({ org: "o", namespace: "n" }, { since: "2026-09-01T00:00:00Z" });
      expect(out.get("m1")).toBe("2026-10-03T03:00:00Z");
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("keeps the later restore of one second, written with and without a fraction", async () => {
    stubAudit([
      {
        rows: [
          { ts: "2026-10-01T12:00:00.400000Z", subject: JSON.stringify({ memory_id: "m1" }) },
          { ts: "2026-10-01T12:00:00Z", subject: JSON.stringify({ memory_id: "m1" }) },
        ],
      },
    ]);
    const out = await listUnsuperseded({ org: "o", namespace: "n" }, { since: "2026-09-01T00:00:00Z" });
    expect(out.get("m1")).toBe("2026-10-01T12:00:00.400000Z");
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
  it("has one wording per reason", () => {
    expect(autoResolveReasonText("conflict_resolver")).toBe("Replaced by a later statement of the same attribute");
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
