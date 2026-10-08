import { afterEach, describe, expect, it, vi } from "vitest";
import { listCostRollup } from "./api";
import { isRecord } from "./lib/guards";

// The Usage page renders no cache savings, so its cost.summary call says so — otherwise
// the server's cache statement scans the window's args map for a figure nobody reads.
describe("listCostRollup", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks for no cache figures unless told to", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ rows: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    await listCostRollup({ org: "o", namespace: "n" }, { days: 7 });
    await listCostRollup({ org: "o", namespace: "n" }, { days: 7, includeCache: true });
    const [first, second] = bodies.map((b) => (isRecord(b) ? b : {}));
    expect(first.include_cache).toBe(false);
    expect(first.namespace).toBe("n");
    expect(first.group_by).toEqual(["day", "agent", "operation"]);
    expect(second.include_cache).toBe(true);
  });
});
