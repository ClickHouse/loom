#!/usr/bin/env node
// loom-namespace-health — Stop hook.
//
// On the FIRST Stop of a session, ask `namespace.audit` (one day, no
// samples) how the session's namespace is being used and, when it carries a
// critical finding, hand the agent ONE line naming the codes and
// /loom-optimize — as a block-once reason, the one channel a Stop hook has
// to the model. Later stops in the session pass without a call. See
// lib/namespace-health.mjs for the rules.
//
// Fail-open everywhere: no token, no scope, server down or slower than the
// hook's bound, a healthy namespace, a stop this hook caused — the agent
// stops as normal and nothing is printed. LOOM_NAMESPACE_HEALTH=off disables.

import {
  envSwitch,
  postBounded,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { auditBody, decideAudit, healthPayload, renderHealth } from "../lib/namespace-health.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

const ENABLED = envSwitch("LOOM_NAMESPACE_HEALTH", true);

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
  // A Stop fired by our own block (or another gate's) must never block again.
  if (payload.stop_hook_active) {
    scheduleExit(0);
    return;
  }

  const cp = sessionStatePath("nshealth", payload.session_id);
  const decision = decideAudit(readJsonFile(cp));
  if (!decision.audit) {
    scheduleExit(0);
    return;
  }
  writeJsonFile(cp, decision.cache);

  const scope = resolveScope(String(payload.cwd || process.cwd()));
  if (!scope || !scope.namespace) {
    scheduleExit(0);
    return;
  }
  const report = await postBounded("namespace.audit", auditBody(scope), { token, serverUrl: resolveServerUrl() });
  const out = healthPayload(renderHealth(report, scope.namespace));
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

runHookMain(main);
