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
// hooks/loom-record.mjs hands a whole hook payload to a detached copy of
// itself the same way (spawnDetached). That file is not a flusher job: the
// launcher replays it, and the flusher would find nothing in it to send.
//
// A flusher job is named loom-flush-<ms>-<pid>.json and a handed-off payload
// loom-record-<Event>-<ms>-<pid>.json; the client status check counts the
// ones older than ten minutes as stranded.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { writePrivateFile } from "./tmp-state.mjs";

const FLUSHER = fileURLToPath(new URL("../hooks/loom-flush.mjs", import.meta.url));

let lastStamp = 0;

/**
 * `<tmp>/loom-<kind>-<ms>-<pid>.json`. `ms` is the time, or one past the last
 * stamp this process took: the scripts hooks/loom-record.mjs runs dispatch
 * from one process, and two jobs of one millisecond would share a file.
 */
export function jobPath(kind, tmp = os.tmpdir()) {
  lastStamp = Math.max(Date.now(), lastStamp + 1);
  return path.join(tmp, `loom-${kind}-${lastStamp}-${process.pid}.json`);
}

/**
 * Write `text` to a new job file of `kind` and start `node <argv> <job file>`
 * detached: in its own session with no inherited stdio, so it outlives the
 * hook and the host process, though not a machine or container that goes
 * with the host. True when it was started; false (job file removed)
 * otherwise.
 */
export function spawnDetached(argv, text, { kind = "flush", env = process.env, spawnImpl = spawn, tmp } = {}) {
  const file = jobPath(kind, tmp);
  try {
    // The job carries session text: owner-only, like the other tmp state.
    writePrivateFile(file, text);
  } catch {
    return false;
  }
  try {
    const child = spawnImpl(process.execPath, [...argv, file], {
      detached: true,
      stdio: "ignore",
      // A detached child on Windows gets its own console unless hidden: no
      // window flashes per Stop.
      windowsHide: true,
      env,
    });
    // A spawn failure arrives as an 'error' event; unheard, it would crash the hook.
    child.on("error", () => {});
    child.unref();
    return true;
  } catch {
    try {
      fs.unlinkSync(file);
    } catch {
      /* already gone */
    }
    return false;
  }
}

/** Hand `job` to the flusher, whose requests are bounded by `timeoutMs`
 *  (spawnDetached's result). */
export function dispatchFlush(job, { timeoutMs = 15000, spawnImpl = spawn, tmp } = {}) {
  const env = { ...process.env, LOOM_HOOK_TIMEOUT_MS: String(timeoutMs) };
  return spawnDetached([FLUSHER], JSON.stringify(job), { env, spawnImpl, tmp });
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
  const appends = Array.isArray(job.appends) ? job.appends : [];
  const posts = (Array.isArray(job.posts) ? job.posts : []).filter((p) => p && typeof p.op === "string" && p.body);
  const settled = async (calls) =>
    (await Promise.allSettled(calls)).map((r) => (r.status === "fulfilled" ? r.value : null));

  let postsOk = false;
  if (posts.length) {
    const replies = await settled(posts.map((p) => post(p.op, p.body)));
    postsOk = replies.every(Boolean);
  }
  // A sender that moved its cursor ahead optimistically names rollbackTo: on
  // failure the cursor goes back, unless a later job has moved it on since.
  if (!appends.length && job.cursorPath && Number.isInteger(job.rollbackTo) && !postsOk) {
    if (cursor.read(job.cursorPath).trim() === String(job.advanceTo)) cursor.write(job.cursorPath, job.rollbackTo);
  }

  let appendsOk = false;
  if (appends.length) {
    const replies = await settled(appends.map((a) => post("conversation.append", a)));
    appendsOk = replies.every(Boolean);
    noteRecording(appends, replies);
    // The cursor moves only when every turn landed; otherwise the next Stop
    // re-reads and retries (msg_id makes a resend idempotent).
    if (appendsOk && job.cursorPath && Number.isInteger(job.advanceTo)) cursor.write(job.cursorPath, job.advanceTo);
  }
  return { postsOk, appendsOk };
}
