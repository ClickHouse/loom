// Test environments that mean the same on every OS. A test names a home
// and a temp directory with HOME and TMPDIR; Node on Windows reads
// USERPROFILE for os.homedir() and TEMP/TMP for os.tmpdir(), and a Windows
// process needs SystemRoot to open a socket.
import assert from "node:assert/strict";
import fs from "node:fs";

const WINDOWS = process.platform === "win32";

/** `env` plus the Windows names for its HOME and TMPDIR, and SystemRoot. */
export function hostEnv(env) {
  if (!WINDOWS) return env;
  const out = { SystemRoot: process.env.SystemRoot, ...env };
  if (env.HOME !== undefined) out.USERPROFILE = env.HOME;
  if (env.TMPDIR !== undefined) {
    out.TEMP = env.TMPDIR;
    out.TMP = env.TMPDIR;
  }
  return out;
}

/** Run `fn` with this process's temp directory at `tmp`, for helpers that
 *  read os.tmpdir() in process; the old values are back once `fn` (or the
 *  promise it returns) settles. */
export function withTmpdir(tmp, fn) {
  const names = WINDOWS ? ["TMPDIR", "TEMP", "TMP"] : ["TMPDIR"];
  const saved = names.map((n) => process.env[n]);
  const restore = () =>
    names.forEach((n, i) => {
      if (saved[i] === undefined) delete process.env[n];
      else process.env[n] = saved[i];
    });
  for (const n of names) process.env[n] = tmp;
  let result;
  try {
    result = fn();
  } catch (e) {
    restore();
    throw e;
  }
  if (result && typeof result.then === "function") return result.finally(restore);
  restore();
  return result;
}

/** Assert `file`'s permission bits are `mode`, where there are any: NTFS has
 *  no mode bits beyond read-only, and a file under the user's profile or temp
 *  directory is private by the ACL it inherits. */
export function assertMode(file, mode) {
  if (!WINDOWS) assert.equal(fs.statSync(file).mode & 0o777, mode, `${file} mode`);
}
