// The small state files hooks keep in $TMPDIR between invocations: per
// session (a cursor, a dedupe set, a once-per-session marker) or per server
// scope (a cached answer). One naming rule for all of them:
// `loom-<kind>-<first 12 hex of sha1(key)><ext>`, so a session id or a URL
// never reaches the file system as a path component.
//
// Reads return the fallback and writes return false on any error: a hook
// that cannot keep state behaves as on its first run.
//
// Every write goes through writePrivateFile: these files can hold prompt and
// reply text, and their names are predictable in a shared tmp dir.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/** `$TMPDIR/loom-<kind>-<sha1(key)[0:12]><ext>`. */
export function tmpStatePath(kind, key, { ext = ".json", tmp = os.tmpdir() } = {}) {
  const h = crypto
    .createHash("sha1")
    .update(String(key ?? ""))
    .digest("hex")
    .slice(0, 12);
  return path.join(tmp, `loom-${kind}-${h}${ext}`);
}

/** The per-session state file of `kind`; a missing id shares "nosession". */
export function sessionStatePath(kind, sessionId, opts = {}) {
  return tmpStatePath(kind, String(sessionId || "nosession"), opts);
}

/** A hook's per-session object: {} when there is none, or it does not parse
 *  as a plain object. */
export function readSessionState(kind, sessionId, opts = {}) {
  const v = readJsonFile(sessionStatePath(kind, sessionId, opts), {});
  return v && typeof v === "object" && !Array.isArray(v) ? v : {};
}

/** Best effort: when tmp is not writable the next call starts from {}. */
export function writeSessionState(kind, sessionId, value, opts = {}) {
  return writeJsonFile(sessionStatePath(kind, sessionId, opts), value);
}

/** The parsed JSON in `file`, or `fallback` when it is missing or malformed. */
export function readJsonFile(file, fallback = {}) {
  try {
    const v = JSON.parse(fs.readFileSync(file, "utf8"));
    return v === null || v === undefined ? fallback : v;
  } catch {
    return fallback;
  }
}

/** Write (or append) `text` to `file` with mode 0600. Throws, writing
 *  nothing, when the path is a symlink, not a regular file, or owned by
 *  another user: someone else may have planted it in a shared tmp dir. */
export function writePrivateFile(file, text, { append = false } = {}) {
  const c = fs.constants;
  // O_NONBLOCK so a planted FIFO fails the open instead of hanging the hook.
  const flags = c.O_WRONLY | c.O_CREAT | (append ? c.O_APPEND : c.O_TRUNC) | (c.O_NOFOLLOW ?? 0) | (c.O_NONBLOCK ?? 0);
  const fd = fs.openSync(file, flags, 0o600);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) throw new Error(`${file} is not a regular file`);
    if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
      throw new Error(`${file} belongs to another user`);
    }
    // A file left by an older version keeps its mode on open; tighten it.
    fs.fchmodSync(fd, 0o600);
    fs.writeSync(fd, text);
  } finally {
    fs.closeSync(fd);
  }
}

/** Remove `file`; true when it is gone. */
export function removeFile(file) {
  try {
    fs.unlinkSync(file);
    return true;
  } catch (e) {
    return e && e.code === "ENOENT";
  }
}

/** Write `value` as JSON; true when it was written. */
export function writeJsonFile(file, value) {
  try {
    writePrivateFile(file, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

/** An integer cursor kept as text; 0 when missing or malformed. */
export function readCursor(file) {
  try {
    const n = parseInt(fs.readFileSync(file, "utf8").trim(), 10);
    return Number.isInteger(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}

export function writeCursor(file, n) {
  try {
    writePrivateFile(file, String(n));
    return true;
  } catch {
    return false;
  }
}
