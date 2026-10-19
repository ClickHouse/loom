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

  it("cuts by characters, never inside one", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => Response.json({ runs: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    await listRunsPage({ org: "dev", namespace: "team" }, { q: "😀".repeat(RUN_LIST_MAX_Q + 50) });
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body)) as { q: string };
    expect(Array.from(body.q)).toHaveLength(RUN_LIST_MAX_Q);
    expect(body.q).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
  });

  // A full later page has no total and still pages; a server that ignores `offset` does not.
  it("tells a full later page from a server that does not page", async () => {
    const answer = (out: object) =>
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => Response.json(out)),
      );
    const id = { org: "dev", namespace: "team" };
    answer({ runs: [] });
    expect(await listRunsPage(id, { offset: 50 })).toMatchObject({ total: null, paged: true });
    answer({ runs: [], ignored_args: ["offset", "order_by"] });
    expect(await listRunsPage(id, { offset: 50 })).toMatchObject({ total: null, paged: false });
    answer({ runs: [] });
    expect(await listRunsPage(id, {})).toMatchObject({ total: null, paged: false });
    answer({ runs: [], total: 0 });
    expect(await listRunsPage(id, {})).toMatchObject({ total: 0, paged: true });
  });
});
