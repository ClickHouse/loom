// Single source of truth for the MCP-client list — the picker labels and the
// config-block formatter. The first-run wizard and the setup view both import
// from here, so adding a client is one edit. The server mirrors the list in
// server/identity/app/token_clients.py (MCP_CLIENTS).

export type ClientId = "claude-desktop" | "claude-code" | "codex" | "cursor" | "vscode" | "cline" | "windsurf" | "zed";

interface MCPClient {
  id: ClientId;
  label: string;
}

export const CLIENTS: MCPClient[] = [
  { id: "claude-desktop", label: "Claude Desktop" },
  { id: "claude-code", label: "Claude Code" },
  { id: "codex", label: "Codex" },
  { id: "cursor", label: "Cursor" },
  { id: "vscode", label: "VS Code" },
  { id: "cline", label: "Cline" },
  { id: "windsurf", label: "Windsurf" },
  { id: "zed", label: "Zed" },
];

/** True for a value naming one of CLIENTS — a stored or picked id is checked, not cast. */
export function isClientId(v: string | null): v is ClientId {
  return CLIENTS.some((c) => c.id === v);
}

// Clients that speak MCP only over stdio — need the npx mcp-remote shim to
// bridge to a remote HTTP MCP server. Mirrors server/identity/app/token_clients.py.
const STDIO_VIA_NPX: ReadonlySet<ClientId> = new Set(["claude-desktop", "zed", "cline"]);

// Coding agents mount the `coding` toolset: 19 tools, where the deployment default `core`
// lists 41 once the code tools are on — over Cursor's historical 40-tool cap, and ~14k
// tokens of listing per turn for Claude Code. `?toolset=` rides the URL, so one server
// serves both.
const CODING_TOOLSET = "coding";
export const CODING_TOOLSET_CLIENTS: ReadonlySet<ClientId> = new Set(["claude-code", "codex", "cursor"]);

// Codex reads the bearer from an environment variable it is told the NAME of;
// the token itself never sits in ~/.codex/config.toml.
const CODEX_TOKEN_ENV = "LOOM_API_KEY";

/** Append `?toolset=<tier>` to an MCP URL unless it already names one. */
export function withToolset(mcpUrl: string, toolset: string): string {
  if (/[?&]toolset=/.test(mcpUrl)) return mcpUrl;
  return `${mcpUrl}${mcpUrl.includes("?") ? "&" : "?"}toolset=${toolset}`;
}

/** The one command that registers Loom in Claude Code: user scope, stored in
 *  ~/.claude.json. Claude Code does not read ~/.claude/mcp.json. */
export function claudeMcpAddCommand(args: { mcpUrl: string; token: string; name?: string }): string {
  const name = args.name ?? "loom";
  return `claude mcp add --transport http ${name} "${args.mcpUrl}" -H "Authorization: Bearer ${args.token}" -s user`;
}

/** The one command that registers Loom in Codex: stored as `[mcp_servers.<name>]` in
 *  ~/.codex/config.toml by Codex itself. */
export function codexMcpAddCommand(args: { mcpUrl: string; name?: string }): string {
  const name = args.name ?? "loom";
  return `codex mcp add ${name} --url "${args.mcpUrl}" --bearer-token-env-var ${CODEX_TOKEN_ENV}`;
}

/** The same Codex entry as TOML, for a hand edit of ~/.codex/config.toml. */
function codexConfigToml(args: { mcpUrl: string; name?: string }): string {
  const name = args.name ?? "loom";
  return `[mcp_servers.${name}]\nurl = "${args.mcpUrl}"\nbearer_token_env_var = "${CODEX_TOKEN_ENV}"\n`;
}

interface FormattedMcpConfig {
  configKey: string;
  block: Record<string, unknown>;
  /** A one-line registration command, for the clients that have one. */
  command?: string;
  /** The block rendered in the client's own file format when it is not JSON. */
  fileText?: string;
  /** Shell lines the client needs before it can authenticate (Codex reads the
   *  bearer from an env var). */
  envLines?: string;
}

/** Build an MCP config block for a client. Built here as well as by the server's
 *  mint, because an answer from the previous build has no `clients`;
 *  `clientSetups.fixture.json` keeps the two alike.
 *  For Claude Code the block is the project-scoped `.mcp.json` form and
 *  `command` is the `claude mcp add` one-liner (user scope) — the two places
 *  Claude Code actually reads. For Codex the block is `[mcp_servers.loom]`,
 *  `fileText` is its TOML, `command` is `codex mcp add`, and `envLines` is the
 *  export the bearer env var needs. */
export function formatMcpConfig(args: { client: ClientId; token: string; mcpUrl: string }): FormattedMcpConfig {
  const configKey =
    args.client === "vscode"
      ? "servers"
      : args.client === "zed"
        ? "context_servers"
        : args.client === "codex"
          ? "mcp_servers"
          : "mcpServers";

  const mcpUrl = CODING_TOOLSET_CLIENTS.has(args.client) ? withToolset(args.mcpUrl, CODING_TOOLSET) : args.mcpUrl;

  if (args.client === "codex") {
    return {
      configKey,
      block: { mcp_servers: { loom: { url: mcpUrl, bearer_token_env_var: CODEX_TOKEN_ENV } } },
      command: codexMcpAddCommand({ mcpUrl }),
      fileText: codexConfigToml({ mcpUrl }),
      envLines: `export ${CODEX_TOKEN_ENV}="${args.token}"`,
    };
  }

  const authHeader = `Authorization: Bearer ${args.token}`;
  const entry: Record<string, unknown> = STDIO_VIA_NPX.has(args.client)
    ? {
        command: "npx",
        args: ["-y", "mcp-remote", mcpUrl, "--allow-http", "--header", authHeader],
      }
    : (() => {
        const e: Record<string, unknown> = {
          url: mcpUrl,
          headers: { Authorization: `Bearer ${args.token}` },
        };
        if (args.client === "claude-code" || args.client === "vscode") {
          e.type = "http";
        }
        return e;
      })();

  const out: FormattedMcpConfig = {
    configKey,
    block: { [configKey]: { loom: entry } },
  };
  if (args.client === "claude-code") {
    out.command = claudeMcpAddCommand({ mcpUrl, token: args.token });
  }
  return out;
}

/** What the token dialog shows in its config box: the client's own file text
 *  when the file is not JSON, else the JSON block. */
export function configPreviewText(cfg: FormattedMcpConfig): string {
  return cfg.fileText ?? JSON.stringify(cfg.block, null, 2);
}

/** The two commands that install a client's Loom capture plugin (hooks +
 *  skills), or null for a client without one. */
export function pluginInstallLines(client: ClientId): string | null {
  if (client === "claude-code") {
    return "claude plugin marketplace add ClickHouse/loom\nclaude plugin install loom@loom";
  }
  if (client === "codex") {
    return "codex plugin marketplace add ClickHouse/loom\ncodex plugin add loom@loom";
  }
  return null;
}
