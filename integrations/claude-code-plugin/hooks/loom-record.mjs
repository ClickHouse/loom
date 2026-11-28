#!/usr/bin/env node
// One process per event for the scripts the host need not start one by one.
// A script that only sends data (a tool observation, the conversation, usage,
// the working-memory note, the run's open and close, the namespace audit, the
// session's hand-offs) prints nothing the host reads, so the host does not
// wait for it: one detached process runs an event's recording scripts. The
// two lookups a prompt does wait for run in the hook itself, side by side,
// and their context goes to the host as one answer.
//
//   node loom-record.mjs <Event> [client]
//       what the host runs, with the payload on stdin. The payload goes to
//       a private job file, one detached copy of this script is started on
//       it, and the hook exits; at a prompt it first runs the lookups and
//       prints what they found. With no credential there is nothing to
//       record, and nothing is started.
//   node loom-record.mjs <Event> <client or -> <job file>
//       the detached copy, and the replay of a job file a dead copy left:
//       runs the event's recording scripts in this one process, each as if
//       the host had started it, then removes the job file.
//
// A hand-off outlives the hook, the session and the host process: the job
// file is on disk before the hook returns, and the copy runs in its own
// session with no inherited stdio (lib/flush-job.mjs), so neither's exit
// ends it. It does not outlive the machine: a host torn down when the client
// exits (a container whose entrypoint is the client) loses what a copy had
// not sent by then, the session's close first. A copy that cannot be started
// records nothing for that event, and one that dies leaves its job file to
// be replayed; no cursor has moved, so the next Stop sends the conversation
// turns again.

import { spawnDetached } from "../lib/flush-job.mjs";
import { agentMode, printHookAnswer, readStdinText, resolveToken, runHooksTogether } from "../lib/loom-client.mjs";
import { readPrivateFile, removeFile } from "../lib/tmp-state.mjs";

/** The scripts each event records with, in the order they start. At
 *  SessionEnd loom-run reads the recalled-key stash before
 *  loom-session-memory removes it. */
export const RECORDERS = {
  UserPromptSubmit: ["loom-run", "loom-namespace-health"],
  PostToolUse: ["loom-capture"],
  PostToolUseFailure: ["loom-capture"],
  Stop: ["loom-capture", "loom-conversation", "loom-usage", "loom-working-memory"],
  SessionEnd: ["loom-capture", "loom-conversation", "loom-run", "loom-session-memory", "loom-session-brief"],
};

/** The scripts whose answer the host waits for: they run in the hook. */
export const LOOKUPS = {
  UserPromptSubmit: ["loom-prompt-pack", "loom-prompt-recall"],
};

function run(raw, names) {
  return runHooksTogether(
    raw,
    names.map((name) => new URL(`./${name}.mjs`, import.meta.url).href),
  );
}

/** One answer for the host from the context each lookup has, in their order,
 *  each on a line of its own; null when none has any. */
export function mergedContext(event, answers) {
  const texts = (answers || []).map((a) => a?.hookSpecificOutput?.additionalContext).filter(Boolean);
  if (!texts.length) return null;
  return { hookSpecificOutput: { hookEventName: event, additionalContext: texts.join("\n") } };
}

async function hook(script, event, client) {
  const raw = await readStdinText();
  // Left open, a stdin its host never closes would hold this process.
  process.stdin.destroy();
  // An agent's token is left to the copy: renewing one is a request.
  if (agentMode() || resolveToken()) {
    spawnDetached([script, event, client || "-"], raw, { kind: `record-${event}` });
  }
  const answer = LOOKUPS[event] ? mergedContext(event, await run(raw, LOOKUPS[event])) : null;
  if (answer) printHookAnswer(answer, () => process.exit(0));
  else process.exit(0);
}

async function replay(event, jobPath) {
  let raw = null;
  try {
    raw = readPrivateFile(jobPath);
  } catch {
    /* missing, or not a file of ours: nothing to record */
  }
  if (raw !== null) await run(raw, RECORDERS[event]);
  removeFile(jobPath);
}

const [, script, event, client, jobPath] = process.argv;
// Every path ends quietly: a hook never fails the host session.
if (Object.hasOwn(RECORDERS, event)) {
  if (jobPath) replay(event, jobPath).catch(() => {});
  else hook(script, event, client).catch(() => process.exit(0));
}
