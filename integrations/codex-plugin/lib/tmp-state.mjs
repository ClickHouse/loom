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

/** The text of `file`. Throws when the path is a symlink, not a regular file, a
 *  hard link, or owned by another user, like writePrivateFile: a planted file is
 *  not our state. */
export function readPrivateFile(file) {
  const c = fs.constants;
  const fd = fs.openSync(file, c.O_RDONLY | (c.O_NOFOLLOW ?? 0) | (c.O_NONBLOCK ?? 0));
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) throw new Error(`${file} is not a regular file`);
    if (st.nlink !== 1) throw Object.assign(new Error(`${file} has other hard links`), { code: "ELINKED" });
    if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
      throw new Error(`${file} belongs to another user`);
    }
    return fs.readFileSync(fd, "utf8");
  } finally {
    fs.closeSync(fd);
  }
}

/** Set `file`'s mtime to now, with readPrivateFile's checks; true when done.
 *  Opened for writing: Windows sets a file's times only through a handle
 *  that may write. */
export function touchPrivateFile(file) {
  try {
    const c = fs.constants;
    const fd = fs.openSync(file, c.O_RDWR | (c.O_NOFOLLOW ?? 0) | (c.O_NONBLOCK ?? 0));
    try {
      const st = fs.fstatSync(fd);
      if (!st.isFile() || st.nlink !== 1) return false;
      if (typeof process.getuid === "function" && st.uid !== process.getuid()) return false;
      const now = new Date();
      fs.futimesSync(fd, now, now);
      return true;
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return false;
  }
}

/** The parsed JSON in `file`, or `fallback` when it is missing, malformed or not ours. */
export function readJsonFile(file, fallback = {}) {
  try {
    const v = JSON.parse(readPrivateFile(file));
    return v === null || v === undefined ? fallback : v;
  } catch {
    return fallback;
  }
}

/** Write (or append) `text` to `file` with mode 0600. Throws, changing
 *  nothing, when the path is a symlink, not a regular file, a hard link, or
 *  owned by another user: someone else may have planted it in a shared tmp dir. */
export function writePrivateFile(file, text, { append = false } = {}) {
  const c = fs.constants;
  // No O_TRUNC: truncate only after the checks, so a planted hard link to a
  // file of ours is refused before it is emptied. O_NONBLOCK so a planted
  // FIFO fails the open instead of hanging the hook.
  const flags = c.O_WRONLY | c.O_CREAT | (append ? c.O_APPEND : 0) | (c.O_NOFOLLOW ?? 0) | (c.O_NONBLOCK ?? 0);
  const fd = fs.openSync(file, flags, 0o600);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) throw new Error(`${file} is not a regular file`);
    if (st.nlink !== 1) throw Object.assign(new Error(`${file} has other hard links`), { code: "ELINKED" });
    if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
      throw new Error(`${file} belongs to another user`);
    }
    // An existing file keeps its mode on open; tighten it.
    fs.fchmodSync(fd, 0o600);
    if (!append) fs.ftruncateSync(fd, 0);
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
    const n = parseInt(readPrivateFile(file).trim(), 10);
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

export const STALE_STATE_MS = 24 * 60 * 60 * 1000;
// tmpStatePath's names but the code shunt's decision log (a log the user reads, not
// session state), and the recalled-key and code-trace stashes named by session id.
const STATE_FILE_RE =
  /^loom-(?!shunt-log-)(?:[a-z0-9-]+-[0-9a-f]{12}(?:\.[a-z]+)?|(?:recalled|code-trace)-[\w-]+\.json)$/;

/** Remove this user's state files in `tmp` untouched for `maxAgeMs`: a session's
 *  journal and cursors outlive it, since SessionEnd hooks run in parallel and none
 *  of them can know it is the last reader. Returns how many were removed. */
export function sweepStaleStateFiles({ tmp = os.tmpdir(), maxAgeMs = STALE_STATE_MS, now = Date.now() } = {}) {
  let removed = 0;
  let names = [];
  try {
    names = fs.readdirSync(tmp);
  } catch {
    return 0;
  }
  const uid = typeof process.getuid === "function" ? process.getuid() : null;
  for (const name of names) {
    if (!STATE_FILE_RE.test(name)) continue;
    const file = path.join(tmp, name);
    try {
      const st = fs.lstatSync(file);
      if (!st.isFile() || (uid !== null && st.uid !== uid) || now - st.mtimeMs < maxAgeMs) continue;
      fs.unlinkSync(file);
      removed += 1;
    } catch {
      /* gone already, or not ours to remove */
    }
  }
  return removed;
}
