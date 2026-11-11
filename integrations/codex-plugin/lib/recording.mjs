// Whether conversation recording is on for a namespace.
//
// The owner's Record conversations switch in the dashboard (`conversation_logging_enabled`) is
// the only thing that decides whether turns are kept. The server accepts an
// append into a namespace with the switch off and stores nothing
// (`stored: false, reason: "logging_disabled"`), so a client that ignored
// the reply would send every turn for nothing and its primer would claim
// logging it does not do. This module keeps what the server said, per
// server + org + namespace, in a small tmp file:
//
//   - the flusher records each append batch's verdict;
//   - the conversation hook skips appends while a fresh "off" is cached,
//     re-asking once the cache is older than RECORDING_TTL_MS, so the
//     owner turning it back on is picked up;
//   - the SessionStart primer states the truth, from the cache or one
//     `namespace.get`.
//
// Pure helpers plus two tiny file reads/writes; every failure reads as
// "unknown", and unknown sends the turns.

import os from "node:os";
import { readJsonFile, tmpStatePath, writeJsonFile } from "./tmp-state.mjs";

export const RECORDING_TTL_MS = 10 * 60 * 1000;

/** The cache file for one (server, org, namespace). */
export function recordingCachePath(serverUrl, org, namespace, tmp = os.tmpdir()) {
  return tmpStatePath("recording", `${serverUrl || ""}|${org || ""}|${namespace || ""}`, { tmp });
}

/** true / false from a cache younger than `ttlMs`; null when unknown. */
export function readRecording(serverUrl, org, namespace, { now = Date.now(), ttlMs = RECORDING_TTL_MS, tmp } = {}) {
  const c = readJsonFile(recordingCachePath(serverUrl, org, namespace, tmp), null);
  if (!c || typeof c.on !== "boolean" || !(now - Number(c.ts) < ttlMs)) return null;
  return c.on;
}

export function writeRecording(serverUrl, org, namespace, on, { now = Date.now(), tmp } = {}) {
  if (typeof on !== "boolean") return;
  writeJsonFile(recordingCachePath(serverUrl, org, namespace, tmp), { ts: now, on });
}

/** What a batch of `conversation.append` replies says about the switch:
 *  false when the server refused to keep a turn because recording is off,
 *  true when it kept one, null when the replies say neither (failures). */
export function recordingFromAppends(replies) {
  let on = null;
  for (const r of replies || []) {
    if (!r || typeof r !== "object") continue;
    if (r.stored === false && r.reason === "logging_disabled") return false;
    if (r.stored === true) on = true;
  }
  return on;
}

/** The switch from a `namespace.get` reply, or null. */
export function recordingFromNamespace(body) {
  if (!body || body.found !== true) return null;
  const v = body.conversation_logging_enabled;
  return typeof v === "boolean" ? v : null;
}

/** The primer's conversation line for a known or unknown switch. */
export function recordingPrimerLine(on, namespace) {
  const dont = "Do NOT call loom_conversation_create / loom_conversation_append yourself";
  if (on === false) {
    return (
      `# Loom: conversation recording is off for ${namespace} (the owner's Record conversations switch on the dashboard's Conversations page); ` +
      `turns are not kept. ${dont}: they would not be stored either.`
    );
  }
  if (on === true) {
    return (
      "# Loom: conversation logging is automatic (handled by the Stop hook). " +
      `${dont} — doing so would duplicate every turn.`
    );
  }
  return (
    `# Loom: the Stop hook records this conversation when recording is on for ${namespace} ` +
    `(the owner's Record conversations switch on the dashboard's Conversations page). ${dont}.`
  );
}
