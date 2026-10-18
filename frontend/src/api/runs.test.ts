import { afterEach, describe, expect, it, vi } from "vitest";
import { listRunsPage, RUN_LIST_MAX_Q } from "./runs";

// run.list takes a search of at most RUN_LIST_MAX_Q characters: a longer one (a pasted
// goal) is cut to it, so the page shows results rather than a 422.

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listRunsPage", () => {
  it("cuts a search longer than run.list takes", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => Response.json({ runs: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    await listRunsPage({ org: "dev", namespace: "team" }, { q: `  ${"x".repeat(RUN_LIST_MAX_Q + 50)}  ` });
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body)) as { q: string };
    expect(body.q).toHaveLength(RUN_LIST_MAX_Q);
  });
});
