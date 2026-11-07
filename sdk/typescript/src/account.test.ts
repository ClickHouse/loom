import { describe, expect, it } from "vitest";
import type {
  AccountClient,
  CreateTokenOptions,
  GrantRole,
  TokenClientSetup,
  TokenCreated,
  TokenNamespace,
  TokenRevoked,
  TokenRow,
} from "./account.js";
import { Agent } from "./agent.js";
import { LoomClient } from "./client.js";
import { PreconditionFailed } from "./errors.js";
import type { TokenProvider } from "./machine.js";
import { Transport, type TransportOptions } from "./transport.js";

interface Sent {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

/** A client whose fetch answers from `reply` and records every request. */
function client(reply: (s: Sent) => Response, opts: Partial<TransportOptions> = { apiKey: "k-static" }) {
  const sent: Sent[] = [];
  const fetch = async (url: string, init: RequestInit) => {
    const s: Sent = {
      url,
      method: init.method ?? "",
      headers: { ...(init.headers as Record<string, string>) },
      body: init.body === undefined ? undefined : JSON.parse(init.body as string),
    };
    sent.push(s);
    return reply(s);
  };
  const transport = new Transport({ baseUrl: "http://x", fetch, sleep: async () => {}, logLevel: "off", ...opts });
  return { loom: new LoomClient(transport), sent };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

const row: TokenRow = {
  name: "ci",
  sa_subject: "sa:ci-abc123",
  namespaces: ["support"],
  roles: ["writer"],
  ttl_days: 90,
  created_by: "alice",
  created_at: "2026-10-01T00:00:00",
  expires_at: "2026-12-30T00:00:00",
  revoked_at: null,
  revoked_by: "",
  last_used_at: null,
  ns_roles: { support: "writer" },
  yours: true,
};

const cursorSetup: TokenClientSetup = {
  config_key: "mcpServers",
  config: { mcpServers: { loom: { url: "http://x/mcp?toolset=coding" } } },
  config_toml: "",
  env: {},
  command: "",
  config_path: { darwin: "~/.cursor/mcp.json", linux: "~/.cursor/mcp.json", win32: "%USERPROFILE%\\.cursor\\mcp.json" },
};

describe("AccountClient", () => {
  it("creates with ordered mixed namespaces, a name, a default role and ttlDays 0", async () => {
    const created: TokenCreated = {
      token: "jwt",
      name: "ci",
      sa_subject: "sa:ci-abc123",
      org: "acme",
      namespaces: ["support", "docs"],
      ns_roles: { support: "writer", docs: "viewer" },
      reads_everything: false,
      saves_to: "support",
      created_at: "2026-10-01T00:00:00",
      expires_at: null,
      restricted_namespaces: [],
      mcp_url: "http://x/mcp",
      clients: { cursor: cursorSetup },
    };
    const { loom, sent } = client(() => json(created));
    const namespaces: TokenNamespace[] = ["support", { namespace: "docs", role: "viewer" }];
    const role: GrantRole = "writer";
    const options: CreateTokenOptions = { name: "ci", role, ttlDays: 0 };
    const out = await loom.account.createToken(namespaces, options);
    expect(out).toEqual(created);
    expect(sent[0].method).toBe("POST");
    expect(sent[0].url).toBe("http://x/v1/me/service_accounts.create");
    expect(sent[0].body).toEqual({
      name: "ci",
      namespaces: ["support", { namespace: "docs", role: "viewer" }],
      ttl_days: 0,
      role: "writer",
    });
  });

  it("reads everything by default, with the default ttl of 90 days and no name or role unless given", async () => {
    const { loom, sent } = client(() => json({}));
    await loom.account.createToken();
    expect(sent[0].body).toEqual({ namespaces: "*", ttl_days: 90 });
    await loom.account.createToken(["support"], { ttlDays: null });
    expect(sent[1].body).toEqual({ namespaces: ["support"], ttl_days: null });
  });

  it("lists through a GET with the bearer and no body", async () => {
    const { loom, sent } = client(() => json({ rows: [row] }));
    expect(await loom.account.tokens()).toEqual([row]);
    expect(sent[0]).toMatchObject({ method: "GET", url: "http://x/v1/me/service_accounts", body: undefined });
    expect(sent[0].headers.Authorization).toBe("Bearer k-static");
    expect(sent[0].headers["X-Request-Id"]).toBeTruthy();
    expect(sent[0].headers["Idempotency-Key"]).toBeUndefined();
    expect(sent[0].headers["Content-Type"]).toBeUndefined();
  });

  it("retries the GET on a 5xx, as a read", async () => {
    let n = 0;
    const { loom, sent } = client(() => (n++ === 0 ? json({ detail: { code: "X" } }, 503) : json({ rows: [] })));
    expect(await loom.account.tokens()).toEqual([]);
    expect(sent.map((s) => s.headers["X-Loom-Retry-Count"])).toEqual(["0", "1"]);
  });

  it("revokes by name", async () => {
    const revoked: TokenRevoked = {
      revoked: true,
      name: "ci",
      revoked_at: "2026-10-02T00:00:00",
      grants_revoked: 1,
    };
    const { loom, sent } = client(() => json(revoked));
    expect(await loom.account.revokeToken("ci")).toEqual(revoked);
    expect(sent[0]).toMatchObject({
      method: "POST",
      url: "http://x/v1/me/service_accounts.revoke",
      body: { name: "ci" },
    });
  });

  it("surfaces 409 TOKEN_NAME_TAKEN as PreconditionFailed, not retried", async () => {
    const { loom, sent } = client(() =>
      json({ detail: { code: "TOKEN_NAME_TAKEN", message: "that name is in use; choose another name" } }, 409),
    );
    const err = await loom.account.createToken(["support"], { name: "ci" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(PreconditionFailed);
    expect((err as PreconditionFailed).code).toBe("TOKEN_NAME_TAKEN");
    expect((err as PreconditionFailed).status).toBe(409);
    expect(sent).toHaveLength(1);
  });

  it("sends a TokenProvider's bearer on the GET and refreshes it once on a 401", async () => {
    const tokens = ["t-old", "t-new"];
    const refusedSeen: Array<string | undefined> = [];
    const tokenProvider: TokenProvider = {
      token: (refused) => {
        refusedSeen.push(refused);
        return tokens[refusedSeen.length - 1] ?? "t-new";
      },
    };
    const { loom, sent } = client(
      (s) =>
        s.headers.Authorization === "Bearer t-old" ? json({ detail: { code: "AUTH" } }, 401) : json({ rows: [] }),
      { tokenProvider },
    );
    expect(await loom.account.tokens()).toEqual([]);
    expect(sent.map((s) => [s.method, s.headers.Authorization])).toEqual([
      ["GET", "Bearer t-old"],
      ["GET", "Bearer t-new"],
    ]);
    expect(refusedSeen).toEqual([undefined, "t-old"]);
  });

  it("stops a GET on the caller's signal", async () => {
    const { loom, sent } = client(() => json({ rows: [] }));
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(loom.account.tokens({ signal: ctrl.signal })).rejects.toThrow(/aborted/);
    expect(sent).toHaveLength(0);
  });

  it("is the same client on an Agent", () => {
    const { loom } = client(() => json({}));
    const bot = new Agent("acme", "support", "assistant", loom);
    const account: AccountClient = bot.account;
    expect(account).toBe(loom.account);
  });
});
