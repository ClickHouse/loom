import { afterEach, describe, expect, it, vi } from "vitest";
import { createToken } from "./api";

// A list mint answered by a replica of the previous build carries only the old fields;
// the result dialog still gets everything it renders.

afterEach(() => {
  vi.unstubAllGlobals();
});

function answer(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })),
  );
}

describe("createToken", () => {
  it("fills what the previous build's answer lacks from the page and the claim", async () => {
    answer({
      token: "tok",
      name: "ci",
      sa_subject: "sa:ci-1",
      org: "dev",
      namespaces: ["team-a", "team-b"],
      ns_roles: { "team-a": "viewer", "team-b": "viewer" },
      created_at: "2026-01-01T00:00:00",
      expires_at: null,
    });
    const out = await createToken({ name: "ci", namespaces: ["team-a", "team-b"], ttl_days: 90 });
    expect(out).toMatchObject({
      token: "tok",
      reads_everything: false,
      saves_to: null,
      mcp_url: `${window.location.origin}/mcp`,
    });
  });

  it("keeps what this build answers", async () => {
    answer({
      token: "tok",
      name: "token-x",
      namespaces: ["me", "*"],
      reads_everything: true,
      saves_to: "me",
      mcp_url: "https://loom.example/mcp",
    });
    const out = await createToken({ namespaces: ["me", "*"], ttl_days: 90 });
    expect(out).toMatchObject({ reads_everything: true, saves_to: "me", mcp_url: "https://loom.example/mcp" });
  });
});
