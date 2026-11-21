// Install-time options → the one file every reader knows.
//
// `claude plugin install loom@loom --config server_url=… --config token=…`
// stores the values with Claude Code and exports them to hook processes as
// CLAUDE_PLUGIN_OPTION_SERVER_URL / CLAUDE_PLUGIN_OPTION_TOKEN. The MCP entry
// takes the URL by `${user_config.server_url}` substitution, but its header
// helper is not a hook and receives no option. It reads available credential
// env vars, then ~/.loom/config.json, then ~/.loom/token. So the first hook
// of a session writes the option token — and the URL beside it — into
// ~/.loom/config.json when nothing else already provides a token. The helper
// waits briefly for that file. Writes are idempotent and private; an invalid
// endpoint refuses network work with an actionable configuration notice.

import { claudeEndpoint, EndpointError, parseEndpoint } from "./mcp-endpoint.mjs";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  hookClient,
  loomConfigPath,
  loomTokenPath,
  normalizeUrl,
  pluginDefaults,
  readConfigFile,
  writeConfigMerged,
} from "./loom-client.mjs";
import { readJsonFile, removeFile, tmpStatePath, writeJsonFile } from "./tmp-state.mjs";

/** The endpoint's control-plane base: an explicit install value, else
 *  the environment default. "" when neither names a server. */
function namedServerUrl(env) {
  if (entryIsFixed() || hookClient(process.argv, env) !== "claude-code") {
    return normalizeUrl(env.LOOM_BASE_URL || env.CLAUDE_PLUGIN_OPTION_SERVER_URL);
  }
  return env.CLAUDE_PLUGIN_OPTION_SERVER_URL || env.LOOM_BASE_URL ? claudeEndpoint(env).base : "";
}

/** The control-plane base of Claude Code's MCP endpoint. */
export function mcpServerUrl(env = process.env) {
  return namedServerUrl(env) || "http://localhost:7777";
}

/** The Codex plugin ships a fixed MCP entry (`plugin-defaults.json`), which
 *  the person may replace whole in Codex's own config; these scripts cannot
 *  read that, so under Codex nothing here reasons about the entry's URL. */
export function entryIsFixed() {
  return Boolean(pluginDefaults().server_url);
}

/** Whether two control-plane bases match, including proxy prefixes, with the
 *  loopback spellings folded. Unparsable values compare as strings. */
export function sameServer(a, b) {
  const base = (u) => {
    const parsed = new URL(parseEndpoint(`${String(u || "").trim()}/mcp`).url);
    const host = parsed.hostname === "127.0.0.1" || parsed.hostname === "[::1]" ? "localhost" : parsed.hostname;
    return `${parsed.protocol}//${host}${parsed.port ? `:${parsed.port}` : ""}${parsed.pathname.slice(0, -4)}`;
  };
  try {
    return base(a) === base(b);
  } catch {
    return normalizeUrl(a) === normalizeUrl(b);
  }
}

/** One state file per installed plugin: the Codex copy of these scripts
 *  keeps its own, and never writes it (Codex passes no option). */
const PLUGIN_ROOT = fileURLToPath(new URL("..", import.meta.url));
const installStatePath = (tmp) => tmpStatePath("install", PLUGIN_ROOT, { tmp });

/** The SessionStart hook saves the install option's URL for the status
 *  script, which the agent runs from a shell that carries no option; an
 *  install without the option removes a stale one. */
export function saveInstallServerUrl(env = process.env, { tmp } = {}) {
  if (entryIsFixed() || hookClient(process.argv, env) !== "claude-code") return;
  let endpoint;
  try {
    endpoint = claudeEndpoint(env);
  } catch (error) {
    if (!(error instanceof EndpointError)) throw error;
    writeJsonFile(installStatePath(tmp), { invalid: true });
    throw error;
  }
  const url = env.CLAUDE_PLUGIN_OPTION_SERVER_URL ? endpoint.base : "";
  if (url) writeJsonFile(installStatePath(tmp), { url });
  else removeFile(installStatePath(tmp));
}

/** The saved install URL, "" when no session saved one. */
export function installServerUrl({ tmp } = {}) {
  const saved = readJsonFile(installStatePath(tmp), {});
  if (saved.invalid) throw new EndpointError();
  return String(saved.url || "");
}

/** The server the status script checks under Claude Code: the one the MCP
 *  entry calls, with the saved install URL standing in for the option the
 *  agent's shell lacks. (Under Codex the script checks the hooks' server,
 *  `resolveServerUrl`, and decides that itself.) */
export function statusServerUrl({ tmp } = {}) {
  return installServerUrl({ tmp }) || mcpServerUrl();
}

/** What a run would do, without doing it: {write, url, reason}. `url` is
 *  the server the option token is for, "" when nothing names one. Pure. */
export function planMaterialize(env, { configExists, configToken, tokenFileExists }) {
  const option = String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  if (!option) return { write: false, reason: "no option" };
  if (String(env.LOOM_API_KEY || "").trim()) return { write: false, reason: "LOOM_API_KEY set" };
  if (String(configToken || "").trim()) return { write: false, reason: "config has a token" };
  if (tokenFileExists) return { write: false, reason: "token file exists" };
  const url = namedServerUrl(env);
  return { write: true, url, reason: configExists ? "config lacks a token" : "no config" };
}

/** The primer line when the token the MCP header helper will send is the
 *  config's, saved for another server than Claude Code's MCP entry calls,
 *  which refuses it. The helper reads LOOM_API_KEY, then the config: no
 *  notice when the env var is set, when this hook has just written the
 *  config's token itself, or when the config names no server. The remedy
 *  depends on whether anything named the entry's server: when nothing did,
 *  the entry fell back to localhost and the fix is to name the config's
 *  server. Pure. */
export function installTokenNotice(plan, env = process.env) {
  if (String(env.LOOM_API_KEY || "").trim() || plan.write || !plan.configHasToken) return "";
  const saved = String(plan.configUrl || "").trim();
  const inUse = mcpServerUrl(env);
  if (!saved || sameServer(saved, inUse)) return "";
  const where = `Loom: ${loomConfigPath(env)} holds a token saved for ${saved}, which the loom MCP tools send to `;
  if (!namedServerUrl(env)) {
    return (
      `${where}${inUse}, the MCP entry's default, since nothing names its server. Point the entry at ` +
      `${saved}: install with --config "server_url=${saved}/mcp?toolset=coding", or export LOOM_BASE_URL=${saved}, and restart.`
    );
  }
  return `${where}${inUse}. If the loom tools fail to connect, put a ${inUse} token in that file or in LOOM_API_KEY and restart.`;
}

export function applyPluginOptions(env = process.env, paths = {}) {
  const configPath = paths.configPath || loomConfigPath(env);
  const tokenPath = paths.tokenPath || loomTokenPath(env);
  const configExists = existsSync(configPath);
  const existing = readConfigFile(configPath);
  const plan = planMaterialize(env, {
    configExists,
    configToken: existing.token,
    tokenFileExists: existsSync(tokenPath),
  });
  plan.configUrl = String(existing.url || "").trim();
  plan.configHasToken = Boolean(String(existing.token || "").trim());
  if (!plan.write) return plan;
  try {
    // The token goes in beside the server named for it, over a url another
    // install left: the notice and the status check read them as a pair. A
    // url the file already holds stays when nothing names a server.
    const patch = { token: String(env.CLAUDE_PLUGIN_OPTION_TOKEN).trim() };
    if (plan.url) patch.url = plan.url;
    writeConfigMerged(configPath, patch);
    return { ...plan, path: configPath };
  } catch (e) {
    return { write: false, reason: `write failed: ${e && e.code ? e.code : "error"}` };
  }
}
