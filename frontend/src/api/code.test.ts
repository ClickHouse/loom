import { afterEach, describe, expect, it, vi } from "vitest";

// The dashboard renders only the code results, so its code reads ask for no lessons.
const post = vi.hoisted(() => vi.fn());
vi.mock("../api", async (importOriginal) => ({ ...(await importOriginal<typeof import("../api")>()), post }));
import { codeCallees, codeCallers, codeSearch } from "./code";

const id = { org: "acme", namespace: "code-repo" };

afterEach(() => vi.clearAllMocks());

describe("code client", () => {
  it("asks search, callers and callees for no lessons", async () => {
    post.mockResolvedValue({});
    await codeSearch(id, "q");
    await codeCallers(id, "f");
    await codeCallees(id, "f");
    expect(post.mock.calls.map(([op, body]) => [op, body.include_lessons])).toEqual([
      ["/v1/code.search", false],
      ["/v1/code.callers", false],
      ["/v1/code.callees", false],
    ]);
  });
});
