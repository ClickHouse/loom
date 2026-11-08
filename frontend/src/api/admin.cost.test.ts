import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The cost clients' "last N days" is N UTC dates, today included.
const post = vi.hoisted(() => vi.fn(async () => ({ rows: [] })));
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), post }));
import { getAgentSpend, listCostRollup } from "./admin";

const id = { org: "acme", namespace: "team" };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2000-01-10T12:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("cost windows", () => {
  it("asks for seven dates, today included, for the last 7 days", async () => {
    await listCostRollup(id, { days: 7 });
    await getAgentSpend(id, 7);
    for (const [, body] of post.mock.calls as unknown as [string, Record<string, unknown>][]) {
      expect(body).toEqual(expect.objectContaining({ from_date: "2000-01-04", to_date: "2000-01-10" }));
    }
    expect(post).toHaveBeenCalledTimes(2);
  });
});
