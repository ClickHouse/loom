import { afterEach, describe, expect, it, vi } from "vitest";
import { listProfile } from "./api";

// The Profile page reads its sections once — from the list — and a row by key only when the list says its value was cut.
describe("listProfile", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stub(entries: Array<Record<string, unknown>>, getValue = "the whole value") {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body));
        calls.push({ url, body });
        if (url === "/v1/memory.list")
          return new Response(JSON.stringify({ entries, truncated: false }), { status: 200 });
        return new Response(JSON.stringify({ found: true, value: getValue, version: 9, content_sha256: "abc" }), {
          status: 200,
        });
      }),
    );
    return calls;
  }

  it("issues no memory.get when every section fits the preview", async () => {
    const calls = stub([
      {
        memory_key: "summary",
        value: "short",
        version: 3,
        created_at: "2026-09-19T10:00:00Z",
        agent: "loom-ui",
        value_truncated: false,
      },
      { memory_key: "prefs", value: { tone: "brief" }, version: 4, created_at: "2026-09-19T10:00:00Z", agent: "cli" },
    ]);
    const out = await listProfile({ org: "o", namespace: "n", agent: "loom-ui" });
    expect(calls.map((c) => c.url)).toEqual(["/v1/memory.list"]);
    expect(out.entries.map((e) => [e.label, e.value, e.version, e.last_edited_by])).toEqual([
      ["summary", "short", 3, "loom-ui"],
      ["prefs", JSON.stringify({ tone: "brief" }), 4, "cli"],
    ]);
  });

  it("reads only the section whose value was cut, under its writer's agent", async () => {
    const calls = stub([
      { memory_key: "summary", value: "short", version: 3, created_at: "t", agent: "loom-ui", value_truncated: false },
      {
        memory_key: "long",
        value: "a preview…",
        version: 5,
        created_at: "t",
        agent: "writer-x",
        value_truncated: true,
      },
    ]);
    const out = await listProfile({ org: "o", namespace: "n", agent: "loom-ui" });
    expect(calls.map((c) => c.url)).toEqual(["/v1/memory.list", "/v1/memory.get"]);
    expect(calls[1].body).toMatchObject({ memory_key: "long", agent: "writer-x" });
    expect(out.entries[1]).toMatchObject({
      label: "long",
      value: "the whole value",
      version: 9,
      content_sha256: "abc",
    });
  });
});
