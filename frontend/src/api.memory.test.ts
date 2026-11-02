import { afterEach, describe, expect, it, vi } from "vitest";
import { getMemoryById, unpinMemory } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("unpinMemory", () => {
  it("calls the explicit unpin operation instead of pinning the row again", async () => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(() => null),
      length: 0,
    });
    const fetchMock = vi.fn(
      async (..._args: Parameters<typeof fetch>): Promise<Response> =>
        new Response(JSON.stringify({ unpinned: true, reason: "", memory_id: "m1", version: 2 }), {
          status: 200,
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await unpinMemory({ org: "dev", namespace: "local", agent: "loom-ui" }, "k1");

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/memory.unpin");
    expect(JSON.parse(String(init?.body))).toEqual({
      consumer: "loom-ui",
      org: "dev",
      namespace: "local",
      agent: "loom-ui",
      memory_key: "k1",
    });
  });
});

describe("getMemoryById", () => {
  it("asks for a high row without its value when the caller only names it", async () => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(() => null),
      length: 0,
    });
    const fetchMock = vi.fn(
      async (..._args: Parameters<typeof fetch>): Promise<Response> =>
        new Response(JSON.stringify({ found: false }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getMemoryById("dev", "m1", { omit_high_value: true });

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/memory.get_by_id");
    expect(JSON.parse(String(init?.body))).toEqual({
      consumer: "loom-ui",
      org: "dev",
      memory_id: "m1",
      omit_high_value: true,
    });
  });
});

describe("the dashboard's consumer", () => {
  it("names every call the dashboard's, and keeps a consumer the body names", async () => {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { post } = await import("./api");
    await post("/v1/memory.get", { org: "dev", memory_key: "k" });
    await post("/v1/code.search", { org: "dev", consumer: "someone-else" });
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init.body)));
    expect(bodies.map((b) => b.consumer)).toEqual(["loom-ui", "someone-else"]);
  });
});
