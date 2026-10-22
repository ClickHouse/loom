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
    expect(JSON.parse(String(init?.body))).toEqual({ org: "dev", memory_id: "m1", omit_high_value: true });
  });
});
