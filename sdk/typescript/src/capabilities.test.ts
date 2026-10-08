/** What the deployment serves and what this token may call. */
import { describe, expect, it } from "vitest";
import { Agent } from "./agent.js";
import { Capabilities, probe } from "./capabilities.js";
import { LoomClient } from "./client.js";
import { decodeTokenClaims, ROLE_RANK, roleOn, roleSatisfies } from "./token.js";
import { Transport } from "./transport.js";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => Buffer.from(s, "utf8").toString("base64url");
  return `${b64(JSON.stringify({ alg: "HS256", kid: "k1" }))}.${b64(JSON.stringify(payload))}.sig`;
}

const WRITER = jwt({ sub: "sa:bot", org: "acme", namespaces: ["*"], roles: ["WRITER"], exp: 1_800_000_000 });
const SCOPED = jwt({
  sub: "sa:bot",
  org: "acme",
  namespaces: ["support", "ci"],
  roles: ["viewer"],
  ns_roles: [
    ["support", "admin"],
    ["ci", "viewer"],
  ],
});

const CAPS = {
  build_commit: "abc1234",
  schema_version: "1",
  ops: [
    { name: "memory.get", required_role: "VIEWER" },
    { name: "memory.set", required_role: "WRITER" },
    { name: "conflicts.list", required_role: "ADMIN" },
    { name: "cost.budget_status", required_role: "ADMIN" },
    { name: "admin.users", required_role: "ORG_ADMIN" },
    { name: "ops.strange", required_role: "SUPERUSER" },
  ],
};

function harness(token: string | undefined, replies: unknown[] = [CAPS]) {
  let calls = 0;
  const fetch = async (): Promise<Response> => {
    calls++;
    return new Response(JSON.stringify(replies[Math.min(calls - 1, replies.length - 1)]), { status: 200 });
  };
  const client = new LoomClient(new Transport({ baseUrl: "http://x", apiKey: token, fetch, logLevel: "off" }));
  return { client, calls: () => calls };
}

describe("token claims", () => {
  it("decodes org, namespaces, roles and ns_roles without verifying, and refuses non-JWTs", () => {
    const c = decodeTokenClaims(WRITER);
    expect(c).toMatchObject({
      subject: "sa:bot",
      org: "acme",
      namespaces: ["*"],
      roles: ["writer"],
      nsRoles: [],
      exp: 1_800_000_000,
    });
    expect(decodeTokenClaims(SCOPED)?.nsRoles).toEqual([
      ["support", "admin"],
      ["ci", "viewer"],
    ]);
    expect(decodeTokenClaims("opaque-token")).toBeUndefined();
    expect(decodeTokenClaims("a.b.c")).toBeUndefined();
    expect(decodeTokenClaims(undefined)).toBeUndefined();
    expect(decodeTokenClaims(jwt({ ns_roles: { support: "writer" } }))?.nsRoles).toEqual([["support", "writer"]]);
    expect(decodeTokenClaims(jwt({ sub: "sa:bot-café-日本", roles: [] }))?.subject).toBe("sa:bot-café-日本");
  });

  it("ranks roles like the server and answers undefined for unknown names", () => {
    expect(ROLE_RANK.viewer).toBeLessThan(ROLE_RANK.writer);
    expect(roleSatisfies("writer", "VIEWER")).toBe(true);
    expect(roleSatisfies("writer", "admin")).toBe(false);
    expect(roleSatisfies("writer", "superuser")).toBeUndefined();
    expect(roleSatisfies(undefined, "viewer")).toBeUndefined();
    expect(roleOn(decodeTokenClaims(SCOPED), "support")).toBe("admin");
    expect(roleOn(decodeTokenClaims(SCOPED), "ci")).toBe("viewer");
    expect(roleOn(decodeTokenClaims(SCOPED), "elsewhere")).toBe("admin"); // best the map grants anywhere
    expect(roleOn(decodeTokenClaims(SCOPED))).toBe("admin");
    expect(roleOn(decodeTokenClaims(WRITER), "anything")).toBe("writer");
  });
});

describe("Capabilities", () => {
  it("answers canCall from required_role × token role, undefined when it cannot tell", async () => {
    const { client } = harness(WRITER);
    const caps = await client.capabilities();
    expect(caps).toBeInstanceOf(Capabilities);
    expect(caps.buildCommit).toBe("abc1234");
    expect(caps.has("memory.set")).toBe(true);
    expect(caps.requiredRole("conflicts.list")).toBe("admin");
    expect(caps.role()).toBe("writer");
    expect(caps.canCall("memory.get")).toBe(true);
    expect(caps.canCall("memory.set")).toBe(true);
    expect(caps.canCall("conflicts.list")).toBe(false);
    expect(caps.canCall("admin.users")).toBe(false);
    expect(caps.canCall("ops.strange")).toBeUndefined(); // unknown role name
    expect(caps.canCall("memory.nope")).toBeUndefined(); // not served
    expect(caps.callable()).toEqual(["memory.get", "memory.set"]);
  });

  it("uses the scoped role for the namespace asked about", async () => {
    const { client } = harness(SCOPED);
    const caps = await client.capabilities();
    expect(caps.canCall("conflicts.list", "support")).toBe(true);
    expect(caps.canCall("conflicts.list", "ci")).toBe(false);
    expect(caps.canCall("memory.set", "ci")).toBe(false);
    const bot = new Agent("acme", "ci", "bot", client);
    expect(await bot.canCall("memory.set")).toBe(false);
    expect(await new Agent("acme", "support", "bot", client).canCall("cost.budget_status")).toBe(true);
  });

  it("cannot tell without a JWT, and caches the fetch for maxAgeMs", async () => {
    const { client, calls } = harness("opaque-token");
    const caps = await client.capabilities();
    expect(caps.claims).toBeUndefined();
    expect(caps.canCall("memory.get")).toBeUndefined();
    expect(caps.callable()).toEqual([]);
    await client.capabilities();
    await client.capabilities();
    expect(calls()).toBe(1);
    await client.capabilities({ maxAgeMs: 0 });
    expect(calls()).toBe(2);
  });
});

describe("probe", () => {
  it("names the ops and request fields a deployment lacks, and the ops this SDK does not know", async () => {
    const caps = {
      ...CAPS,
      ops: [
        { name: "memory.get", required_role: "VIEWER", request_fields: ["org", "namespace", "memory_key"] },
        { name: "memory.brand_new", required_role: "VIEWER" },
      ],
    };
    const { client, calls } = harness(WRITER, [caps]);
    const p = await probe(client, {
      ops: ["memory.get", "memory.set"],
      fields: { "memory.get": ["memory_key", "as_of"], "memory.set": ["value"] },
    });
    expect(p).toEqual({
      ok: false,
      buildCommit: "abc1234",
      schemaVersion: "1",
      missingOps: ["memory.set"],
      missingFields: { "memory.get": ["as_of"], "memory.set": ["value"] },
      unknownToSdk: ["memory.brand_new"],
    });
    expect(calls()).toBe(1);
    expect((await probe(client, { ops: ["memory.get"] })).ok).toBe(true);
  });
});
