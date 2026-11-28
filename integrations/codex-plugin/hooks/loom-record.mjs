#!/usr/bin/env node
// The recording hooks' launcher. A script that only sends data (a tool
// observation, the conversation, usage, the working-memory note, the run's
// close, the session's hand-offs) prints nothing the host reads, so the host
// has no reason to wait for it.
//
//   node loom-record.mjs <Event> [client]
//       what the host runs: the payload on stdin goes to a private job file,
//       one detached copy of this script is started on it, and the hook
//       exits.
//   node loom-record.mjs <Event> <client or -> <job file>
//       the detached copy: runs the event's recording scripts in this one
//       process, each as if the host had started it, then removes the job
//       file.
//
// The hand-off outlives the session: the job file is on disk before the hook
// returns, and the copy runs in its own session with no inherited stdio
// (lib/flush-job.mjs), so neither the hook's exit nor the host's ends it. A
// copy that cannot be started records nothing for that event; no cursor has
// moved, so the next Stop sends the conversation turns again.
//
// The host side imports nothing it does not use: it runs after every tool
// call.

import { spawnDetached } from "../lib/flush-job.mjs";
import { readStdinText } from "../lib/hook-stdin.mjs";
import { readPrivateFile, removeFile } from "../lib/tmp-state.mjs";

/** The scripts each event records with, in the order they start: loom-run
 *  reads the recalled-key stash before loom-session-memory removes it. */
export const RECORDERS = {
  PostToolUse: ["loom-capture"],
  PostToolUseFailure: ["loom-capture"],
  Stop: ["loom-capture", "loom-conversation", "loom-usage", "loom-working-memory"],
  SessionEnd: ["loom-capture", "loom-conversation", "loom-run", "loom-session-memory", "loom-session-brief"],
};

async function handOff(script, event, client) {
  spawnDetached([script, event, client || "-"], await readStdinText());
}

async function record(scripts, jobPath) {
  let raw = null;
  try {
    raw = readPrivateFile(jobPath);
  } catch {
    /* missing, or not a file of ours: nothing to record */
  }
  if (raw !== null) {
    const { runHooksTogether } = await import("../lib/loom-client.mjs");
    await runHooksTogether(
      raw,
      scripts.map((name) => new URL(`./${name}.mjs`, import.meta.url).href),
    );
  }
  removeFile(jobPath);
}

const [, script, event, client, jobPath] = process.argv;
// Every path ends quietly: a hook never fails the host session.
if (Object.hasOwn(RECORDERS, event)) {
  if (jobPath) record(RECORDERS[event], jobPath).catch(() => {});
  // Exits itself: a host that never closes stdin would hold the process open.
  else handOff(script, event, client).finally(() => process.exit(0));
}
