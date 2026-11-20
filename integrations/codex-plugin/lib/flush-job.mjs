// Detached delivery. A write the host must not wait for (a conversation
// append that embeds server-side, a brief revision that runs a model) is
// written to a job file and handed to hooks/loom-flush.mjs, spawned detached
// and unref'd: the hook exits at once and the flusher outlives it.
//
// Job shapes (the flusher's contract):
//   { appends: [<conversation.append body>], cursorPath, advanceTo }
//       the flusher moves the cursor to advanceTo only when every append landed
//   { posts: [{ op, body }] }
//       any wire op, no cursor
//   { posts, cursorPath, advanceTo, rollbackTo }
//       the sender already moved the cursor; a failed post restores rollbackTo
//
// Job files are named loom-flush-<ms>-<pid>.json; the client status check
// counts the ones older than ten minutes as stranded.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { sessionIsProtected } from "./session-protection.mjs";
import { sessionProtectionContext, withCapturedSessionProtection } from "./session-response.mjs";
import { writePrivateFile } from "./tmp-state.mjs";

const FLUSHER = fileURLToPath(new URL("../hooks/loom-flush.mjs", import.meta.url));

export function flushJobPath(tmp = os.tmpdir(), now = Date.now(), pid = process.pid) {
  return path.join(tmp, `loom-flush-${now}-${pid}.json`);
}

/**
 * Write `job` and spawn the flusher on it with request bound `timeoutMs`.
 * True when the flusher was started; false (job file removed) otherwise.
 */
export function dispatchFlush(job, { timeoutMs = 15000, spawnImpl = spawn, tmp } = {}) {
  const roots = sessionProtectionContext()?.roots;
  if (!roots?.length || roots.some(sessionIsProtected)) return false;
  const jobPath = flushJobPath(tmp);
  try {
    // The job carries session text: owner-only, like the other tmp state.
    writePrivateFile(jobPath, JSON.stringify({ ...job, protectionRoots: roots.map(({ base, id }) => ({ base, id })) }));
  } catch {
    return false;
  }
  try {
    if (roots.some(sessionIsProtected)) {
      fs.unlinkSync(jobPath);
      return false;
    }
    const child = spawnImpl(process.execPath, [FLUSHER, jobPath], {
      detached: true,
      stdio: "ignore",
      // A detached child on Windows gets its own console unless hidden: no
      // window flashes per Stop.
      windowsHide: true,
      env: { ...process.env, LOOM_HOOK_TIMEOUT_MS: String(timeoutMs) },
    });
    // A spawn failure arrives as an 'error' event; unheard, it would crash the hook.
    child.on("error", () => {});
    child.unref();
    return true;
  } catch {
    try {
      fs.unlinkSync(jobPath);
    } catch {
      /* already gone */
    }
    return false;
  }
}

/**
 * Deliver one job; the flusher's policy, with its I/O injected:
 *   post(op, body)          → the reply body, or null on any failure
 *   cursor.read(path)       → the cursor file's text ("" when missing)
 *   cursor.write(path, n)
 *   noteRecording(appends, replies) — what the replies said about recording
 * Returns { postsOk, appendsOk }.
 */
export async function runFlushJob(input, { post, cursor, noteRecording = () => {} }) {
  const job = input && typeof input === "object" ? input : {};
  const roots = Object.freeze(
    (Array.isArray(job.protectionRoots) ? job.protectionRoots : []).map((root) =>
      Object.freeze({ base: root?.base, id: root?.id }),
    ),
  );
  const protectedNow = () => !roots.length || roots.some(sessionIsProtected);
  const rollback = () => {
    if (!(Array.isArray(job.appends) && job.appends.length) && job.cursorPath && Number.isInteger(job.rollbackTo)) {
      if (cursor.read(job.cursorPath).trim() === String(job.advanceTo)) cursor.write(job.cursorPath, job.rollbackTo);
    }
  };
  if (protectedNow()) {
    rollback();
    return { postsOk: false, appendsOk: false };
  }
  const result = withCapturedSessionProtection(roots, async () => {
    const send = (op, body) => (protectedNow() ? null : post(op, body));
    const appends = Array.isArray(job.appends) ? job.appends : [];
    const posts = (Array.isArray(job.posts) ? job.posts : []).filter((p) => p && typeof p.op === "string" && p.body);
    const settled = async (calls) =>
      (await Promise.allSettled(calls)).map((r) => (r.status === "fulfilled" ? r.value : null));

    let postsOk = false;
    if (posts.length) {
      const replies = await settled(posts.map((p) => send(p.op, p.body)));
      postsOk = replies.every(Boolean) && !protectedNow();
    }
    // A failed or suppressed post must not acknowledge an optimistic cursor.
    if (!postsOk) rollback();

    let appendsOk = false;
    if (appends.length) {
      const replies = await settled(appends.map((a) => send("conversation.append", a)));
      appendsOk = replies.every(Boolean) && !protectedNow();
      if (!protectedNow()) noteRecording(appends, replies);
      if (appendsOk && !protectedNow() && job.cursorPath && Number.isInteger(job.advanceTo)) {
        cursor.write(job.cursorPath, job.advanceTo);
      }
    }
    return { postsOk, appendsOk };
  });
  if (result !== undefined) return result;
  rollback();
  return { postsOk: false, appendsOk: false };
}
