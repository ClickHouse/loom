import { describe, expect, it } from "vitest";
import serverSetups from "./clientSetups.fixture.json";
import {
  CLIENTS,
  CODING_TOOLSET_CLIENTS,
  claudeMcpAddCommand,
  codexMcpAddCommand,
  configPreviewText,
  formatMcpConfig,
  isClientId,
  pluginInstallLines,
  withToolset,
  type ClientId,
} from "./clients";
import { isRecord } from "./guards";

const T = "eyJ.tok.en";
const URL = "https://loom.example/mcp";
/** The value as a record, or empty when it is not one — a wrong shape fails the expects below. */
const rec = (v: unknown): Record<string, unknown> => (isRecord(v) ? v : {});

describe("withToolset", () => {
  it("appends ?toolset= to a bare URL and &toolset= after a query", () => {
    expect(withToolset(URL, "coding")).toBe(`${URL}?toolset=coding`);
    expect(withToolset(`${URL}?schema_profile=standard`, "coding")).toBe(
      `${URL}?schema_profile=standard&toolset=coding`,
    );
  });
  it("leaves a URL that already names a toolset alone", () => {
    expect(withToolset(`${URL}?toolset=core`, "coding")).toBe(`${URL}?toolset=core`);
  });
});

describe("formatMcpConfig — Claude Code", () => {
  const cfg = formatMcpConfig({ client: "claude-code", token: T, mcpUrl: URL });

  it("emits the claude mcp add command at user scope with the coding toolset", () => {
    expect(cfg.command).toBe(
      `claude mcp add --transport http loom "${URL}?toolset=coding" -H "Authorization: Bearer ${T}" -s user`,
    );
    expect(claudeMcpAddCommand({ mcpUrl: URL, token: T, name: "mem" })).toContain("--transport http mem ");
  });

  it("the block is the project .mcp.json form Claude Code reads", () => {
    expect(cfg.configKey).toBe("mcpServers");
    expect(cfg.block).toEqual({
      mcpServers: {
        loom: {
          type: "http",
          url: `${URL}?toolset=coding`,
          headers: { Authorization: `Bearer ${T}` },
        },
      },
    });
  });
});

describe("formatMcpConfig — Cursor", () => {
  it("carries ?toolset=coding so the listing stays under the 40-tool cap", () => {
    const cfg = formatMcpConfig({ client: "cursor", token: T, mcpUrl: URL });
    const loom = rec(rec(cfg.block.mcpServers).loom);
    expect(loom.url).toBe(`${URL}?toolset=coding`);
    expect(loom.type).toBeUndefined();
    expect(cfg.command).toBeUndefined();
  });
});

describe("formatMcpConfig — every other client keeps the bare URL", () => {
  const others = CLIENTS.map((c) => c.id).filter((id) => !CODING_TOOLSET_CLIENTS.has(id));

  it.each(others)("%s", (id: ClientId) => {
    const cfg = formatMcpConfig({ client: id, token: T, mcpUrl: URL });
    const json = JSON.stringify(cfg.block);
    expect(json).toContain(URL);
    expect(json).not.toContain("toolset=");
    expect(cfg.command).toBeUndefined();
  });

  it("stdio clients get the mcp-remote bridge", () => {
    const cfg = formatMcpConfig({ client: "claude-desktop", token: T, mcpUrl: URL });
    const loom = rec(rec(cfg.block.mcpServers).loom);
    expect(loom.command).toBe("npx");
    expect(loom.args).toContain(URL);
  });
});

describe("formatMcpConfig — Codex", () => {
  const cfg = formatMcpConfig({ client: "codex", token: T, mcpUrl: URL });

  it("registers through codex mcp add with the bearer read from LOOM_API_KEY", () => {
    expect(cfg.command).toBe(`codex mcp add loom --url "${URL}?toolset=coding" --bearer-token-env-var LOOM_API_KEY`);
    expect(codexMcpAddCommand({ mcpUrl: URL, name: "mem" })).toContain("codex mcp add mem ");
  });

  it("the block is the [mcp_servers.loom] table and the preview is its TOML", () => {
    expect(cfg.configKey).toBe("mcp_servers");
    expect(cfg.block).toEqual({
      mcp_servers: { loom: { url: `${URL}?toolset=coding`, bearer_token_env_var: "LOOM_API_KEY" } },
    });
    expect(cfg.fileText).toBe(
      `[mcp_servers.loom]\nurl = "${URL}?toolset=coding"\nbearer_token_env_var = "LOOM_API_KEY"\n`,
    );
    expect(configPreviewText(cfg)).toBe(cfg.fileText);
    // the token never appears in the file Codex keeps; it rides the env line
    expect(cfg.fileText).not.toContain(T);
    expect(cfg.envLines).toBe(`export LOOM_API_KEY="${T}"`);
  });

  it("JSON clients preview as JSON", () => {
    const cc = formatMcpConfig({ client: "claude-code", token: T, mcpUrl: URL });
    expect(configPreviewText(cc)).toBe(JSON.stringify(cc.block, null, 2));
  });

  it("both coding agents have a capture plugin; chat clients do not", () => {
    expect(pluginInstallLines("codex")).toBe(
      "codex plugin marketplace add ClickHouse/loom\ncodex plugin add loom@loom",
    );
    expect(pluginInstallLines("claude-code")).toContain("claude plugin install loom@loom");
    expect(pluginInstallLines("cursor")).toBeNull();
  });
});

describe("isClientId", () => {
  it("accepts every listed client id and rejects anything else", () => {
    for (const c of CLIENTS) expect(isClientId(c.id)).toBe(true);
    expect(isClientId("claude")).toBe(false);
    expect(isClientId("")).toBe(false);
    expect(isClientId(null)).toBe(false);
  });
});

describe("the dashboard builds the setups the server's token mint returns", () => {
  // The fixture is token_clients.client_config's output; tests/unit/test_token_clients.py
  // checks that side.
  const cases = serverSetups.flatMap(({ mcp_url, setups }) =>
    Object.entries(setups).map(([client, server]) => ({ mcpUrl: mcp_url, client, server })),
  );
  it.each(cases)("$client on $mcpUrl", ({ mcpUrl, client, server }) => {
    expect(isClientId(client)).toBe(true);
    const ours = formatMcpConfig({ client: client as ClientId, token: "T", mcpUrl });
    expect(ours.configKey).toBe(server.config_key);
    expect(ours.block).toEqual(server.config);
    expect(ours.command ?? "").toBe(server.command);
    expect(ours.fileText ?? "").toBe(server.config_toml);
    const env = Object.entries(server.env as Record<string, string>).map(([k, v]) => `export ${k}="${v}"`);
    expect(ours.envLines ?? "").toBe(env.join("\n"));
  });
});
