// The plugin's minimum Loom server version. Each plugin manifest declares
// `minServerVersion`; SessionStart compares it with the `version` the server's
// `GET /v1/health` reports and prints one line when the server is older.
// Anything unreadable (no manifest field, a server without the field, a
// version that is not SemVer) says nothing: the check never stands between
// the user and a session.

import { readFileSync } from "node:fs";

// The manifests sit beside lib/: Claude Code's in .claude-plugin/, Codex's in
// .codex-plugin/ (lib/ is synced into both plugins).
const MANIFESTS = ["../.claude-plugin/plugin.json", "../.codex-plugin/plugin.json"];

const SEMVER = /^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

/** [major, minor, patch] of a SemVer string, or null. A prerelease or build
 *  suffix is dropped, so `1.2.0-rc.1` counts as 1.2.0: a release candidate
 *  of the minimum is not warned about. */
export function parseVersion(text) {
  const m = SEMVER.exec(String(text ?? "").trim());
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

/** True only when both parse and `version` is older than `minimum`. */
export function isOlder(version, minimum) {
  const a = parseVersion(version);
  const b = parseVersion(minimum);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] < b[i];
  }
  return false;
}

/** This plugin's `{ version, minServerVersion }` from its manifest; empty
 *  strings when no manifest can be read. */
export function pluginManifest(base = import.meta.url) {
  for (const rel of MANIFESTS) {
    try {
      const m = JSON.parse(readFileSync(new URL(rel, base), "utf8"));
      return { version: String(m.version || ""), minServerVersion: String(m.minServerVersion || "") };
    } catch {
      // Not this plugin's layout, or unreadable: try the other one.
    }
  }
  return { version: "", minServerVersion: "" };
}

/** The sentence about a server older than the plugin's minimum, or "". */
export function serverVersionMessage(health, { serverUrl, manifest = pluginManifest() }) {
  const version = health && typeof health.version === "string" ? health.version : "";
  if (!isOlder(version, manifest.minServerVersion)) return "";
  return (
    `the Loom server at ${serverUrl} runs ${version.trim()}, older than ${manifest.minServerVersion}, ` +
    `the oldest this plugin (${manifest.version}) supports; upgrade the server.`
  );
}

/** SessionStart's `loom:` line for a `/v1/health` body, or "". */
export function serverVersionLine(health, options) {
  const message = serverVersionMessage(health, options);
  return message ? `loom: ${message}` : "";
}
