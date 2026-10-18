import { afterEach, describe, expect, it, vi } from "vitest";
import { listMemories } from "./api";

// Profile sections have their own view, so the generic Memories list drops them unless asked.
describe("listMemories", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const id = { org: "o", namespace: "n", agent: "loom-ui" };

  function stub(answer: Record<string, unknown>) {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify(answer), { status: 200 });
      }),
    );
    return bodies;
  }

  const entries = [
    { memory_key: "a", memory_type: "semantic" },
    { memory_key: "p1", memory_type: "profile" },
    { memory_key: "b", memory_type: "episodic" },
    { memory_key: "p2", memory_type: "profile" },
    { memory_key: "c" },
  ];

  it("drops profile rows by default, keeping the others in order, and asks for every agent", async () => {
    const bodies = stub({ entries, truncated: false, total: 5, today: 0 });
    const out = await listMemories(id);
    expect(out.entries.map((e) => e.memory_key)).toEqual(["a", "b", "c"]);
    expect(out.total).toBe(5);
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toMatchObject({ org: "o", namespace: "n", agent: "*", limit: 200, include_tombstoned: false });
    expect(bodies[0]).not.toHaveProperty("include_profile");
  });

  it("drops profile rows when include_profile is false", async () => {
    const bodies = stub({ entries, truncated: false, total: 5, today: 0 });
    const out = await listMemories(id, 50, { include_profile: false });
    expect(out.entries.map((e) => e.memory_key)).toEqual(["a", "b", "c"]);
    expect(bodies[0]).toMatchObject({ agent: "*", limit: 50 });
    expect(bodies[0]).not.toHaveProperty("include_profile");
  });

  it("keeps profile rows when include_profile is true", async () => {
    const bodies = stub({ entries, truncated: false, total: 5, today: 0 });
    const out = await listMemories(id, 200, { include_profile: true });
    expect(out.entries.map((e) => e.memory_key)).toEqual(["a", "p1", "b", "p2", "c"]);
    expect(bodies[0]).not.toHaveProperty("include_profile");
  });

  it("answers an empty list when the response carries no entries", async () => {
    stub({ unchanged: true, truncated: false });
    const out = await listMemories(id, 200, { known_version: "42" });
    expect(out.entries).toEqual([]);
    expect(out.unchanged).toBe(true);
  });

  // Stacked tags must all match (a build that predates `tags_every` matches any one, and the
  // page then requires every tag itself).
  it("asks for the tags as typed, every one when there are several", async () => {
    const bodies = stub({ entries: [], truncated: false, total: 0, today: 0 });
    await listMemories(id, 50, { tags: ["API"] });
    await listMemories(id, 50, { tags: ["API", "beta"] });
    expect(bodies[0]).toMatchObject({ tags: ["API"] });
    expect(bodies[0]).not.toHaveProperty("tags_every");
    expect(bodies[1]).toMatchObject({ tags: ["API", "beta"], tags_every: true });
  });
});
