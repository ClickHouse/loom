import { afterEach, describe, expect, it, vi } from "vitest";
import { generateMcpConfig } from "./api";

// The Setup page's mint body, as the previous server build reads it during a rolling upgrade.
describe("generateMcpConfig", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends persist: true with the client, namespace and expiry", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ token: "t", mcp_url: "u", ignored_args: ["persist"] }), { status: 200 });
      }),
    );
    await generateMcpConfig("claude-code", { namespace: "support", ttl_days: 30 });
    expect(bodies).toEqual([
      { consumer: "loom-ui", client: "claude-code", persist: true, namespace: "support", ttl_days: 30 },
    ]);
  });
});
