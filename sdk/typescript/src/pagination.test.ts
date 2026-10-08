import { describe, expect, it } from "vitest";
import { collect, paginate } from "./pagination.js";

describe("paginate", () => {
  it("follows next_cursor until the server stops sending one", async () => {
    const pages: Record<string, { rows: number[]; next_cursor: string | null }> = {
      start: { rows: [1, 2], next_cursor: "c2" },
      c2: { rows: [3], next_cursor: "c3" },
      c3: { rows: [4], next_cursor: null },
    };
    const seen: Array<string | undefined> = [];
    const out = await collect(
      paginate(async (cur) => {
        seen.push(cur);
        return pages[cur ?? "start"];
      }),
    );
    expect(out).toEqual([1, 2, 3, 4]);
    expect(seen).toEqual([undefined, "c2", "c3"]);
  });
  it("collects nothing, and fetches nothing, for a limit of 0", async () => {
    let pages = 0;
    const gen = paginate(async () => {
      pages++;
      return { rows: [1, 2, 3] };
    });
    expect(await collect(gen, 0)).toEqual([]);
    expect(pages).toBe(0);
  });

  it("stops on a repeated cursor and honours maxPages / limit", async () => {
    const out = await collect(paginate(async () => ({ results: ["a"], next_cursor: "same" }), { maxPages: 3 }));
    expect(out).toEqual(["a", "a"]);
    const two = await collect(
      paginate(async () => ({ results: ["a", "b", "c"], next_cursor: null })),
      2,
    );
    expect(two).toEqual(["a", "b"]);
  });
});
