#!/usr/bin/env node
// loom-namespace-health — the session's namespace audit, told at a Stop.
//
// Once per session, ask `namespace.audit` (one day, no samples) how the
// session's namespace is being used and, when it carries a critical finding,
// hand the agent ONE line naming the codes and /loom-optimize — as a
// block-once reason, the one channel a Stop hook has to the model. See
// lib/namespace-health.mjs for the rules.
//
//   UserPromptSubmit  → asks, and keeps the verdict in the session's state.
//                       Claude Code runs this in loom-record's detached
//                       process, so the prompt does not wait for the audit.
//   Stop              → tells a stored verdict once, with no call. A wiring
//                       that starts this script at Stop only (Codex, a
//                       hand-wired client) asks here, on the first Stop, as
//                       does a Stop that finds an ask older than twice the
//                       request bound with no verdict: its process died.
//
// Fail-open everywhere: no token, no scope, server down or slower than the
// hook's bound, a healthy namespace, a stop this hook caused — the agent
// stops as normal and nothing is printed. LOOM_NAMESPACE_HEALTH=off disables.

import {
  envSwitch,
  planeEnabled,
  postBounded,
  readStdinJson,
  requestTimeoutMs,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { auditBody, decideAsk, decideAudit, healthPayload, renderHealth } from "../lib/namespace-health.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

const ENABLED = planeEnabled("memory") && envSwitch("LOOM_NAMESPACE_HEALTH", true);

/** The line for the session's namespace: "" when there is nothing to say. */
async function verdict(payload, token) {
  const scope = resolveScope(String(payload.cwd || process.cwd()));
  if (!scope || !scope.namespace) return "";
  const report = await postBounded("namespace.audit", auditBody(scope), { token, serverUrl: resolveServerUrl() });
  return renderHealth(report, scope.namespace);
}

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
  const cp = sessionStatePath("nshealth", payload.session_id);

  if (process.argv[2] === "UserPromptSubmit") {
    const decision = decideAsk(readJsonFile(cp));
    if (decision.ask) {
      writeJsonFile(cp, decision.state);
      const line = await verdict(payload, token);
      // Read again: a Stop that found this ask overdue has asked in place and marked the session.
      writeJsonFile(cp, { ...readJsonFile(cp), line });
    }
    scheduleExit(0);
    return;
  }

  // A Stop fired by our own block (or another gate's) must never block again.
  if (payload.stop_hook_active) {
    scheduleExit(0);
    return;
  }
  const state = readJsonFile(cp);
  // Twice the bound: the copy stamps its ask, looks the checkout up, and then
  // its request alone may take the bound. A live copy is not asked over.
  const decision = decideAudit(state, 2 * requestTimeoutMs());
  if (decision.cache !== state) writeJsonFile(cp, decision.cache);
  const line = decision.audit ? await verdict(payload, token) : decision.line;
  const out = healthPayload(line);
  if (out) process.stdout.write(JSON.stringify(out) + "\n");
  scheduleExit(0);
}

runHookMain(main);
