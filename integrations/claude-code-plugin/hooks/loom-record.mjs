#!/usr/bin/env node
// The recording hooks' launcher. A script that only sends data (a tool
// observation, the conversation, usage, the working-memory note, the run's
// close, the session's hand-offs) prints nothing the host reads, so one
// process runs an event's scripts, and the host waits for it only where
// nothing may be left to finish the work afterwards.
//
//   node loom-record.mjs <Event> [client]
//       what the host runs, with the payload on stdin.
//       After a tool call and at Stop the payload goes to a private job
//       file, one detached copy of this script is started on it, and the
//       hook exits. With no credential there is nothing to record, and
//       nothing is started.
//       At SessionEnd the event's scripts run here and the hook exits when
//       they have returned: a host that is torn down when the client exits
//       (a container whose entrypoint is the client, a CI runner that kills
//       what a job leaves behind) would end a detached copy before it had
//       sent anything.
//   node loom-record.mjs <Event> <client or -> <job file>
//       the detached copy, and the replay of a job file a dead copy left:
//       runs the event's scripts in this one process, each as if the host
//       had started it, then removes the job file.
//
// A hand-off outlives the hook and the host process: the job file is on disk
// before the hook returns, and the copy runs in its own session with no
// inherited stdio (lib/flush-job.mjs), so neither's exit ends it. It does
// not outlive the machine: a host torn down at the client's exit loses what
// a copy had not sent by then. A copy that cannot be started records
// nothing for that event, and one that dies leaves its job file to be
// replayed; no cursor has moved, so the next Stop sends the conversation
// turns again.

import { spawnDetached } from "../lib/flush-job.mjs";
import { agentMode, readStdinText, resolveToken, runHooksTogether } from "../lib/loom-client.mjs";
import { readPrivateFile, removeFile } from "../lib/tmp-state.mjs";

/** The scripts each event records with, in the order they start. At
 *  SessionEnd loom-run reads the recalled-key stash before
 *  loom-session-memory removes it when its run is begun; a run whose begin
 *  it has to ask again waits for that answer first and can close without
 *  the keys. */
export const RECORDERS = {
  PostToolUse: ["loom-capture"],
  PostToolUseFailure: ["loom-capture"],
  Stop: ["loom-capture", "loom-conversation", "loom-usage", "loom-working-memory"],
  SessionEnd: ["loom-capture", "loom-conversation", "loom-run", "loom-session-memory", "loom-session-brief"],
};

function run(event, raw) {
  return runHooksTogether(
    raw,
    RECORDERS[event].map((name) => new URL(`./${name}.mjs`, import.meta.url).href),
  );
}

async function hook(script, event, client) {
  const raw = await readStdinText();
  if (event === "SessionEnd") return run(event, raw);
  // An agent's token is left to the copy: renewing one is a request.
  if (agentMode() || resolveToken()) {
    spawnDetached([script, event, client || "-"], raw, { kind: `record-${event}` });
  }
  // Exits itself: a host that never closes stdin would hold the process open.
  process.exit(0);
}

async function replay(event, jobPath) {
  let raw = null;
  try {
    raw = readPrivateFile(jobPath);
  } catch {
    /* missing, or not a file of ours: nothing to record */
  }
  if (raw !== null) await run(event, raw);
  removeFile(jobPath);
}

const [, script, event, client, jobPath] = process.argv;
// Every path ends quietly: a hook never fails the host session.
if (Object.hasOwn(RECORDERS, event)) {
  if (jobPath) replay(event, jobPath).catch(() => {});
  else hook(script, event, client).catch(() => process.exit(0));
}
