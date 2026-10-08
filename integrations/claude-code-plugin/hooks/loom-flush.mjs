#!/usr/bin/env node
// Detached flusher: delivers one job a hook handed off (lib/flush-job.mjs
// holds the job shapes and the delivery policy) and removes the job file.
// It outlives the hook that spawned it, so the host never waits on it; its
// requests are bounded by the LOOM_HOOK_TIMEOUT_MS the spawner set, and a
// hard exit backs that bound.
//
// Usage: node loom-flush.mjs <job-file.json>

import { readFileSync, unlinkSync, writeFileSync } from "node:fs";

import { runFlushJob } from "../lib/flush-job.mjs";
import {
  postBounded,
  requestTimeoutMs,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { recordingFromAppends, writeRecording } from "../lib/recording.mjs";

const cursor = {
  read(path) {
    try {
      return readFileSync(path, "utf8");
    } catch {
      return "";
    }
  },
  write(path, n) {
    try {
      writeFileSync(path, String(n));
    } catch {
      /* best-effort */
    }
  },
};

async function main() {
  const jobPath = process.argv[2];
  if (!jobPath) return;
  // Every request is bounded by requestTimeoutMs; the posts run in parallel,
  // then the appends: two bounds, plus slack.
  scheduleExit(0, 2 * requestTimeoutMs() + 5000);
  let job = null;
  try {
    job = JSON.parse(readFileSync(jobPath, "utf8"));
  } catch {
    /* unreadable or garbage: nothing to deliver */
  }
  const token = resolveToken();
  if (job && typeof job === "object") {
    const serverUrl = resolveServerUrl();
    await runFlushJob(job, {
      // Without a token every post fails, so an optimistic cursor rolls back.
      post: (op, body) => (token ? postBounded(op, body, { token, serverUrl }) : Promise.resolve(null)),
      cursor,
      // Keep what the server said about the Recording switch, so the next
      // Stop does not send turns it will not keep.
      noteRecording: (appends, replies) => {
        const first = appends[0] || {};
        writeRecording(serverUrl, first.org, first.namespace, recordingFromAppends(replies));
      },
    });
  }
  try {
    unlinkSync(jobPath);
  } catch {
    /* best-effort */
  }
}

runHookMain(main);
