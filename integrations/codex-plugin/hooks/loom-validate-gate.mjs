#!/usr/bin/env node
// loom-validate-gate — Stop hook.
//
// On Stop, collect the memory keys this session wrote (assistant tool_use
// blocks in the transcript naming memory.set / update / update_by_id /
// apply), ask `namespace.validate` for a report scoped to exactly those
// keys, and block the stop ONCE per distinct key set with the findings that
// name them. See lib/validate-gate.mjs for the rules.
//
// Fail-open everywhere: no token, no transcript, no keys written, server
// down, second stop on the same keys — the agent stops as normal.
// LOOM_VALIDATE_GATE=off disables.

import {
  postBounded,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  scheduleExit,
  envSwitch,
} from "../lib/loom-client.mjs";
import { readSessionState, writeSessionState } from "../lib/tmp-state.mjs";
import { readAllLines } from "../lib/transcript.mjs";
import { decideBlock, gatePayload, keysSignature, renderValidate, writtenKeys } from "../lib/validate-gate.mjs";

const ENABLED = envSwitch("LOOM_VALIDATE_GATE", true);
async function main() {
  if (!ENABLED) {
    scheduleExit(0);
    return;
  }
  const token = resolveToken();
  if (!token) {
    scheduleExit(0);
    return;
  }
  const payload = await readStdinJson();
  // A Stop fired by our own block must never block again.
  if (payload.stop_hook_active) {
    scheduleExit(0);
    return;
  }

  const transcript = payload.transcript_path || "";
  if (!transcript) {
    scheduleExit(0);
    return;
  }
  const keys = writtenKeys(readAllLines(transcript));
  if (!keys.length) {
    scheduleExit(0);
    return;
  }

  const decision = decideBlock(readSessionState("validategate", payload.session_id), keysSignature(keys));
  writeSessionState("validategate", payload.session_id, decision.cache);
  if (!decision.block) {
    scheduleExit(0);
    return;
  }

  const scope = resolveScope(String(payload.cwd || process.cwd()));
  const report = await postBounded(
    "namespace.validate",
    { org: scope.org, namespace: scope.namespace, keys, sample_limit: 5, consumer: "claude-code-validategate" },
    { token, serverUrl: resolveServerUrl() },
  );
  const out = gatePayload(renderValidate(report));
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

main().catch(() => scheduleExit(0));
