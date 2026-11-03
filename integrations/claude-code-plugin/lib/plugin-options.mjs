// Install-time options → the one file every reader knows.
//
// `claude plugin install loom@loom --config server_url=… --config token=…`
// stores the values with Claude Code and exports them to hook processes as
// CLAUDE_PLUGIN_OPTION_SERVER_URL / CLAUDE_PLUGIN_OPTION_TOKEN. The MCP entry
// takes the URL by `${user_config.server_url}` substitution, but its header
// helper is not a hook and receives no option (measured on 2.1.273): it reads
// LOOM_API_KEY, then ~/.loom/config.json, then ~/.loom/token. So the first hook
// of a session writes the option token — and the URL beside it — into
// ~/.loom/config.json when nothing else already provides a token. Claude
// Code re-runs the helper on a 401 and retries the call once, so the first
// tool call of the first session finds it. Idempotent, 0600, never throws.

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { loomConfigPath, loomTokenPath, readConfigFile, resolveServerUrl, writeConfigMerged } from "./loom-client.mjs";
import { readJsonFile, removeFile, tmpStatePath, writeJsonFile } from "./tmp-state.mjs";

/** The install option's URL, else `LOOM_BASE_URL`: the order of the MCP
 *  entry's `${user_config.server_url}`, whose default is
 *  `${LOOM_BASE_URL:-http://localhost:7777}` (`.mcp.json`). "" when neither. */
function optionServerUrl(env) {
  return String(env.CLAUDE_PLUGIN_OPTION_SERVER_URL || env.LOOM_BASE_URL || "")
    .trim()
    .replace(/\/+$/, "");
}

/** The URL the plugin's MCP entry calls. */
export function mcpServerUrl(env = process.env) {
  return optionServerUrl(env) || "http://localhost:7777";
}

/** One state file per installed plugin: the Codex copy of these scripts
 *  keeps its own, and never writes it (Codex passes no option). */
const INSTALL_URL_STATE = tmpStatePath("install", fileURLToPath(new URL("..", import.meta.url)));

/** The SessionStart hook saves the install option's URL for the status
 *  script, which the agent runs from a shell that carries no option; an
 *  install without the option removes a stale one. */
export function saveInstallServerUrl(env = process.env) {
  const url = String(env.CLAUDE_PLUGIN_OPTION_SERVER_URL || "")
    .trim()
    .replace(/\/+$/, "");
  if (url) writeJsonFile(INSTALL_URL_STATE, { url });
  else removeFile(INSTALL_URL_STATE);
}

/** The saved install URL, "" when no session saved one. */
export function installServerUrl() {
  return String(readJsonFile(INSTALL_URL_STATE, {}).url || "");
}

/** The server the status script checks: what the hooks use, with the saved
 *  install URL standing in for the option the agent's shell lacks. */
export function statusServerUrl(env = process.env) {
  return String(env.LOOM_BASE_URL || installServerUrl() || resolveServerUrl()).replace(/\/+$/, "");
}

/** What a run would do, without doing it: {write, url, reason}. Pure. */
export function planMaterialize(env, { configExists, configToken, tokenFileExists }) {
  const option = String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  if (!option) return { write: false, reason: "no option" };
  if (String(env.LOOM_API_KEY || "").trim()) return { write: false, reason: "LOOM_API_KEY set" };
  if (String(configToken || "").trim()) return { write: false, reason: "config has a token" };
  if (tokenFileExists) return { write: false, reason: "token file exists" };
  // The token is saved beside the server its MCP entry sends it to.
  const url = optionServerUrl(env);
  return { write: true, url, reason: configExists ? "config lacks a token" : "no config" };
}

/** The primer line when the install token was skipped for a config token
 *  saved for another server than `mcpUrl`, the one the MCP entry calls: the
 *  header helper sends that token there, which refuses it. "" otherwise. Pure. */
export function installTokenNotice(plan, mcpUrl) {
  if (plan.reason !== "config has a token") return "";
  const saved = String(plan.configUrl || "");
  const inUse = String(mcpUrl || "").replace(/\/+$/, "");
  if (!saved || !inUse || saved === inUse) return "";
  return (
    `Loom: ~/.loom/config.json holds a token saved for ${saved}, which the loom MCP tools send to ` +
    `${inUse}; the hooks use the install token. If the loom tools fail to connect, put a ${inUse} ` +
    `token in that file or in LOOM_API_KEY and restart.`
  );
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
  plan.configUrl = String(existing.url || "")
    .trim()
    .replace(/\/+$/, "");
  if (!plan.write) return plan;
  try {
    const patch = { token: String(env.CLAUDE_PLUGIN_OPTION_TOKEN).trim() };
    if (plan.url && !existing.url) patch.url = plan.url;
    writeConfigMerged(configPath, patch);
    return { ...plan, path: configPath };
  } catch (e) {
    return { write: false, reason: `write failed: ${e && e.code ? e.code : "error"}` };
  }
}
