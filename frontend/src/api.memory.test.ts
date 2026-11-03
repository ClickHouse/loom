import { afterEach, describe, expect, it, vi } from "vitest";
import { getMemoryById, type MemoryDetail, pinMemory, unpinMemory } from "./api";

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

    await unpinMemory({ org: "dev", namespace: "local", agent: "bot", user_id: "alice", session_id: "s1" }, "k1");
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1]?.body))).toMatchObject({
      agent: "bot",
      user_id: "alice",
      session_id: "s1",
    });
  });
});

describe("pinMemory", () => {
  const row: MemoryDetail = {
    found: true,
    value: "deploys freeze on Fridays",
    memory_type: "preference",
    confidence: 0.8,
    version: 3,
    pinned: false,
    created_at: "2026-09-18T09:00:00Z",
    tags: ["infra", "prod"],
    entities: ["deploy"],
    source_trace: "conversation:t1",
    source_resource_id: "doc-1",
    source_class: "extracted",
    sensitivity: "high",
    valid_at: "2026-09-01T00:00:00Z",
    valid_to: null,
    char_limit: 200,
  };

  async function pinBody(detail: MemoryDetail): Promise<Record<string, unknown>> {
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
        new Response(JSON.stringify({ version: 4, pinned: true }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await pinMemory({ org: "dev", namespace: "local", agent: "loom-ui" }, "deploy/freeze", detail);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/memory.pin");
    return JSON.parse(String(init?.body));
  }

  // memory.pin writes a whole new version as memory.set does, so a field the body leaves
  // out is lost on the pinned row.
  it("writes the row back with its tags, entities, confidence, sensitivity, provenance and validity", async () => {
    expect(await pinBody(row)).toEqual({
      consumer: "loom-ui",
      org: "dev",
      namespace: "local",
      agent: "loom-ui",
      user_id: "-",
      session_id: "-",
      memory_key: "deploy/freeze",
      value: "deploys freeze on Fridays",
      memory_type: "preference",
      tags: ["infra", "prod"],
      entities: ["deploy"],
      confidence: 0.8,
      sensitivity: "high",
      source_trace: "conversation:t1",
      source_resource_id: "doc-1",
      valid_at: "2026-09-01T00:00:00Z",
      char_limit: 200,
    });
  });

  it("writes to the row's own agent, user and session, not the caller's", async () => {
    const body = await pinBody({ ...row, agent: "support-bot", user_id: "alice", session_id: "s1" });
    expect(body).toMatchObject({ agent: "support-bot", user_id: "alice", session_id: "s1" });
  });

  it("guards the write with the hash of the value it read", async () => {
    expect((await pinBody({ ...row, content_sha256: "abc" })).if_match_sha256).toBe("abc");
  });

  it("declares the row's source_class only when a caller may declare it", async () => {
    expect((await pinBody({ ...row, source_class: "imported" })).source_class).toBe("imported");
    expect(await pinBody({ ...row, source_class: "asserted" })).not.toHaveProperty("source_class");
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

  it("leaves a route that is not an op as it is: the model setup refuses an unknown field", async () => {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { resetModelConfiguration, retryModelConfiguration } = await import("./api/models");
    await resetModelConfiguration("v1");
    await retryModelConfiguration("c1");
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init.body)));
    expect(bodies).toEqual([{ expected_version: "v1" }, { config_id: "c1" }]);
  });
});
