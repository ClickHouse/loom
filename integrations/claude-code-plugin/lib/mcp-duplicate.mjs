// The plugin mounts the Loom server from its own `.mcp.json`.
// A user-scope entry in ~/.claude.json pointing at the same server lists every
// Loom tool a second time (a listing read on every turn), and its
// copy carries no code scope and no header-helper token — models call either.
// The SessionStart primer says so once a day; it costs nothing per turn.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const NOTICE_EVERY_MS = 24 * 3600 * 1000;

function _host(url) {
  try {
    const u = new URL(String(url || ""));
    return `${u.protocol}//${u.host}`.toLowerCase();
  } catch {
    return "";
  }
}

/** Names of ~/.claude.json user-scope MCP entries that point at `serverUrl`. */
export function duplicateEntries(claudeJson, serverUrl) {
  const want = _host(serverUrl);
  const servers = (claudeJson && typeof claudeJson === "object" && claudeJson.mcpServers) || {};
  if (!want) return [];
  return Object.entries(servers)
    .filter(([, s]) => s && typeof s === "object" && _host(s.url) === want)
    .map(([name]) => name);
}

export function renderDuplicateNotice(names) {
  if (!names.length) return "";
  const n = names[0];
  return (
    `Loom notice: Loom is registered twice in Claude Code — this plugin's own mount and the ` +
    `user-scope MCP entry "${n}" in ~/.claude.json. Every turn lists both tool sets, and the ` +
    `user-scope copy carries no code scope for this checkout. Prefer the plugin's tools ` +
    `(mcp__plugin_loom_loom__*); the user can remove the duplicate with ` +
    `\`claude mcp remove ${n} -s user\`.`
  );
}

/** The notice, at most once per NOTICE_EVERY_MS; "" otherwise or on any error. */
export function duplicateNotice({
  serverUrl,
  home = homedir(),
  now = () => Date.now(),
  statePath = join(home, ".loom", ".mcp-duplicate-notice"),
} = {}) {
  try {
    const cfg = JSON.parse(readFileSync(join(home, ".claude.json"), "utf8"));
    const names = duplicateEntries(cfg, serverUrl);
    if (!names.length) return "";
    let last = null; // never shown
    try {
      last = Number(readFileSync(statePath, "utf8").trim());
    } catch {
      last = null;
    }
    if (Number.isFinite(last) && now() - last < NOTICE_EVERY_MS) return "";
    mkdirSync(dirname(statePath), { recursive: true, mode: 0o700 });
    writeFileSync(statePath, String(now()), { mode: 0o600 });
    return renderDuplicateNotice(names);
  } catch {
    return "";
  }
}
