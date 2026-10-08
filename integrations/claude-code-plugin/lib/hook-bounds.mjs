// Pure helpers for the hooks' request and exit bounds.
//
// The bound is a multiple of the round trip SessionStart measures against
// `/v1/health` (cached per server), clamped to a floor the capture path
// tolerates and a ceiling no tool call should wait: one fixed number either
// drops captures on a slow link or holds tool calls on a fast one. An env
// value wins (the doctor flags it); no measurement means the default.

import { tmpStatePath } from "./tmp-state.mjs";

export const DEFAULT_BOUND_MS = 3000;
export const MIN_BOUND_MS = 3000;
export const MAX_BOUND_MS = 8000;
export const RTT_FACTOR = 5;
export const RTT_TTL_MS = 6 * 60 * 60 * 1000;

/** The bound a measured round trip earns: RTT_FACTOR × rtt, clamped. */
export function derivedBoundMs(rttMs) {
  const rtt = Number(rttMs);
  if (!Number.isFinite(rtt) || rtt <= 0) return DEFAULT_BOUND_MS;
  return Math.min(MAX_BOUND_MS, Math.max(MIN_BOUND_MS, Math.round(rtt * RTT_FACTOR)));
}

/**
 * The effective bound: an explicit env value (> 0) wins; else a cached
 * round trip younger than RTT_TTL_MS derives it; else the default.
 * `cache` is the parsed `{ rtt_ms, ts }` file or null.
 */
export function boundMs({ envValue, cache, now = Date.now() } = {}) {
  const env = Number(envValue);
  if (Number.isFinite(env) && env > 0) return { ms: env, source: "env" };
  if (cache && Number(cache.rtt_ms) > 0 && now - Number(cache.ts || 0) < RTT_TTL_MS) {
    return { ms: derivedBoundMs(cache.rtt_ms), source: "rtt" };
  }
  return { ms: DEFAULT_BOUND_MS, source: "default" };
}

/** Where the round trip for `serverUrl` is cached: one file per server. */
export function rttCachePath(tmpdir, serverUrl) {
  return tmpStatePath("rtt", String(serverUrl || ""), { tmp: tmpdir });
}
