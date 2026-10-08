/**
 * Logging with one redaction point. A `Logger` is four
 * methods; the SDK filters by level, writes one line per completed call at
 * `info`, retries at `warn`, final failures at `error`, and headers and
 * body sizes at `debug`. Credentials never reach the sink: `redactHeaders`
 * is the single place a header value is prepared for a log line.
 */

import { LoomError } from "./errors.js";

export type LogLevel = "debug" | "info" | "warn" | "error" | "off";

export interface Logger {
  debug(message: string, ...args: unknown[]): void;
  info(message: string, ...args: unknown[]): void;
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
}

export const LOG_LEVELS: readonly LogLevel[] = ["debug", "info", "warn", "error", "off"];
export const DEFAULT_LOG_LEVEL: LogLevel = "warn";

const RANK: Record<LogLevel, number> = { debug: 0, info: 1, warn: 2, error: 3, off: 4 };
const PREFIX = "[loom]";

// `LOOM_LOG_LEVEL` is shared with the server and the Python SDK, which take
// Python's level names.
const STDLIB_ALIASES: Record<string, LogLevel> = {
  warning: "warn",
  critical: "error",
  fatal: "error",
  notset: "debug",
};

/** A level from a string, naming where it came from when it is not one. */
export function parseLogLevel(value: string, source: string): LogLevel {
  const v = value.trim().toLowerCase();
  if ((LOG_LEVELS as readonly string[]).includes(v)) return v as LogLevel;
  const alias = STDLIB_ALIASES[v];
  if (alias) return alias;
  throw new LoomError(`${source}: invalid log level "${value}"; expected one of ${LOG_LEVELS.join(", ")}`);
}

/** The default sink: `console`, prefixed `[loom]`. */
export const consoleLogger: Logger = {
  debug: (m, ...a) => console.debug(`${PREFIX} ${m}`, ...a),
  info: (m, ...a) => console.info(`${PREFIX} ${m}`, ...a),
  warn: (m, ...a) => console.warn(`${PREFIX} ${m}`, ...a),
  error: (m, ...a) => console.error(`${PREFIX} ${m}`, ...a),
};

const drop = (): void => {};

/** `sink` filtered to `level` and above. */
export function withLevel(sink: Logger, level: LogLevel): Logger {
  const on = (at: LogLevel) => RANK[at] >= RANK[level];
  return {
    debug: on("debug") ? (m, ...a) => sink.debug(m, ...a) : drop,
    info: on("info") ? (m, ...a) => sink.info(m, ...a) : drop,
    warn: on("warn") ? (m, ...a) => sink.warn(m, ...a) : drop,
    error: on("error") ? (m, ...a) => sink.error(m, ...a) : drop,
  };
}

export const NOOP_LOGGER: Logger = { debug: drop, info: drop, warn: drop, error: drop };

/** Credential headers: the scheme and the last four characters survive, nothing else. */
const CREDENTIAL_HEADERS: ReadonlySet<string> = new Set([
  "authorization",
  "proxy-authorization",
  "x-api-key",
  "cookie",
  "set-cookie",
]);

/** A secret as a log line or `toJSON` may show it: `…` and, past 8 characters, its last four. */
export function redactSecret(secret: string): string {
  return secret.length > 8 ? `…${secret.slice(-4)}` : "…";
}

export function redactHeaderValue(name: string, value: string): string {
  if (!CREDENTIAL_HEADERS.has(name.toLowerCase())) return value;
  const m = /^(\S+)\s+(\S+)$/.exec(value.trim());
  const [scheme, secret] = m ? [m[1], m[2]] : ["", value.trim()];
  return `${scheme ? `${scheme} ` : ""}${redactSecret(secret)}`;
}

/** A copy of `headers` safe for a log line. The one redaction point. */
export function redactHeaders(headers: Record<string, string> | Headers): Record<string, string> {
  const out: Record<string, string> = {};
  if (headers instanceof Headers) {
    headers.forEach((v, k) => {
      out[k] = redactHeaderValue(k, v);
    });
    return out;
  }
  for (const [k, v] of Object.entries(headers)) out[k] = redactHeaderValue(k, v);
  return out;
}
