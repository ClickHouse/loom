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

import { loomConfigPath, loomTokenPath, readConfigFile, writeConfigMerged } from "./loom-client.mjs";

/** What a run would do, without doing it: {write, url, reason}. Pure. */
export function planMaterialize(env, { configExists, configToken, tokenFileExists }) {
  const option = String(env.CLAUDE_PLUGIN_OPTION_TOKEN || "").trim();
  if (!option) return { write: false, reason: "no option" };
  if (String(env.LOOM_API_KEY || "").trim()) return { write: false, reason: "LOOM_API_KEY set" };
  if (String(configToken || "").trim()) return { write: false, reason: "config has a token" };
  if (tokenFileExists) return { write: false, reason: "token file exists" };
  const url = String(env.LOOM_BASE_URL || env.CLAUDE_PLUGIN_OPTION_SERVER_URL || "")
    .trim()
    .replace(/\/+$/, "");
  return { write: true, url, reason: configExists ? "config lacks a token" : "no config" };
}

/** The primer line when the install token was skipped for a config token
 *  saved for another server: the MCP header helper sends that one to the
 *  server the plugin is installed for, which refuses it. "" otherwise. Pure. */
export function installTokenNotice(plan, serverUrl) {
  if (plan.write || plan.reason !== "config has a token") return "";
  const saved = String(plan.configUrl || "");
  const inUse = String(serverUrl || "").replace(/\/+$/, "");
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
